"""Ziele für Inbound-Events. Für eine Message Queue einfach eine weitere
Klasse mit `async def emit(self, event: dict)` schreiben (z. B. Redis, RabbitMQ)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol

import aiohttp

log = logging.getLogger(__name__)


class Sink(Protocol):
    async def emit(self, event: dict) -> None: ...
    async def close(self) -> None: ...


class LogSink:
    """Fallback ohne Ziel-URL: schreibt Events nur ins Log."""

    def __init__(self, name: str):
        self.name = name

    async def emit(self, event: dict) -> None:
        log.info("[%s] %s", self.name, json.dumps(event, ensure_ascii=False))

    async def close(self) -> None:
        pass


class HttpSink:
    """POSTet jedes Event als JSON, mit Retries und exponentiellem Backoff."""

    def __init__(self, url: str, token: str = "", retries: int = 4, timeout: float = 10):
        self.url = url
        self.retries = retries
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout, headers=self._headers)
        return self._session

    async def emit(self, event: dict) -> None:
        session = await self._get_session()
        for attempt in range(1, self.retries + 1):
            try:
                async with session.post(self.url, json=event) as resp:
                    if resp.status < 500:
                        if resp.status >= 400:
                            log.error("Inbound abgelehnt (%s): %s", resp.status, await resp.text())
                        return
                    log.warning("Inbound %s, Versuch %d/%d", resp.status, attempt, self.retries)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Inbound-Fehler %r, Versuch %d/%d", e, attempt, self.retries)
            await asyncio.sleep(min(2 ** attempt, 30))
        log.error("Event verworfen nach %d Versuchen: %s", self.retries, event)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


def make_sink(url: str, token: str, name: str) -> Sink:
    return HttpSink(url, token) if url else LogSink(name)
