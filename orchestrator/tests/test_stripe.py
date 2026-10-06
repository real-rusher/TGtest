import json
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from botdb import Product
from orchestrator.payments import InvalidWebhook, format_price
from orchestrator.payments.stripe import StripeError, StripeProvider, sign, verify_signature

SECRET = "whsec_test"
PRODUCT = Product("small", "Kleines Paket", "50 Antworten", 299, "EUR", 50, True)


def session_event(event_type="checkout.session.completed", payment_status="paid", **obj):
    data = {"id": "cs_123", "amount_total": 299, "currency": "eur", "payment_status": payment_status}
    data.update(obj)
    return json.dumps({"type": event_type, "data": {"object": data}}).encode()


def provider(api_base="https://api.stripe.com"):
    return StripeProvider("sk_test", SECRET, "https://t.me/example", "https://example.test/cancel", api_base=api_base)


def test_signatur_gueltig_und_ungueltig():
    body = b'{"a":1}'
    verify_signature(body, sign(body, SECRET), SECRET)
    with pytest.raises(InvalidWebhook):
        verify_signature(body, sign(body, "falsch"), SECRET)
    with pytest.raises(InvalidWebhook):
        verify_signature(body + b" ", sign(body, SECRET), SECRET)
    with pytest.raises(InvalidWebhook):
        verify_signature(body, sign(body, SECRET, timestamp=int(time.time()) - 1000), SECRET)
    with pytest.raises(InvalidWebhook):
        verify_signature(body, None, SECRET)
    with pytest.raises(InvalidWebhook):
        verify_signature(body, "t=abc,v1=00", SECRET)


def test_mehrere_v1_signaturen_beim_schluesselwechsel():
    body = b"{}"
    ts = int(time.time())
    good = sign(body, SECRET, ts).split("v1=")[1]
    verify_signature(body, f"t={ts},v1=deadbeef,v1={good}", SECRET)


def test_webhook_bezahlt():
    body = session_event()
    event = provider().parse_webhook(body, {"Stripe-Signature": sign(body, SECRET)})
    assert (event.provider, event.ref, event.amount_cents, event.currency) == ("stripe", "cs_123", 299, "EUR")


def test_webhook_verzoegerte_zahlung():
    p = provider()
    body = session_event(payment_status="unpaid")
    assert p.parse_webhook(body, {"Stripe-Signature": sign(body, SECRET)}) is None
    body = session_event("checkout.session.async_payment_succeeded")
    assert p.parse_webhook(body, {"Stripe-Signature": sign(body, SECRET)}).ref == "cs_123"


def test_webhook_andere_events_und_kaputte_daten():
    p = provider()
    body = session_event("payment_intent.created")
    assert p.parse_webhook(body, {"Stripe-Signature": sign(body, SECRET)}) is None
    body = b"kein json"
    with pytest.raises(InvalidWebhook):
        p.parse_webhook(body, {"Stripe-Signature": sign(body, SECRET)})
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"payment_status": "paid"}}}).encode()
    with pytest.raises(InvalidWebhook):
        p.parse_webhook(body, {"Stripe-Signature": sign(body, SECRET)})


async def test_checkout_anlegen_gegen_fake_stripe():
    seen = {}

    async def create(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["form"] = dict(await request.post())
        return web.json_response({"id": "cs_new", "url": "https://checkout.stripe.test/cs_new"})

    async def broken(request):
        return web.json_response({"error": {"message": "Invalid currency"}}, status=400)

    app = web.Application()
    app.router.add_post("/v1/checkout/sessions", create)
    server = TestServer(app)
    await server.start_server()
    try:
        p = provider(str(server.make_url("")))
        link = await p.create_checkout(42, PRODUCT)
        assert (link.provider, link.ref, link.url) == ("stripe", "cs_new", "https://checkout.stripe.test/cs_new")
        form = seen["form"]
        assert seen["auth"] == "Bearer sk_test"
        assert form["mode"] == "payment" and form["client_reference_id"] == "42"
        assert form["metadata[product_id]"] == "small"
        assert form["line_items[0][price_data][currency]"] == "eur"
        assert form["line_items[0][price_data][unit_amount]"] == "299"
        assert form["line_items[0][price_data][product_data][name]"] == "Kleines Paket"
        assert form["cancel_url"] == "https://example.test/cancel"
        await p.close()
    finally:
        await server.close()

    app = web.Application()
    app.router.add_post("/v1/checkout/sessions", broken)
    server = TestServer(app)
    await server.start_server()
    try:
        p = provider(str(server.make_url("")))
        with pytest.raises(StripeError, match="Invalid currency"):
            await p.create_checkout(42, PRODUCT)
        await p.close()
    finally:
        await server.close()


def test_preisformat():
    assert format_price(299, "EUR") == "2,99 €"
    assert format_price(1000, "usd") == "10,00 $"
    assert format_price(50, "SEK") == "0,50 SEK"
