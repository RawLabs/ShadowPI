"""Member sweep helpers for detecting deleted/suspicious accounts."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


from .config import Settings
from .database import Database


@dataclass(slots=True)
class MemberRisk:
    user_id: int
    display: str
    username: str | None
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    is_deleted: bool = False

    def add(self, points: int, reason: str) -> None:
        if points:
            self.score += points
        if reason not in self.reasons:
            self.reasons.append(reason)

    def require(self, action: str) -> None:
        if action not in self.actions:
            self.actions.append(action)


@dataclass(slots=True)
class SweepStats:
    chat_title: str
    total_members: int = 0
    deleted_accounts: list[MemberRisk] = field(default_factory=list)
    cas_hits: list[MemberRisk] = field(default_factory=list)
    high_risk: list[MemberRisk] = field(default_factory=list)
    silent_watchers: list[MemberRisk] = field(default_factory=list)
    actions_taken: int = 0
    shadowbans_applied: int = 0

    def as_text(self) -> str:
        lines = [
            f"ShadowPI sweep for {self.chat_title}",
            f"Members scanned: {self.total_members}",
            f"Deleted accounts found: {len(self.deleted_accounts)}",
            f"CAS/banlist hits: {len(self.cas_hits)}",
            f"High-risk profiles: {len(self.high_risk)}",
            f"Silent watchers: {len(self.silent_watchers)}",
            f"Actions taken: {self.actions_taken}",
            f"Shadowbans applied: {self.shadowbans_applied}",
        ]

        def _append_section(title: str, risks: list[MemberRisk]) -> None:
            if not risks:
                return
            lines.append(f"\n{title}:")
            for risk in risks[:5]:
                reasons = ", ".join(risk.reasons) or "no reasons"
                lines.append(f"- {risk.display} (score {risk.score}): {reasons}")

        _append_section("Deleted members", self.deleted_accounts)
        _append_section("High-risk", self.high_risk)
        _append_section("Silent watchers", self.silent_watchers)

        return "\n".join(lines)


class MemberRiskAssessor:
    """Applies heuristic scoring for sleeper/mole detection."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.now = int(time.time())
        self.silent_days = 7
        self.ghost_days = 30
        self.forward_window = 600
        self.shadowban_threshold = 80
        self.flag_threshold = 60

    def _age_days(self, first_seen: int | None) -> float:
        if not first_seen:
            return 0.0
        return (self.now - first_seen) / 86400

    def assess(self, profile: dict[str, Any]) -> MemberRisk:
        user_id = profile.get("user_id")
        username = profile.get("username")
        full_name = profile.get("full_name")
        display = full_name or (f"@{username}" if username else str(user_id))
        risk = MemberRisk(user_id=user_id, display=display, username=username)

        if profile.get("is_deleted") or (full_name == "Deleted Account" and not username):
            risk.is_deleted = True
            risk.require("kick")
            risk.add(0, "Deleted account shell")
            return risk

        first_seen = profile.get("first_seen") or self.now
        messages_sent = profile.get("messages_sent", 0) or 0
        forwards_sent = profile.get("forwards_sent", 0) or 0
        warnings = profile.get("warnings", 0) or 0
        deletions = profile.get("deleted_by_mod", 0) or 0
        identity_changes = profile.get("identity_changes", 0) or 0
        first_message_ts = profile.get("first_message_ts", 0) or 0
        first_message_type = profile.get("first_message_type")
        first_forward_ts = profile.get("first_forward_ts", 0) or 0
        cas_status = profile.get("cas_status")
        shadowbanned = profile.get("shadowbanned")

        age_days = self._age_days(first_seen)

        if messages_sent == 0 and age_days >= self.silent_days:
            risk.add(10, f"Silent watcher for {int(age_days)}d")

        if not username and (not full_name or " " not in full_name) and age_days >= self.ghost_days:
            risk.add(15, "Ghost profile (no username/last name)")

        if forwards_sent and forwards_sent >= max(3, messages_sent):
            risk.add(10, "Forward-only activity")

        if first_forward_ts and (first_forward_ts - first_seen) <= self.forward_window:
            risk.add(25, "Forward-on-join pattern")

        if first_message_ts and first_message_type == "link" and (
            first_message_ts - first_seen
        ) <= self.forward_window:
            risk.add(20, "Link dropped immediately after join")

        if identity_changes >= 3:
            risk.add(20, "Identity morphing detected")

        if warnings or deletions:
            risk.add(30, "Prior incidents across groups")

        if cas_status == "banned":
            risk.add(80, "CAS/export flagged")
            risk.require("ban")

        if shadowbanned:
            risk.add(10, "Already shadowbanned")

        if risk.score >= self.shadowban_threshold:
            risk.require("shadowban")
        elif risk.score >= self.flag_threshold:
            risk.require("flag")

        return risk


async def run_member_sweep(
    chat_id: int,
    chat_title: str,
    *,
    db: Database,
    settings: Settings,
    mode: str = "report",
    limit: int | None = None,
    ban_callback=None,
    shadowban_callback=None,
) -> SweepStats:
    assessor = MemberRiskAssessor(settings)
    stats = SweepStats(chat_title=chat_title)

    profiles = db.users_by_chat(chat_id, limit)
    stats.total_members = len(profiles)

    for profile in profiles:
        risk = assessor.assess(profile)

        if risk.is_deleted:
            stats.deleted_accounts.append(risk)
            if mode != "report" and ban_callback:
                await ban_callback(profile["user_id"], reason="Deleted account")
            stats.actions_taken += 1
            continue

        if "ban" in risk.actions:
            stats.cas_hits.append(risk)
            if mode != "report" and ban_callback:
                await ban_callback(profile["user_id"], reason="Banlist hit")
                stats.actions_taken += 1
            continue

        if risk.score >= assessor.flag_threshold:
            stats.high_risk.append(risk)
        if any(reason.startswith("Silent watcher") for reason in risk.reasons):
            stats.silent_watchers.append(risk)

        if mode != "report" and "shadowban" in risk.actions and shadowban_callback:
            await shadowban_callback(profile["user_id"])
            stats.shadowbans_applied += 1
            stats.actions_taken += 1

    return stats
