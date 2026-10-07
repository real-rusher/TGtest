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

from botdb import BotRepository, InvalidFact, ProductNotFound, UserNotFound

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
    await r.upsert_user(1, "meister", "Max")
    await r.upsert_product("guide", "Der Guide", 50, "Ein PDF", ["file_a", "file_b"])
    await r.upsert_product("pack", "Das Pack", 120)
    yield r
    await r.close()


async def test_unbekannter_user_liefert_none(repo):
    assert await repo.get_user_context(999) is None


async def test_leerer_kontext(repo):
    ctx = await repo.get_user_context(1)
    assert ctx.user.username == "meister"
    assert ctx.user.ai_enabled is True
    assert ctx.memories == [] and ctx.purchases == [] and ctx.open_offers == []


async def test_record_memory_und_kontext(repo):
    await repo.get_user_context(1)  # Cache befuellen
    res = await repo.record_memory(1, "  Mag   Warhammer 40k ", "Hobby")
    assert res.created and res.memory.fact == "Mag Warhammer 40k" and res.memory.category == "hobby"
    ctx = await repo.get_user_context(1)
    assert [m.fact for m in ctx.memories] == ["Mag Warhammer 40k"]  # Cache wurde invalidiert


async def test_memory_duplikat(repo):
    a = await repo.record_memory(1, "Wohnt in Aachen")
    b = await repo.record_memory(1, "wohnt in aachen ")
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


async def test_angebot_dann_kauf(repo):
    assert await repo.record_offer(1, "guide") is True
    ctx = await repo.get_user_context(1)
    assert [o.product_id for o in ctx.open_offers] == ["guide"] and ctx.purchases == []

    res = await repo.record_purchase(1, "guide")
    assert res.newly_purchased and res.entry.status == "purchased" and res.entry.title == "Der Guide"
    ctx = await repo.get_user_context(1)
    assert ctx.purchased_product_ids == {"guide"} and ctx.open_offers == []


async def test_kauf_ohne_angebot_und_idempotenz(repo):
    first = await repo.record_purchase(1, "pack")
    second = await repo.record_purchase(1, "pack")
    assert first.newly_purchased and not second.newly_purchased
    assert first.entry.id == second.entry.id
    # Angebot nach Kauf stuft nicht zurueck
    assert await repo.record_offer(1, "pack") is False
    ctx = await repo.get_user_context(1)
    assert ctx.purchased_product_ids == {"pack"} and ctx.open_offers == []


async def test_kauf_fehler(repo):
    with pytest.raises(ProductNotFound):
        await repo.record_purchase(1, "gibts_nicht")
    with pytest.raises(UserNotFound):
        await repo.record_purchase(77, "guide")


async def test_parallele_kaeufe_nur_einer_neu(repo):
    results = await asyncio.gather(*[repo.record_purchase(1, "guide") for _ in range(8)])
    assert sum(r.newly_purchased for r in results) == 1
    assert len({r.entry.id for r in results}) == 1


async def test_ai_toggle_und_namensaenderung(repo):
    await repo.get_user_context(1)
    await repo.set_ai_enabled(1, False)
    assert (await repo.get_user_context(1)).user.ai_enabled is False
    await repo.upsert_user(1, "meister_neu", "Max")
    assert (await repo.get_user_context(1)).user.username == "meister_neu"
    # upsert ueberschreibt ai_enabled nicht
    assert (await repo.get_user_context(1)).user.ai_enabled is False
    with pytest.raises(UserNotFound):
        await repo.set_ai_enabled(555, True)


async def test_jsonb_file_ids(repo):
    p = await repo.get_product("guide")
    assert p["file_ids"] == ["file_a", "file_b"]
    assert [x["product_id"] for x in await repo.list_active_products()] == ["pack", "guide"]


async def test_cache_roundtrip_identisch(repo):
    await repo.record_memory(1, "Ümlaute & Emoji 🚀 bleiben erhalten")
    await repo.record_purchase(1, "guide")
    await repo.record_offer(1, "pack")
    fresh = await repo.get_user_context(1)  # aus DB, landet im Cache
    cached = await repo.get_user_context(1)  # aus Cache (falls Redis aktiv)
    assert fresh == cached


async def test_schema_mehrfach_anwendbar(repo):
    await repo.init_schema()
    await repo.init_schema()
    assert (await repo.get_user_context(1)) is not None


# ---------------------------------------------------------------- Erweiterungen fuer den Bot-Core


async def test_produkt_ohne_stars_preis(repo):
    await repo.upsert_product("link_only", "Nur per Link", None, "Zahlung extern")
    assert (await repo.get_product("link_only"))["price_stars"] is None
    await repo.record_purchase(1, "link_only")
    ctx = await repo.get_user_context(1)
    assert ctx.purchases[0].price_stars is None
    assert ctx == await repo.get_user_context(1)  # Cache-Roundtrip mit None


async def test_deactivate_products_except(repo):
    assert await repo.deactivate_products_except(["guide"]) == 1
    assert [p["product_id"] for p in await repo.list_active_products()] == ["guide"]
    assert (await repo.get_product("pack"))["is_active"] is False


async def test_try_offer_mit_sperrzeit(repo):
    assert await repo.try_offer(1, "guide", cooldown_seconds=3600) is True
    assert await repo.try_offer(1, "guide", cooldown_seconds=3600) is False  # zu frueh
    assert await repo.try_offer(1, "guide", cooldown_seconds=0) is True  # Sperrzeit vorbei
    await repo.record_purchase(1, "guide")
    assert await repo.try_offer(1, "guide", cooldown_seconds=0) is False  # gekauft: nie wieder
    with pytest.raises(ProductNotFound):
        await repo.try_offer(1, "gibtsnicht", 0)


async def test_chatverlauf(repo):
    ids = [await repo.add_message(1, role, text) for role, text in
           [("user", "hi"), ("assistant", "hey"), ("user", "wie gehts"), ("user", "?")]]
    assert ids == sorted(ids)
    last = await repo.recent_messages(1, limit=3)
    assert [m.content for m in last] == ["hey", "wie gehts", "?"]
    with pytest.raises(ValueError):
        await repo.add_message(1, "system", "x")
    with pytest.raises(UserNotFound):
        await repo.add_message(404, "user", "x")


async def test_mark_disclosed_nur_einmal(repo):
    assert await repo.mark_disclosed(1) is True
    assert await repo.mark_disclosed(1) is False


async def test_zahlung_idempotent_und_erstattung(repo):
    assert await repo.record_payment("ch_1", "stars", 1, "guide", 50, "XTR") is True
    assert await repo.record_payment("ch_1", "stars", 1, "guide", 50, "XTR") is False
    await repo.record_purchase(1, "guide")
    assert (await repo.get_payment("ch_1"))["amount"] == 50
    assert await repo.mark_refunded("ch_1") is True
    assert await repo.mark_refunded("ch_1") is False
    ctx = await repo.get_user_context(1)
    assert ctx.purchases == [] and ctx.open_offers[0].product_id == "guide"
