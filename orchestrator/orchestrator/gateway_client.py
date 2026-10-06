"""HTTP-Client für den Sendeauftrag an das Telegram-Gateway (POST /send)."""

from __future__ import annotations

import logging
from typing import Sequence

import aiohttp

log = logging.getLogger(__name__)


class GatewayError(RuntimeError):
    pass


class GatewayPaused(GatewayError):
    """Der Chat ist im Gateway pausiert (/pause) oder der Versand wurde dadurch abgebrochen."""


class GatewayClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(headers=self._headers)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def send(
        self,
        user_id: int,
        chunks: Sequence[str],
        delays: Sequence[float],
        *,
        wait: bool = True,
    ) -> int:
        """Schickt einen Sendeauftrag. Mit wait=True kehrt der Aufruf erst nach dem letzten Chunk zurück.

        Gibt die Anzahl zugestellter Chunks zurück (0 bei wait=False).
        """
        assert self._session is not None, "start() wurde nicht aufgerufen"
        if not chunks:
            return 0
        payload = {"user_id": user_id, "chunks": list(chunks), "delays": list(delays)}
        timeout = aiohttp.ClientTimeout(total=(sum(delays) + 60) if wait else 15)
        params = {"wait": "1"} if wait else None
        try:
            async with self._session.post(
                f"{self._base}/send", json=payload, params=params, timeout=timeout
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 409:
                    raise GatewayPaused(f"Chat {user_id} ist pausiert")
                if resp.status >= 300:
                    raise GatewayError(f"Gateway HTTP {resp.status}: {body}")
                return int(body.get("sent", 0)) if wait else 0
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise GatewayError(f"Gateway nicht erreichbar: {exc!r}") from exc
