"""HTTP-Schnittstelle des Orchestrators.

POST /inbound                   Events vom Telegram-Gateway (Bearer INBOUND_TOKEN)
POST /payments/stripe/webhook   Stripe-Webhook (Stripe-Signatur)
GET  /payments/dummy/{ref}      Testseite, nur mit PAYMENT_PROVIDER=dummy
POST /payments/dummy/{ref}      Testzahlung abschließen, nur mit PAYMENT_PROVIDER=dummy
GET  /health                    Lebenszeichen
"""

from __future__ import annotations

import hmac
import html
import logging

from aiohttp import web

from botdb import BotRepository, PaymentMismatch, PaymentNotFound

from .config import Config
from .conversation import ConversationManager, Inbound
from .payments import InvalidWebhook, PaymentEvent, PaymentProvider, format_price
from .payments.dummy import DummyProvider

log = logging.getLogger(__name__)


def build_app(
    manager: ConversationManager,
    payments: PaymentProvider,
    repo: BotRepository,
    config: Config,
) -> web.Application:
    async def inbound(request: web.Request) -> web.Response:
        header = request.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied, config.inbound_token):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            msg = Inbound.from_payload(await request.json())
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        manager.enqueue(msg)
        return web.json_response({"status": "accepted"}, status=202)

    async def process_event(event: PaymentEvent) -> web.Response:
        try:
            await manager.handle_payment(event)
        except PaymentNotFound:
            # Gehört nicht zu uns (z. B. andere Integration im selben Stripe-Konto)
            log.warning("Unbekannte Zahlung %s:%s ignoriert", event.provider, event.ref)
            return web.json_response({"status": "ignored"})
        except PaymentMismatch as exc:
            log.error("Zahlung %s:%s passt nicht: %s", event.provider, event.ref, exc)
            return web.json_response({"status": "mismatch"})
        return web.json_response({"status": "ok"})

    async def stripe_webhook(request: web.Request) -> web.Response:
        body = await request.read()
        try:
            event = payments.parse_webhook(body, request.headers)
        except InvalidWebhook as exc:
            log.warning("Ungültiger Webhook: %s", exc)
            return web.json_response({"error": "invalid signature"}, status=400)
        if event is None:
            return web.json_response({"status": "ignored"})
        # Bei Datenbankfehlern liefert aiohttp 500 und Stripe versucht es später erneut.
        return await process_event(event)

    async def dummy_page(request: web.Request) -> web.Response:
        ref = request.match_info["ref"]
        payment = await repo.get_payment(DummyProvider.name, ref)
        if payment is None:
            raise web.HTTPNotFound(text="Unbekannter Zahlungslink")
        if payment.status == "paid":
            body = "<p>Diese Testzahlung ist bereits abgeschlossen.</p>"
        else:
            body = (
                f"<p>{payment.credits} Antworten für "
                f"{html.escape(format_price(payment.amount_cents, payment.currency))}</p>"
                '<form method="post"><button>Testzahlung abschließen</button></form>'
            )
        page = (
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Testzahlung</title><body style='font-family:sans-serif;max-width:30rem;margin:3rem auto;"
            "padding:0 1rem;background:#111;color:#eee'><h1>Testzahlung</h1>"
            "<p><b>Kein echtes Geld.</b> Nur für Entwicklung.</p>" + body
        )
        return web.Response(text=page, content_type="text/html")

    async def dummy_pay(request: web.Request) -> web.Response:
        ref = request.match_info["ref"]
        payment = await repo.get_payment(DummyProvider.name, ref)
        if payment is None:
            raise web.HTTPNotFound(text="Unbekannter Zahlungslink")
        await process_event(
            PaymentEvent(DummyProvider.name, ref, payment.amount_cents, payment.currency)
        )
        raise web.HTTPSeeOther(location=str(request.rel_url))

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application(client_max_size=512 * 1024)
    app.router.add_post("/inbound", inbound)
    app.router.add_post("/payments/stripe/webhook", stripe_webhook)
    app.router.add_get("/health", health)
    if isinstance(payments, DummyProvider):
        log.warning("PAYMENT_PROVIDER=dummy: Testzahlungen ohne echtes Geld sind aktiv!")
        app.router.add_get("/payments/dummy/{ref}", dummy_page)
        app.router.add_post("/payments/dummy/{ref}", dummy_pay)
    return app
