"""HTTP-Client für das Telegram-Modul (telegram-bot oder telegram-userbot).

Beide Module haben fast dieselbe Schnittstelle. Die Unterschiede (Feldnamen,
Stars nur beim Bot) werden hier versteckt.
"""

from __future__ import annotations

import logging
from typing import Sequence

import aiohttp

log = logging.getLogger(__name__)

TELEGRAM_MAX_TEXT = 4096


class TransportError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Telegram-Modul antwortet {status}: {message}")
        self.status = status


class UserUnreachable(TransportError):
    """User unbekannt (404) oder Chat pausiert (409)."""


def split_long(chunks: Sequence[str], delays: Sequence[float]) -> tuple[list[str], list[float]]:
    """Teilt Chunks über dem Telegram-Limit, das Delay gilt für das erste Teilstück."""
    out_chunks: list[str] = []
    out_delays: list[float] = []
    for chunk, delay in zip(chunks, delays):
        first = True
        while chunk:
            part = chunk[:TELEGRAM_MAX_TEXT]
            if len(chunk) > TELEGRAM_MAX_TEXT:
                cut = max(part.rfind("\n"), part.rfind(" "))
                if cut > 0:
                    part = chunk[:cut]
            chunk = chunk[len(part):].lstrip()
            if part.strip():
                out_chunks.append(part)
                out_delays.append(delay if first else 0.5)
                first = False
    return out_chunks, out_delays


class TransportClient:
    def __init__(self, kind: str, base_url: str, token: str, session: aiohttp.ClientSession | None = None):
        if kind not in ("bot", "userbot"):
            raise ValueError("kind muss 'bot' oder 'userbot' sein")
        self.kind = kind
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._session = session
        self._own_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    async def close(self) -> None:
        if self._own_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def _post(self, path: str, body: dict, timeout: float = 30) -> dict:
        session = await self._get_session()
        try:
            async with session.post(
                self._base + path,
                json=body,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = {"error": await resp.text()}
                if resp.status < 300:
                    return data or {}
                message = str(data.get("error") or data)
                if resp.status in (404, 409):
                    raise UserUnreachable(resp.status, message)
                raise TransportError(resp.status, message)
        except aiohttp.ClientError as exc:
            raise TransportError(0, f"nicht erreichbar: {exc!r}") from exc

    async def send_chunks(self, user_id: int, chunks: Sequence[str], delays: Sequence[float]) -> None:
        """Sendet Chunks mit Tipp-Status und wartet, bis alles zugestellt ist."""
        chunks, delays = split_long(chunks, delays)
        if not chunks:
            return
        key = "text_chunks" if self.kind == "bot" else "chunks"
        body = {"user_id": user_id, key: chunks, "delays": delays}
        await self._post("/send?wait=1", body, timeout=sum(delays) + 60)

    async def send_file(self, user_id: int, file: str, caption: str = "") -> None:
        await self._post("/send_file", {"user_id": user_id, "file": file, "caption": caption}, timeout=120)

    async def send_invoice(self, user_id: int, title: str, description: str, payload: str, amount: int) -> None:
        if self.kind != "bot":
            raise TransportError(0, "Stars-Rechnungen gibt es nur im Bot-Modus")
        await self._post(
            "/invoice",
            {
                "user_id": user_id,
                "title": title[:32],
                "description": (description or title)[:255],
                "payload": payload,
                "amount": amount,
            },
        )

    async def refund_stars(self, user_id: int, charge_id: str) -> None:
        if self.kind != "bot":
            raise TransportError(0, "Stars-Erstattungen gibt es nur im Bot-Modus")
        await self._post("/refund", {"user_id": user_id, "charge_id": charge_id})
