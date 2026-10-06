"""HTTP-API für Outbound-Befehle vom Core.

POST /send     {"user_id", "text_chunks", "delays"}          -> 202 (Hintergrund) | ?wait=1 -> 200
POST /invoice  {"user_id", "title", "description", "payload", "amount"}
POST /refund   {"user_id", "charge_id"}
GET  /health
"""
from __future__ import annotations

import asyncio
import hmac
import logging

from aiohttp import web
from telethon.errors import RPCError

from .adapter import AdapterError, TelegramAdapter, UnknownUserError, validate_outbound

log = logging.getLogger(__name__)
ADAPTER = web.AppKey("adapter", TelegramAdapter)
TASKS = web.AppKey("tasks", set)


def _err(status: int, msg: str) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=status)


def make_app(adapter: TelegramAdapter, token: str = "") -> web.Application:
    @web.middleware
    async def auth(request: web.Request, handler):
        if token and request.path != "/health":
            given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(given, token):
                return _err(401, "unauthorized")
        return await handler(request)

    app = web.Application(middlewares=[auth], client_max_size=1024 * 1024)
    app[ADAPTER] = adapter
    app[TASKS] = set()

    async def read_json(request: web.Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text='{"ok":false,"error":"invalid JSON"}', content_type="application/json")
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text='{"ok":false,"error":"JSON object expected"}', content_type="application/json")
        return data

    async def run(coro):
        try:
            return await coro, None
        except UnknownUserError as e:
            return None, _err(404, str(e))
        except AdapterError as e:
            return None, _err(400, str(e))
        except RPCError as e:
            log.error("Telegram-Fehler: %r", e)
            return None, _err(502, f"telegram: {e.__class__.__name__}")

    async def send(request: web.Request) -> web.Response:
        data = await read_json(request)
        uid, chunks, delays = data.get("user_id"), data.get("text_chunks"), data.get("delays")
        try:
            validate_outbound(uid, chunks, delays)
        except AdapterError as e:
            return _err(400, str(e))

        if request.query.get("wait") in ("1", "true"):
            sent, err = await run(adapter.send_message_chunks(uid, chunks, delays))
            return err or web.json_response({"ok": True, "messages_sent": sent})

        async def background():
            _, err = await run(adapter.send_message_chunks(uid, chunks, delays))
            if err:
                log.error("Senden an %s fehlgeschlagen: %s", uid, err.text)

        task = asyncio.create_task(background())
        app[TASKS].add(task)
        task.add_done_callback(app[TASKS].discard)
        return web.json_response({"ok": True, "queued": len(chunks)}, status=202)

    async def invoice(request: web.Request) -> web.Response:
        d = await read_json(request)
        try:
            args = (int(d["user_id"]), str(d["title"]), str(d["description"]), str(d["payload"]), d["amount"])
        except (KeyError, TypeError, ValueError):
            return _err(400, "user_id, title, description, payload, amount erforderlich")
        _, err = await run(adapter.send_stars_invoice(*args))
        return err or web.json_response({"ok": True})

    async def refund(request: web.Request) -> web.Response:
        d = await read_json(request)
        try:
            uid, charge = int(d["user_id"]), str(d["charge_id"])
        except (KeyError, TypeError, ValueError):
            return _err(400, "user_id und charge_id erforderlich")
        _, err = await run(adapter.refund_stars(uid, charge))
        return err or web.json_response({"ok": True})

    async def health(_: web.Request) -> web.Response:
        connected = adapter.client.is_connected()
        return web.json_response({"ok": connected}, status=200 if connected else 503)

    async def drain(app: web.Application):
        if app[TASKS]:
            log.info("Warte auf %d laufende Sendungen", len(app[TASKS]))
            await asyncio.gather(*app[TASKS], return_exceptions=True)

    app.on_shutdown.append(drain)
    app.add_routes([
        web.post("/send", send),
        web.post("/invoice", invoice),
        web.post("/refund", refund),
        web.get("/health", health),
    ])
    return app
