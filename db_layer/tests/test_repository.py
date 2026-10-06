"""Integrationstests gegen echtes PostgreSQL und Redis.

Benoetigt:
    TEST_PG_DSN     z. B. postgresql://postgres@127.0.0.1:5433/botdb
    TEST_REDIS_URL  z. B. redis://127.0.0.1:6380/15   (optional)
Achtung: Die Tests leeren das Schema 'public' der Testdatenbank.
"""

import asyncio
import os

import asyncpg
import pytest
import pytest_asyncio
from redis.asyncio import Redis

from botdb import (
    BotRepository,
    InvalidFact,
    InvalidMessage,
    PaymentMismatch,
    PaymentNotFound,
    ProductNotFound,
    UserNotFound,
)

DSN = os.environ.get("TEST_PG_DSN")
REDIS_URL = os.environ.get("TEST_REDIS_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="TEST_PG_DSN nicht gesetzt")


@pytest_asyncio.fixture(params=["mit_redis", "ohne_redis"])
async def repo(request):
    if request.param == "mit_redis" and not REDIS_URL:
        pytest.skip("TEST_REDIS_URL nicht gesetzt")
    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    url = REDIS_URL if request.param == "mit_redis" else None
    if url:
        r = Redis.from_url(url)
        await r.flushdb()
        await r.aclose()
    r = await BotRepository.connect(DSN, url, cache_ttl=60)
    await r.upsert_user(1, "nutzer", "Max", initial_credits=3)
    await r.upsert_product("small", "Kleines Paket", 299, 50, description="50 Antworten")
    await r.upsert_product("big", "Großes Paket", 999, 250)
    yield r
    await r.close()


# ---------------------------------------------------------------- User


async def test_unbekannter_user_liefert_none(repo):
    assert await repo.get_user_context(999) is None
    assert await repo.get_user(999) is None


async def test_leerer_kontext(repo):
    ctx = await repo.get_user_context(1)
    assert ctx.user.username == "nutzer"
    assert ctx.user.ai_enabled is True
    assert ctx.user.credits == 3 and ctx.user.disclosed_at is None
    assert ctx.memories == [] and ctx.purchases == [] and ctx.last_offer_at is None


async def test_upsert_gratis_guthaben_nur_beim_anlegen(repo):
    user, created = await repo.upsert_user(2, None, "Neu", initial_credits=5)
    assert created and user.credits == 5
    user, created = await repo.upsert_user(2, "neu", "Neu", initial_credits=5)
    assert not created and user.credits == 5 and user.username == "neu"
    with pytest.raises(ValueError):
        await repo.upsert_user(3, None, None, initial_credits=-1)


async def test_ai_toggle_und_namensaenderung(repo):
    await repo.get_user_context(1)
    await repo.set_ai_enabled(1, False)
    assert (await repo.get_user_context(1)).user.ai_enabled is False
    await repo.upsert_user(1, "nutzer_neu", "Max")
    assert (await repo.get_user_context(1)).user.username == "nutzer_neu"
    assert (await repo.get_user_context(1)).user.ai_enabled is False
    with pytest.raises(UserNotFound):
        await repo.set_ai_enabled(555, True)


async def test_ki_hinweis_nur_einmal(repo):
    await repo.get_user_context(1)
    assert await repo.mark_disclosed(1) is True
    assert await repo.mark_disclosed(1) is False
    assert (await repo.get_user_context(1)).user.disclosed_at is not None


# ---------------------------------------------------------------- Gedaechtnis


async def test_record_memory_und_kontext(repo):
    await repo.get_user_context(1)  # Cache befuellen
    res = await repo.record_memory(1, "  Mag   Brettspiele ", "Hobby")
    assert res.created and res.memory.fact == "Mag Brettspiele" and res.memory.category == "hobby"
    ctx = await repo.get_user_context(1)
    assert [m.fact for m in ctx.memories] == ["Mag Brettspiele"]  # Cache wurde invalidiert


async def test_memory_duplikat(repo):
    a = await repo.record_memory(1, "Wohnt in Köln")
    b = await repo.record_memory(1, "wohnt in köln ")
    assert a.created and not b.created and a.memory.id == b.memory.id
    assert len((await repo.get_user_context(1)).memories) == 1


async def test_memory_fehler(repo):
    with pytest.raises(InvalidFact):
        await repo.record_memory(1, "   ")
    with pytest.raises(InvalidFact):
        await repo.record_memory(1, "x" * 2001)
    with pytest.raises(UserNotFound):
        await repo.record_memory(42, "gibt es nicht")


async def test_memory_reihenfolge_und_limit(repo):
    for i in range(5):
        await repo.record_memory(1, f"Fakt {i}")
    ctx = await repo.get_user_context(1, memory_limit=3)
    assert [m.fact for m in ctx.memories] == ["Fakt 4", "Fakt 3", "Fakt 2"]


# ---------------------------------------------------------------- Chatverlauf


async def test_chatverlauf_reihenfolge_und_limit(repo):
    for i in range(6):
        await repo.add_message(1, "user" if i % 2 == 0 else "assistant", f"m{i}")
    recent = await repo.get_recent_messages(1, limit=4)
    assert [m.content for m in recent] == ["m2", "m3", "m4", "m5"]
    assert [m.role for m in recent] == ["user", "assistant", "user", "assistant"]


async def test_chatverlauf_fehler(repo):
    with pytest.raises(InvalidMessage):
        await repo.add_message(1, "system", "x")
    with pytest.raises(InvalidMessage):
        await repo.add_message(1, "user", "  ")
    with pytest.raises(UserNotFound):
        await repo.add_message(404, "user", "hallo")


# ---------------------------------------------------------------- Guthaben


async def test_guthaben_verbrauchen(repo):
    assert await repo.consume_credit(1) == 2
    assert await repo.consume_credit(1) == 1
    assert await repo.consume_credit(1) == 0
    assert await repo.consume_credit(1) is None
    assert await repo.add_credits(1, 10) == 10
    assert (await repo.get_user_context(1)).user.credits == 10
    with pytest.raises(UserNotFound):
        await repo.add_credits(404, 1)
    with pytest.raises(ValueError):
        await repo.add_credits(1, 0)


async def test_guthaben_parallel_nie_negativ(repo):
    results = await asyncio.gather(*[repo.consume_credit(1) for _ in range(10)])
    assert sorted(r for r in results if r is not None) == [0, 1, 2]
    assert results.count(None) == 7


# ---------------------------------------------------------------- Katalog und Angebote


async def test_katalog(repo):
    p = await repo.get_product("small")
    assert (p.price_cents, p.currency, p.credits, p.description) == (299, "EUR", 50, "50 Antworten")
    assert [x.product_id for x in await repo.list_active_products()] == ["small", "big"]
    await repo.upsert_product("big", "Großes Paket", 999, 250, is_active=False)
    assert [x.product_id for x in await repo.list_active_products()] == ["small"]
    assert await repo.get_product("nope") is None


async def test_angebote(repo):
    assert await repo.last_offer_at(1) is None
    await repo.get_user_context(1)
    await repo.record_offer(1, "small")
    first = await repo.last_offer_at(1)
    assert first is not None
    assert (await repo.get_user_context(1)).last_offer_at == first
    with pytest.raises(ProductNotFound):
        await repo.record_offer(1, "nope")
    with pytest.raises(UserNotFound):
        await repo.record_offer(77, "small")


# ---------------------------------------------------------------- Zahlungen


async def test_zahlung_verbuchen(repo):
    product = await repo.get_product("small")
    pending = await repo.create_payment("stripe", "cs_1", 1, product)
    assert pending.status == "pending" and pending.credits == 50 and pending.amount_cents == 299

    await repo.get_user_context(1)
    res = await repo.complete_payment("stripe", "cs_1", 299, "eur")
    assert res.newly_paid and res.payment.status == "paid" and res.new_balance == 53

    again = await repo.complete_payment("stripe", "cs_1", 299, "EUR")
    assert not again.newly_paid and again.new_balance == 53

    ctx = await repo.get_user_context(1)
    assert ctx.user.credits == 53
    assert [(p.product_id, p.title, p.credits) for p in ctx.purchases] == [("small", "Kleines Paket", 50)]


async def test_zahlung_fehler(repo):
    product = await repo.get_product("big")
    await repo.create_payment("stripe", "cs_2", 1, product)
    with pytest.raises(PaymentMismatch):
        await repo.complete_payment("stripe", "cs_2", 1, "EUR")
    with pytest.raises(PaymentMismatch):
        await repo.complete_payment("stripe", "cs_2", 999, "USD")
    with pytest.raises(PaymentNotFound):
        await repo.complete_payment("stripe", "cs_unbekannt", 999, "EUR")
    with pytest.raises(asyncpg.UniqueViolationError):
        await repo.create_payment("stripe", "cs_2", 1, product)
    assert (await repo.get_payment("stripe", "cs_2")).status == "pending"


async def test_parallele_webhooks_nur_einmal_gutgeschrieben(repo):
    product = await repo.get_product("small")
    await repo.create_payment("stripe", "cs_3", 1, product)
    results = await asyncio.gather(
        *[repo.complete_payment("stripe", "cs_3", 299, "EUR") for _ in range(8)]
    )
    assert sum(r.newly_paid for r in results) == 1
    assert (await repo.get_user(1)).credits == 53


# ---------------------------------------------------------------- Loeschen


async def test_user_loeschen_anonymisiert_zahlungen(repo):
    await repo.record_memory(1, "Wohnt in Köln")
    await repo.add_message(1, "user", "hallo")
    await repo.record_offer(1, "small")
    product = await repo.get_product("small")
    await repo.create_payment("stripe", "cs_4", 1, product)
    await repo.complete_payment("stripe", "cs_4", 299, "EUR")

    await repo.get_user_context(1)
    assert await repo.delete_user(1) is True
    assert await repo.get_user_context(1) is None
    assert await repo.get_recent_messages(1) == []
    payment = await repo.get_payment("stripe", "cs_4")
    assert payment.user_id is None and payment.status == "paid"
    assert await repo.delete_user(1) is False


async def test_zahlung_nach_loeschen(repo):
    product = await repo.get_product("small")
    await repo.create_payment("stripe", "cs_5", 1, product)
    await repo.delete_user(1)
    res = await repo.complete_payment("stripe", "cs_5", 299, "EUR")
    assert res.newly_paid and res.new_balance is None


# ---------------------------------------------------------------- Cache und Schema


async def test_cache_roundtrip_identisch(repo):
    await repo.record_memory(1, "Ümlaute & Emoji 🚀 bleiben erhalten")
    await repo.record_offer(1, "big")
    await repo.mark_disclosed(1)
    product = await repo.get_product("small")
    await repo.create_payment("stripe", "cs_6", 1, product)
    await repo.complete_payment("stripe", "cs_6", 299, "EUR")
    fresh = await repo.get_user_context(1)  # aus DB, landet im Cache
    cached = await repo.get_user_context(1)  # aus Cache (falls Redis aktiv)
    assert fresh == cached


async def test_schema_mehrfach_anwendbar(repo):
    await repo.init_schema()
    await repo.init_schema()
    assert (await repo.get_user_context(1)) is not None
