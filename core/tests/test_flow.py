"""Ende-zu-Ende-Tests gegen echtes PostgreSQL, mit Fake-Sprachmodell und Fake-Telegram-Modul.

Benoetigt:  TEST_PG_DSN  z. B. postgresql://postgres@127.0.0.1:5433/botdb_test
Achtung: Die Tests leeren das Schema 'public' der Testdatenbank.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time

import asyncpg
import pytest
from aiohttp.test_utils import TestClient, TestServer

from botcore.config import BotConfig, Delivery, LLMSettings, Product, Settings
from botcore.llm import LLMRefusal
from botcore.orchestrator import Orchestrator
from botcore.server import build_app
from botdb import BotRepository

DSN = os.environ.get("TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_PG_DSN nicht gesetzt")


class FakeLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, list[dict]]] = []

    async def chat(self, system, messages):
        self.calls.append((system, messages))
        reply = self.replies.pop(0) if self.replies else "ok"
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeTransport:
    kind = "bot"

    def __init__(self):
        self.log: list[tuple] = []

    async def send_chunks(self, user_id, chunks, delays):
        await asyncio.sleep(0)
        self.log.append(("text", user_id, list(chunks)))

    async def send_file(self, user_id, file, caption=""):
        self.log.append(("file", user_id, file, caption))

    async def send_invoice(self, user_id, title, description, payload, amount):
        self.log.append(("invoice", user_id, payload, amount))

    async def refund_stars(self, user_id, charge_id):
        self.log.append(("refund", user_id, charge_id))

    def texts(self, user_id=None):
        return [line for e in self.log if e[0] == "text" and (user_id is None or e[1] == user_id) for line in e[2]]


PRODUCTS = (
    Product("guide", "Der Guide", "Ein PDF", "9 €", 50,
            deliver=(Delivery(file="guide.pdf", caption="Viel Spaß"), Delivery(text="Fragen? Schreib mir.")),
            thank_you="Danke!"),
    Product("pack", "Das Pack", "", "", 120, stripe_link="https://buy.stripe.com/x"),
)


def make_cfg(**overrides):
    base = dict(
        persona="Du bist Lena.",
        reply_delay_seconds=0.05,
        typing_speed=0,
        disclosure_mode="first_message",
        disclosure_text="Hinweis: KI",
        memory_extract_every=2,
        payment_method="stars",
        products=PRODUCTS,
    )
    base.update(overrides)
    return BotConfig(**base)


@pytest.fixture
async def repo():
    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    r = await BotRepository.connect(DSN)
    yield r
    await r.close()


async def make_orch(repo, llm, **cfg):
    orch = Orchestrator(make_cfg(**cfg), repo, llm, FakeTransport(), facts_llm=FakeLLM('{"facts": ["mag Katzen"]}'))
    await orch.sync_products()
    return orch


async def settle(orch, rounds=60):
    """Wartet, bis alle Hintergrund-Tasks des Orchestrators fertig sind."""
    for _ in range(rounds):
        await asyncio.sleep(0.02)
        if not orch._tasks:
            return
    raise AssertionError("Hintergrund-Tasks laufen noch")


# ---------------------------------------------------------------- Chat


async def test_antwort_mit_hinweis_nur_beim_ersten_mal(repo):
    llm = FakeLLM("Hey du! Wie geht's?", "Schön zu hören.")
    orch = await make_orch(repo, llm)
    await orch.handle_inbound(1, "hallo", "maxi", "Max")
    await settle(orch)
    assert orch.transport.texts(1) == ["Hinweis: KI", "Hey du!", "Wie geht's?"]

    await orch.handle_inbound(1, "gut", "maxi", "Max")
    await settle(orch)
    assert orch.transport.texts(1)[-1] == "Schön zu hören"
    assert "Hinweis: KI" not in orch.transport.texts(1)[3:]
    # Verlauf ging ans Modell, Hinweis wurde nicht als Teil der Antwort gespeichert
    assert llm.calls[1][1] == [
        {"role": "user", "content": "hallo"},
        {"role": "assistant", "content": "Hey du! Wie geht's?"},
        {"role": "user", "content": "gut"},
    ]


async def test_kein_hinweis_wenn_abgeschaltet(repo):
    orch = await make_orch(repo, FakeLLM("Hi."), disclosure_mode="off")
    await orch.handle_inbound(1, "hallo", None, None)
    await settle(orch)
    assert orch.transport.texts(1) == ["Hi"]


async def test_mehrere_nachrichten_eine_antwort(repo):
    llm = FakeLLM("Alles klar.")
    orch = await make_orch(repo, llm, reply_delay_seconds=0.2)
    for text in ("hey", "bist du da", "?"):
        await orch.handle_inbound(1, text, None, None)
        await asyncio.sleep(0.05)
    await settle(orch)
    assert len(llm.calls) == 1
    assert [m["content"] for m in llm.calls[0][1]] == ["hey", "bist du da", "?"]


async def test_start_nachricht(repo):
    llm = FakeLLM()
    orch = await make_orch(repo, llm, start_message="Willkommen!")
    await orch.handle_inbound(1, "/start", None, None)
    await settle(orch)
    assert orch.transport.texts(1) == ["Willkommen!", "Hinweis: KI"]
    assert llm.calls == []


async def test_ki_abgeschaltet_antwortet_nicht(repo):
    llm = FakeLLM("sollte nicht kommen")
    orch = await make_orch(repo, llm)
    await repo.upsert_user(1, None, None)
    await repo.set_ai_enabled(1, False)
    await orch.handle_inbound(1, "hallo", None, None)
    await settle(orch)
    assert llm.calls == [] and orch.transport.log == []
    assert [m.content for m in await repo.recent_messages(1)] == ["hallo"]  # trotzdem gespeichert


async def test_ablehnung_des_modells_sendet_nichts(repo):
    orch = await make_orch(repo, FakeLLM(LLMRefusal("nein")))
    await orch.handle_inbound(1, "hallo", None, None)
    await settle(orch)
    assert orch.transport.log == []


async def test_fakten_werden_gespeichert(repo):
    orch = await make_orch(repo, FakeLLM("a", "b"))
    await orch.handle_inbound(1, "ich hab zwei Katzen", None, None)
    await settle(orch)
    await orch.handle_inbound(1, "die heißen Tom und Tim", None, None)
    await settle(orch)
    ctx = await repo.get_user_context(1)
    assert [m.fact for m in ctx.memories] == ["mag Katzen"]
    await orch.handle_inbound(1, "und du?", None, None)
    await settle(orch)
    assert "- mag Katzen" in orch.llm.calls[-1][0]  # Fakten fließen in den Systemprompt ein


# ---------------------------------------------------------------- Angebote und Kauf


async def test_angebot_per_stars_mit_sperrzeit(repo):
    llm = FakeLLM("Da hätte ich was.\n[[OFFER:guide]]", "Nochmal: [[OFFER:guide]]", "Und [[OFFER:gibtsnicht]]")
    orch = await make_orch(repo, llm)
    for text in ("tipps?", "und?", "noch was?"):
        await orch.handle_inbound(1, text, None, None)
        await settle(orch)
    invoices = [e for e in orch.transport.log if e[0] == "invoice"]
    assert invoices == [("invoice", 1, "guide", 50)]
    assert all("OFFER" not in t for t in orch.transport.texts())
    assert "(wurde kürzlich angeboten" in llm.calls[1][0]


async def test_angebot_per_stripe_link(repo):
    orch = await make_orch(repo, FakeLLM("Schau mal [[OFFER:pack]]"), payment_method="stripe",
                           products=(PRODUCTS[1],), offer_text="{title}: {link}")
    await orch.handle_inbound(7, "hi", None, None)
    await settle(orch)
    assert orch.transport.texts(7)[-1] == "Das Pack: https://buy.stripe.com/x?client_reference_id=u7-pack"


async def test_kauf_liefert_genau_einmal_aus(repo):
    orch = await make_orch(repo, FakeLLM())
    await repo.upsert_user(1, None, None)
    assert await orch.complete_purchase(1, "guide", "ch_1", "stars", 50, "XTR") is True
    assert await orch.complete_purchase(1, "guide", "ch_1", "stars", 50, "XTR") is False
    await settle(orch)
    assert orch.transport.log == [
        ("file", 1, "guide.pdf", "Viel Spaß"),
        ("text", 1, ["Fragen? Schreib mir."]),
        ("text", 1, ["Danke!"]),
    ]
    ctx = await repo.get_user_context(1)
    assert [p.product_id for p in ctx.purchases] == ["guide"]
    assert "Bereits gekauft" in __import__("botcore.prompt", fromlist=["x"]).build_system_prompt(orch.cfg, ctx)


async def test_kauf_unbekannter_user_oder_produkt(repo):
    orch = await make_orch(repo, FakeLLM())
    assert await orch.complete_purchase(99, "guide", "ch_x", "stars") is False
    await repo.upsert_user(1, None, None)
    assert await orch.complete_purchase(1, "fehlt", "ch_y", "stars") is False


# ---------------------------------------------------------------- HTTP


SETTINGS = Settings(
    llm=LLMSettings("anthropic", "k", "m"),
    database_url="", redis_url=None, transport="bot", transport_url="",
    internal_token="intern", admin_token="admin", payment_webhook_token="hook",
    stripe_webhook_secret="whsec_test", config_file="",
)


@pytest.fixture
async def http(repo):
    orch = await make_orch(repo, FakeLLM("Hallo!", "Hallo!"))
    client = TestClient(TestServer(build_app(orch, SETTINGS)))
    await client.start_server()
    yield client, orch
    await client.close()
    await orch.shutdown()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_http_inbound_beide_formate(http):
    client, orch = http
    r = await client.post("/inbound", json={"user_id": 1, "text": "hi"})
    assert r.status == 401
    r = await client.post("/inbound", json={"user_id": 1, "text": "hi", "username": "a"}, headers=bearer("intern"))
    assert r.status == 200
    r = await client.post("/inbound", json={"user_id": 2, "message_text": "hi"}, headers=bearer("intern"))
    assert r.status == 200
    r = await client.post("/inbound", json={"user_id": 2}, headers=bearer("intern"))
    assert r.status == 400
    await settle(orch)
    assert orch.transport.texts(1)[-1] == "Hallo!" and orch.transport.texts(2)[-1] == "Hallo!"


async def test_http_stars_zahlung(http):
    client, orch = http
    await orch.repo.upsert_user(1, None, None)
    event = {"event": "successful_payment", "user_id": 1, "currency": "XTR", "total_amount": 50,
             "payload": "guide", "charge_id": "ch_9"}
    r = await client.post("/payment", json=event, headers=bearer("intern"))
    assert r.status == 200
    await settle(orch)
    assert orch.transport.log[0] == ("file", 1, "guide.pdf", "Viel Spaß")


def stripe_request(session):
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": session}}).encode()
    t = int(time.time())
    sig = hmac.new(b"whsec_test", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"Stripe-Signature": f"t={t},v1={sig}", "Content-Type": "application/json"}


async def test_http_stripe_webhook(http):
    client, orch = http
    await orch.repo.upsert_user(5, None, None)
    body, headers = stripe_request({"id": "cs_1", "payment_status": "paid", "client_reference_id": "u5-guide",
                                    "amount_total": 999, "currency": "eur"})
    r = await client.post("/webhooks/stripe", data=body, headers=headers)
    assert r.status == 200
    r = await client.post("/webhooks/stripe", data=body, headers={**headers, "Stripe-Signature": "t=1,v1=x"})
    assert r.status == 400
    await settle(orch)
    assert (await orch.repo.get_payment("cs_1"))["provider"] == "stripe"
    assert ("file", 5, "guide.pdf", "Viel Spaß") in orch.transport.log

    body, headers = stripe_request({"id": "cs_2", "payment_status": "unpaid", "client_reference_id": "u5-guide"})
    assert (await (await client.post("/webhooks/stripe", data=body, headers=headers)).json())["ignored"]


async def test_http_allgemeiner_webhook_und_admin(http):
    client, orch = http
    await orch.repo.upsert_user(3, None, None)
    r = await client.post("/webhooks/payment", json={"user_id": 3, "product_id": "guide"}, headers=bearer("falsch"))
    assert r.status == 401
    r = await client.post("/webhooks/payment", json={"user_id": 3, "product_id": "guide", "payment_id": "pp_1"},
                          headers=bearer("hook"))
    assert (await r.json())["new"] is True
    r = await client.post("/webhooks/payment", json={"user_id": 3, "product_id": "nix"}, headers=bearer("hook"))
    assert r.status == 400
    r = await client.post("/webhooks/payment", json={"ref": "u3-pack", "payment_id": "pp_2"}, headers=bearer("hook"))
    assert (await r.json())["new"] is True
    r = await client.post("/webhooks/payment", json={"ref": "kaputt"}, headers=bearer("hook"))
    assert r.status == 400

    r = await client.post("/admin/ai", json={"user_id": 3, "enabled": False}, headers=bearer("admin"))
    assert r.status == 200 and not (await orch.repo.get_user_context(3)).user.ai_enabled
    r = await client.post("/admin/send", json={"user_id": 3, "text": "Hier ist ein Mensch."}, headers=bearer("admin"))
    assert r.status == 200
    r = await client.post("/admin/grant", json={"user_id": 3, "product_id": "pack"}, headers=bearer("admin"))
    assert (await r.json())["new"] is True  # neue Zahlungs-id, auch wenn schon gekauft
    r = await client.post("/admin/refund", json={"payment_id": "pp_1"}, headers=bearer("admin"))
    assert "zurückbuchen" in (await r.json())["note"]
    r = await client.post("/admin/grant", json={"user_id": 3, "product_id": "pack"}, headers=bearer("hook"))
    assert r.status == 401
    await settle(orch)
    assert "Hier ist ein Mensch." in orch.transport.texts(3)


async def test_http_stars_erstattung(http):
    client, orch = http
    await orch.repo.upsert_user(1, None, None)
    await orch.complete_purchase(1, "guide", "ch_r", "stars", 50, "XTR")
    r = await client.post("/admin/refund", json={"payment_id": "ch_r"}, headers=bearer("admin"))
    assert (await r.json())["note"] == "Stars wurden erstattet."
    assert ("refund", 1, "ch_r") in orch.transport.log
    assert (await orch.repo.get_user_context(1)).purchases == []


async def test_http_admin_ohne_token_abgeschaltet(repo):
    orch = await make_orch(repo, FakeLLM())
    settings = Settings(**{**SETTINGS.__dict__, "admin_token": None, "stripe_webhook_secret": None})
    client = TestClient(TestServer(build_app(orch, settings)))
    await client.start_server()
    try:
        r = await client.post("/admin/grant", json={"user_id": 1, "product_id": "guide"}, headers=bearer(""))
        assert r.status == 404
        assert (await client.post("/webhooks/stripe", data=b"{}")).status == 404
        assert (await client.get("/health")).status == 200
    finally:
        await client.close()
