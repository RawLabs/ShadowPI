"""Helpers for calling the CAS (Combot Anti-Spam) API."""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CasCheckResult:
    """Represents the response of a CAS per-user check."""

    ok: bool
    is_banned: bool
    reason: str | None
    service: str | None
    raw: dict[str, Any]

    @property
    def should_ban(self) -> bool:
        return self.ok and self.is_banned


class CasClient:
    """HTTP client for the CAS endpoints."""

    def __init__(self, base_url: str, export_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.export_url = export_url
        self.timeout = httpx.Timeout(timeout)
        self._client = httpx.AsyncClient(timeout=self.timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def check_user(self, user_id: int) -> CasCheckResult:
        url = f"{self.base_url}/check"
        params = {"user_id": user_id}
        try:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:  # pragma: no cover - network failures
            logger.warning("CAS /check failed for user=%s: %s", user_id, exc)
            return CasCheckResult(False, False, None, None, {"error": str(exc)})

        payload = response.json()
        result = payload.get("result") or {}
        is_banned = bool(result.get("is_banned") or result.get("banned"))
        reason = result.get("offense") or result.get("reason")
        service = result.get("service")
        return CasCheckResult(
            ok=bool(payload.get("ok", True)),
            is_banned=is_banned,
            reason=reason,
            service=service,
            raw=result,
        )

    async def fetch_bulk_user_ids(self) -> list[tuple[int, str | None]]:
        """Download and parse the CSV export.

        Returns a list of (user_id, reason) tuples.
        """

        try:
            response = await self._client.get(self.export_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:  # pragma: no cover - network failures
            logger.warning("CAS export download failed: %s", exc)
            return []

        text_stream = io.StringIO(response.text)
        reader = csv.reader(text_stream)
        parsed: list[tuple[int, str | None]] = []
        for row in reader:
            if not row:
                continue
            user_raw = row[0].strip()
            if not user_raw or user_raw.startswith("#"):
                continue
            try:
                user_id = int(user_raw)
            except ValueError:
                continue
            reason = row[1].strip() if len(row) > 1 and row[1].strip() else None
            parsed.append((user_id, reason))
        return parsed

    async def sync_watchlist(
        self,
        current_ids: Iterable[int],
    ) -> set[int]:
        """Utility to compare remote export with an existing ID set."""

        remote = await self.fetch_bulk_user_ids()
        remote_ids = {item[0] for item in remote}
        missing = remote_ids.difference(current_ids)
        return missing
