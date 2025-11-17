"""Configuration helpers for the ShadowPI bot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(slots=True)
class Settings:
    """Runtime configuration loaded from environment variables."""

    bot_token: str
    data_dir: Path
    database_path: Path
    cas_api_base: str = "https://api.cas.chat"
    cas_bulk_url: str = "https://api.cas.chat/export.csv"
    http_timeout_seconds: float = 10.0
    cas_export_refresh_minutes: int = 30
    newbie_link_block_seconds: int = 600
    flood_window_seconds: int = 10
    flood_message_threshold: int = 5
    rate_repeat_window_seconds: int = 120
    blacklisted_keywords: list[str] = field(default_factory=list)
    blacklisted_domains: list[str] = field(default_factory=list)
    mod_log_chat_id: int | None = None
    attribution_text: str | None = "Powered by CAS (cas.chat)"
    activation_pin: str = "80085"

    warn_score_threshold: int = 30
    mute_score_threshold: int = 60
    ban_score_threshold: int = 100

    @classmethod
    def from_env(cls) -> "Settings":
        token = (
            os.getenv("SHADOWPI_BOT_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
            or ""
        )
        if not token:
            raise RuntimeError("Missing SHADOWPI_BOT_TOKEN/TELEGRAM_BOT_TOKEN env var")

        data_dir = _ensure_directory(
            Path(os.getenv("SHADOWPI_DATA_DIR", "ShadowPI_data")).resolve()
        )
        database_path = data_dir / "shadowpi.sqlite3"

        keywords = _split_csv(os.getenv("SHADOWPI_KEYWORDS")) or [
            "crypto",
            "nude",
            "porn",
            "investment",
        ]
        domains = _split_csv(os.getenv("SHADOWPI_DOMAINS")) or [
            "t.me/joinchat",
            "bit.ly",
            "tinyurl.com",
            "grabify",
        ]

        mod_log_chat = os.getenv("SHADOWPI_MOD_LOG_CHAT")
        mod_log_chat_id = int(mod_log_chat) if mod_log_chat else None

        return cls(
            bot_token=token,
            data_dir=data_dir,
            database_path=database_path,
            cas_api_base=os.getenv("SHADOWPI_CAS_BASE", "https://api.cas.chat"),
            cas_bulk_url=os.getenv("SHADOWPI_CAS_EXPORT", "https://api.cas.chat/export.csv"),
            http_timeout_seconds=_env_float("SHADOWPI_HTTP_TIMEOUT", 10.0),
            cas_export_refresh_minutes=_env_int("SHADOWPI_CAS_REFRESH_MIN", 60),
            newbie_link_block_seconds=_env_int("SHADOWPI_NEWBIE_BLOCK", 600),
            flood_window_seconds=_env_int("SHADOWPI_FLOOD_WINDOW", 10),
            flood_message_threshold=_env_int("SHADOWPI_FLOOD_MSGS", 5),
            rate_repeat_window_seconds=_env_int("SHADOWPI_REPEAT_WINDOW", 120),
            blacklisted_keywords=keywords,
            blacklisted_domains=domains,
            mod_log_chat_id=mod_log_chat_id,
            attribution_text=os.getenv("SHADOWPI_ATTRIBUTION", "Powered by CAS (cas.chat)"),
            activation_pin=os.getenv("SHADOWPI_ACTIVATION_PIN", "80085"),
            warn_score_threshold=_env_int("SHADOWPI_WARN_THRESHOLD", 30),
            mute_score_threshold=_env_int("SHADOWPI_MUTE_THRESHOLD", 60),
            ban_score_threshold=_env_int("SHADOWPI_BAN_THRESHOLD", 100),
        )


def format_attribution(settings: Settings) -> str:
    if not settings.attribution_text:
        return ""
    if "cas.chat" in settings.attribution_text.lower() or "cas" in settings.attribution_text.lower():
        return settings.attribution_text
    return f"{settings.attribution_text} | Powered by CAS (cas.chat)"


def contains_blacklisted(value: str, bad_terms: Iterable[str]) -> bool:
    lowered = value.lower()
    return any(term.lower() in lowered for term in bad_terms)
