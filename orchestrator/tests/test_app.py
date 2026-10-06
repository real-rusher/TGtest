"""HTTP-Schnittstelle gegen echtes PostgreSQL."""

import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from botdb import Product
from orchestrator.app import build_app
from orchestrator.conversation import ConversationManager
from orchestrator.payments.stripe import StripeProvider, sign

from conftest import dumps, event, make_config, needs_db

pytestmark = needs_db
SECRET = "whsec_test"


async def make_client(manager, payments, repo, cfg):
    client = TestClient(TestServer(build_app(manager, payments, repo, cfg)))
    await client.start_server()
    return client


@pytest_asyncio.fixture
async def dummy_client(manager, parts, repo):
    client = await make_client(manager, parts.payments, repo, make_config())
    yield client
    await client.close()


async def test_inbound_auth_und_validierung(dummy_client, manager, parts):
    auth = {"Authorization": "Bearer in-secret"}
    assert (await dummy_client.post("/inbound", json=event())).status == 401
    assert (await dummy_client.post("/inbound", json=event(), headers={"Authorization": "Bearer x"})).status == 401
    assert (await dummy_client.post("/inbound", data=b"{kaputt", headers=auth)).status == 400
    assert (await dummy_client.post("/inbound", json=event(message_text=""), headers=auth)).status == 400

    resp = await dummy_client.post("/inbound", json=event(), headers=auth)
    assert resp.status == 202
    await manager.drain()
    assert len(parts.gateway.sent) == 2  # Hinweis + Antwort

    assert (await dummy_client.get("/health")).status == 200


async def test_dummy_zahlung_ueber_testseite(dummy_client, manager, parts, repo):
    product = await repo.get_product("small")
    link = await parts.payments.create_checkout(42, product)
    await repo.upsert_user(42, None, "Max")
    await repo.create_payment(link.provider, link.ref, 42, product)

    path = link.url.replace("https://example.test", "")
    page = await (await dummy_client.get(path)).text()
    assert "Testzahlung abschließen" in page and "2,99 €" in page

    resp = await dummy_client.post(path, allow_redirects=False)
    assert resp.status == 303
    await manager.drain()
    assert (await repo.get_user(42)).credits == 50
    assert "bereits abgeschlossen" in await (await dummy_client.get(path)).text()
    assert (await dummy_client.get("/payments/dummy/gibtsnicht")).status == 404


async def test_stripe_webhook(repo, parts):
    stripe = StripeProvider("sk", SECRET, "https://t.me/x")
    cfg = make_config(payment_provider="stripe")
    manager = ConversationManager(repo, parts.gateway, parts.generator, parts.facts, stripe, cfg)
    client = await make_client(manager, stripe, repo, cfg)
    try:
        await repo.upsert_user(42, None, "Max")
        product: Product = await repo.get_product("small")
        await repo.create_payment("stripe", "cs_1", 42, product)

        def body(ref="cs_1", amount=299):
            return dumps({
                "type": "checkout.session.completed",
                "data": {"object": {"id": ref, "amount_total": amount, "currency": "eur", "payment_status": "paid"}},
            })

        b = body()
        resp = await client.post("/payments/stripe/webhook", data=b, headers={"Stripe-Signature": sign(b, "falsch")})
        assert resp.status == 400

        resp = await client.post("/payments/stripe/webhook", data=b, headers={"Stripe-Signature": sign(b, SECRET)})
        assert resp.status == 200 and (await resp.json())["status"] == "ok"
        await manager.drain()
        assert (await repo.get_user(42)).credits == 50
        assert parts.gateway.texts()[-1].startswith("Danke")

        b = body("cs_fremd")
        resp = await client.post("/payments/stripe/webhook", data=b, headers={"Stripe-Signature": sign(b, SECRET)})
        assert (await resp.json())["status"] == "ignored"

        await repo.create_payment("stripe", "cs_2", 42, product)
        b = body("cs_2", amount=1)
        resp = await client.post("/payments/stripe/webhook", data=b, headers={"Stripe-Signature": sign(b, SECRET)})
        assert (await resp.json())["status"] == "mismatch"
        assert (await repo.get_user(42)).credits == 50

        # Dummy-Routen sind bei Stripe nicht vorhanden
        assert (await client.get("/payments/dummy/abc")).status == 404
    finally:
        await client.close()
        await manager.shutdown()
