"""HTTP-Schnittstelle des Cores.

Intern (vom Telegram-Modul, Token INTERNAL_TOKEN):
  POST /inbound              eingehende Nachricht
  POST /payment              Telegram-Stars-Zahlung (nur Bot-Modus)

Zahlungsanbieter:
  POST /webhooks/stripe      Stripe-Webhook (Signatur über STRIPE_WEBHOOK_SECRET)
  POST /webhooks/payment     allgemeiner Webhook (Token PAYMENT_WEBHOOK_TOKEN), z. B. für Zapier/Make;
                             Body: {"ref": "u<id>-<produkt>"} oder {"user_id", "product_id"}, dazu "payment_id"

Admin (Token ADMIN_TOKEN, ohne Token abgeschaltet):
  POST /admin/grant          Kauf manuell freischalten und ausliefern
  POST /admin/deliver        Produkt erneut ausliefern
  POST /admin/ai             KI für einen User an/aus (Übernahme durch einen Menschen)
  POST /admin/send           Nachricht von Hand an einen User senden
  POST /admin/refund         Zahlung als erstattet markieren (Stars: wird automatisch erstattet)

GET /health                  Status (ohne Token)
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from aiohttp import web

from botdb import UserNotFound

from .config import Settings
from .orchestrator import Orchestrator
from .payments import SignatureError, parse_reference_id, verify_stripe_signature
from .transport import TransportError

log = logging.getLogger(__name__)

ORCH = web.AppKey("orchestrator", Orchestrator)


def _err(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def _token_ok(request: web.Request, token: str | None) -> bool:
    if not token:
        return False
    header = request.headers.get("Authorization", "")
    supplied = header[7:] if header.startswith("Bearer ") else ""
    return hmac.compare_digest(supplied, token)


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text='{"ok":false,"error":"ungültiges JSON"}', content_type="application/json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text='{"ok":false,"error":"JSON-Objekt erwartet"}', content_type="application/json")
    return data


def _user_id(data: dict[str, Any]) -> int:
    uid = data.get("user_id")
    if isinstance(uid, str) and uid.isdigit():
        uid = int(uid)
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise web.HTTPBadRequest(text='{"ok":false,"error":"user_id fehlt"}', content_type="application/json")
    return uid


def build_app(orch: Orchestrator, settings: Settings) -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)
    app[ORCH] = orch
    cfg = orch.cfg

    def require(token: str | None):
        def decorator(handler):
            async def wrapped(request: web.Request) -> web.StreamResponse:
                if not token:
                    return _err(404, "nicht aktiviert")
                if not _token_ok(request, token):
                    return _err(401, "unauthorized")
                return await handler(request)

            return wrapped

        return decorator

    # ------------------------------------------------------------------ intern

    @require(settings.internal_token)
    async def inbound(request: web.Request) -> web.Response:
        data = await _json(request)
        uid = _user_id(data)
        text = data.get("text") or data.get("message_text")
        if not isinstance(text, str) or not text.strip():
            return _err(400, "text fehlt")
        await orch.handle_inbound(uid, text, data.get("username"), data.get("first_name"))
        return web.json_response({"ok": True})

    @require(settings.internal_token)
    async def stars_payment(request: web.Request) -> web.Response:
        data = await _json(request)
        if data.get("event") != "successful_payment":
            return web.json_response({"ok": True, "ignored": True})
        uid = _user_id(data)
        product = cfg.product(str(data.get("payload", "")))
        amount = data.get("total_amount")
        if product is not None and product.price_stars != amount:
            log.warning("Stars-Betrag %s weicht vom Preis %s ab (%s)", amount, product.price_stars, product.id)
        await orch.complete_purchase(
            uid, str(data.get("payload", "")), str(data.get("charge_id") or ""), "stars", amount, data.get("currency")
        )
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------ Zahlungsanbieter

    async def stripe_webhook(request: web.Request) -> web.Response:
        if not settings.stripe_webhook_secret:
            return _err(404, "nicht aktiviert")
        body = await request.read()
        try:
            event = verify_stripe_signature(body, request.headers.get("Stripe-Signature"), settings.stripe_webhook_secret)
        except SignatureError as exc:
            log.warning("Stripe-Webhook abgelehnt: %s", exc)
            return _err(400, str(exc))

        kind = event.get("type")
        session = (event.get("data") or {}).get("object") or {}
        paid = (kind == "checkout.session.completed" and session.get("payment_status") == "paid") or (
            kind == "checkout.session.async_payment_succeeded"
        )
        if not paid:
            return web.json_response({"ok": True, "ignored": kind})
        ref = parse_reference_id(session.get("client_reference_id"))
        if ref is None:
            log.error("Stripe-Zahlung %s ohne gültige client_reference_id", session.get("id"))
            return web.json_response({"ok": True, "ignored": "client_reference_id"})
        uid, product_id = ref
        await orch.complete_purchase(
            uid, product_id, session.get("id"), "stripe", session.get("amount_total"), session.get("currency")
        )
        return web.json_response({"ok": True})

    @require(settings.payment_webhook_token)
    async def generic_payment(request: web.Request) -> web.Response:
        data = await _json(request)
        ref = parse_reference_id(str(data["ref"])) if data.get("ref") else None
        if data.get("ref") and ref is None:
            return _err(400, "ref ungültig (erwartet u<user_id>-<product_id>)")
        uid, product_id = ref if ref else (_user_id(data), str(data.get("product_id", "")))
        if cfg.product(product_id) is None:
            return _err(400, "unbekannte product_id")
        payment_id = data.get("payment_id")
        new = await orch.complete_purchase(
            uid, product_id, str(payment_id) if payment_id else None, str(data.get("provider") or "link"),
            data.get("amount"), data.get("currency"),
        )
        return web.json_response({"ok": True, "new": new})

    # ------------------------------------------------------------------ Admin

    @require(settings.admin_token)
    async def admin_grant(request: web.Request) -> web.Response:
        data = await _json(request)
        uid, product_id = _user_id(data), str(data.get("product_id", ""))
        if cfg.product(product_id) is None:
            return _err(400, "unbekannte product_id")
        new = await orch.complete_purchase(uid, product_id, data.get("payment_id"), "manual")
        return web.json_response({"ok": True, "new": new})

    @require(settings.admin_token)
    async def admin_deliver(request: web.Request) -> web.Response:
        data = await _json(request)
        uid, product = _user_id(data), cfg.product(str(data.get("product_id", "")))
        if product is None:
            return _err(400, "unbekannte product_id")
        await orch.deliver(uid, product)
        return web.json_response({"ok": True})

    @require(settings.admin_token)
    async def admin_ai(request: web.Request) -> web.Response:
        data = await _json(request)
        uid, enabled = _user_id(data), data.get("enabled")
        if not isinstance(enabled, bool):
            return _err(400, "enabled muss true oder false sein")
        try:
            await orch.repo.set_ai_enabled(uid, enabled)
        except UserNotFound:
            return _err(404, "User unbekannt")
        return web.json_response({"ok": True})

    @require(settings.admin_token)
    async def admin_send(request: web.Request) -> web.Response:
        data = await _json(request)
        uid, text = _user_id(data), data.get("text")
        if not isinstance(text, str) or not text.strip():
            return _err(400, "text fehlt")
        try:
            await orch.send_text(uid, text, verbatim=True)
        except TransportError as exc:
            return _err(502, str(exc))
        try:
            await orch.repo.add_message(uid, "assistant", text.strip())
        except UserNotFound:
            pass
        return web.json_response({"ok": True})

    @require(settings.admin_token)
    async def admin_refund(request: web.Request) -> web.Response:
        data = await _json(request)
        payment = await orch.repo.get_payment(str(data.get("payment_id", "")))
        if payment is None:
            return _err(404, "Zahlung unbekannt")
        if payment["refunded"]:
            return web.json_response({"ok": True, "already": True})
        note = "Geld beim Zahlungsanbieter selbst zurückbuchen."
        if payment["provider"] == "stars":
            try:
                await orch.transport.refund_stars(payment["user_id"], payment["payment_id"])
            except TransportError as exc:
                return _err(502, str(exc))
            note = "Stars wurden erstattet."
        await orch.repo.mark_refunded(payment["payment_id"])
        return web.json_response({"ok": True, "note": note})

    async def health(_: web.Request) -> web.Response:
        try:
            await orch.repo.get_product("__health__")
        except Exception as exc:
            return web.json_response({"ok": False, "database": repr(exc)}, status=503)
        return web.json_response({"ok": True, "transport": orch.transport.kind, "payments": cfg.payment_method})

    app.add_routes(
        [
            web.post("/inbound", inbound),
            web.post("/payment", stars_payment),
            web.post("/webhooks/stripe", stripe_webhook),
            web.post("/webhooks/payment", generic_payment),
            web.post("/admin/grant", admin_grant),
            web.post("/admin/deliver", admin_deliver),
            web.post("/admin/ai", admin_ai),
            web.post("/admin/send", admin_send),
            web.post("/admin/refund", admin_refund),
            web.get("/health", health),
        ]
    )
    return app
