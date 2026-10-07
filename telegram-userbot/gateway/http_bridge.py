"""HTTP-Schnittstelle zum restlichen System.

Ausgang:  POST /send       nimmt Sendeaufträge an (Bearer-Token nötig)
          POST /send_file  sendet eine Datei aus CONTENT_DIR (Bearer-Token nötig)
          GET  /paused     Liste der pausierten user_ids
          GET  /health     Verbindungsstatus (ohne Token)
Eingang:  WebhookForwarder schickt jedes IncomingMessage als JSON per POST
          an INBOUND_WEBHOOK_URL.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

import aiohttp
from aiohttp import web

from .config import Config
from .core import InvalidFile, TelegramGateway, UnknownUser, UserPaused
from .models import IncomingMessage, SendJob

log = logging.getLogger("gateway.http")

PUBLIC_PATHS = {"/health"}


def build_app(gateway: TelegramGateway, config: Config, client: Any = None) -> web.Application:
    token = config.api_token or ""

    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path not in PUBLIC_PATHS:
            header = request.headers.get("Authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else ""
            if not token or not hmac.compare_digest(supplied, token):
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def send(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "ungültiges JSON"}, status=400)
        try:
            job = SendJob.from_dict(data, max_delay=config.max_delay)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        try:
            task = gateway.submit(job)
        except UserPaused:
            return web.json_response({"error": "paused", "user_id": job.user_id}, status=409)

        if request.query.get("wait", "").lower() not in {"1", "true", "yes"}:
            return web.json_response({"status": "queued", "user_id": job.user_id}, status=202)

        # wait=1: auf das Ende warten, ohne den Sendeauftrag an den HTTP-Request zu koppeln.
        await asyncio.wait({task})
        if task.cancelled():
            return web.json_response({"status": "cancelled", "reason": "paused"}, status=409)
        exc = task.exception()
        if isinstance(exc, UserPaused):
            return web.json_response({"error": "paused", "user_id": job.user_id}, status=409)
        if isinstance(exc, UnknownUser):
            return web.json_response({"error": str(exc)}, status=404)
        if exc is not None:
            return web.json_response({"error": repr(exc)}, status=502)
        return web.json_response({"status": "sent", "sent": task.result()})

    async def send_file(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "ungültiges JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "JSON-Objekt erwartet"}, status=400)
        user_id, caption = data.get("user_id"), data.get("caption") or ""
        if isinstance(user_id, bool) or not isinstance(user_id, int) or not isinstance(caption, str):
            return web.json_response({"error": "user_id (Zahl) und caption (Text) erwartet"}, status=400)
        try:
            await gateway.send_file(user_id, data.get("file"), caption)
        except InvalidFile as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except UserPaused:
            return web.json_response({"error": "paused", "user_id": user_id}, status=409)
        except UnknownUser as exc:
            return web.json_response({"error": str(exc)}, status=404)
        except Exception as exc:  # Telegram-Fehler
            log.error("Datei an %s fehlgeschlagen: %r", user_id, exc)
            return web.json_response({"error": repr(exc)}, status=502)
        return web.json_response({"status": "sent"})

    async def paused(_: web.Request) -> web.Response:
        return web.json_response({"paused": gateway.pauses.snapshot()})

    async def health(_: web.Request) -> web.Response:
        connected = bool(client.is_connected()) if client is not None else True
        return web.json_response(
            {"ok": connected, "connected": connected, "paused_count": len(gateway.pauses)},
            status=200 if connected else 503,
        )

    app = web.Application(middlewares=[auth], client_max_size=256 * 1024)
    app.router.add_post("/send", send)
    app.router.add_post("/send_file", send_file)
    app.router.add_get("/paused", paused)
    app.router.add_get("/health", health)
    return app


class WebhookForwarder:
    """Leitet eingehende Nachrichten per HTTP-POST an das System weiter."""

    def __init__(self, url: str, token: str | None = None, retries: int = 4) -> None:
        self._url = url
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._retries = retries
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def __call__(self, message: IncomingMessage) -> None:
        assert self._session is not None, "start() wurde nicht aufgerufen"
        payload = message.to_dict()
        for attempt in range(1, self._retries + 1):
            try:
                async with self._session.post(
                    self._url, json=payload, headers=self._headers
                ) as resp:
                    if resp.status < 300:
                        return
                    if 400 <= resp.status < 500 and resp.status not in (408, 429):
                        log.error(
                            "Webhook lehnt Event für %s ab (HTTP %s), verworfen",
                            message.user_id,
                            resp.status,
                        )
                        return
                    log.warning("Webhook antwortet HTTP %s (Versuch %d)", resp.status, attempt)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("Webhook nicht erreichbar: %r (Versuch %d)", exc, attempt)
            if attempt < self._retries:
                await asyncio.sleep(min(2**attempt, 15))
        log.error("Event für User %s nach %d Versuchen verworfen", message.user_id, self._retries)


async def log_only_handler(message: IncomingMessage) -> None:
    """Fallback ohne INBOUND_WEBHOOK_URL: Events nur ins Log schreiben."""
    log.info("Eingehend (kein Webhook gesetzt): %s", message.to_dict())
