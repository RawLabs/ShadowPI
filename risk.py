"""Risk scoring helpers for ShadowPI."""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque

from telegram import Message

from .config import Settings


URL_REGEX = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(slots=True)
class RiskAssessment:
    score: int
    reasons: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def escalate(self, action: str) -> None:
        if action not in self.actions:
            self.actions.append(action)


class RiskScorer:
    """Applies heuristic scoring based on live message metadata."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._message_windows: dict[int, Deque[int]] = defaultdict(deque)
        self._last_message: dict[int, tuple[str, int]] = {}

    def _track_message(self, user_id: int, timestamp: int) -> int:
        window = self.settings.flood_window_seconds
        dq = self._message_windows[user_id]
        dq.append(timestamp)
        while dq and timestamp - dq[0] > window:
            dq.popleft()
        return len(dq)

    def _check_repeat(self, user_id: int, text: str, timestamp: int) -> bool:
        normalized = " ".join(text.split())
        prev_text, prev_ts = self._last_message.get(user_id, ("", 0))
        self._last_message[user_id] = (normalized, timestamp)
        if not prev_text:
            return False
        within_window = timestamp - prev_ts <= self.settings.rate_repeat_window_seconds
        return within_window and normalized == prev_text

    def evaluate(
        self,
        message: Message,
        *,
        cas_banned: bool,
        watchlist_reason: str | None,
        newbie_restricted: bool,
        contains_link: bool,
        contains_blacklist: bool,
    ) -> RiskAssessment:
        timestamp = int(message.date.timestamp()) if message.date else int(time.time())
        user_id = message.from_user.id if message.from_user else 0
        assessment = RiskAssessment(score=0)

        if cas_banned:
            assessment.score = max(
                assessment.score,
                self.settings.ban_score_threshold + 20,
            )
            assessment.reasons.append("CAS flagged user")

        if watchlist_reason:
            assessment.score = max(
                assessment.score,
                self.settings.mute_score_threshold,
            )
            assessment.reasons.append(f"CAS export match ({watchlist_reason})")

        count = self._track_message(user_id, timestamp)
        if count >= self.settings.flood_message_threshold:
            assessment.score += 20
            assessment.reasons.append(
                f"Sent {count} msgs/{self.settings.flood_window_seconds}s"
            )

        text_content = message.text or message.caption or ""
        if text_content and self._check_repeat(user_id, text_content, timestamp):
            assessment.score += 15
            assessment.reasons.append("Repeated identical message")

        if (
            getattr(message, "forward_origin", None)
            or getattr(message, "forward_from_chat", None)
            or getattr(message, "forward_from", None)
        ):
            assessment.score += 10
            assessment.reasons.append("Forwarded-only content")

        if contains_link and newbie_restricted:
            assessment.score += 20
            assessment.reasons.append("Link posted during probation period")
        elif contains_link:
            assessment.score += 5
            assessment.reasons.append("Link sent")

        if contains_blacklist:
            assessment.score += 30
            assessment.reasons.append("Matched blacklist keyword/domain")

        if assessment.score >= self.settings.ban_score_threshold:
            assessment.escalate("ban")
        elif assessment.score >= self.settings.mute_score_threshold:
            assessment.escalate("mute")
        elif assessment.score >= self.settings.warn_score_threshold:
            assessment.escalate("warn")

        if "ban" in assessment.actions or contains_blacklist:
            assessment.escalate("delete")

        if newbie_restricted and contains_link:
            assessment.escalate("delete")

        return assessment


def detect_link(message: Message) -> bool:
    for text in (message.text, message.caption):
        if text and URL_REGEX.search(text):
            return True
    return False
