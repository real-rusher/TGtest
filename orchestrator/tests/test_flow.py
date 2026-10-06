"""Gesprächsablauf gegen echtes PostgreSQL, mit Fake-Gateway, -Modell und -Zahlung."""

import asyncio

from orchestrator.conversation import ConversationManager, Inbound, parse_command
from orchestrator.llm import GenerationError
from orchestrator.payments import PaymentEvent

from conftest import TEST_DISCLOSURE, event, make_config, needs_db

pytestmark = needs_db


async def say(manager, text, user_id=42, drain=True):
    manager.enqueue(Inbound.from_payload(event(user_id, text)))
    if drain:
        await manager.drain()


def test_befehle_erkennen():
    assert parse_command("/stop") == ("stop", "")
    assert parse_command(" /DELETE confirm ") == ("delete", "confirm")
    assert parse_command("/balance@irgendwas") == ("balance", "")
    assert parse_command("stop") is None
    assert parse_command("/stoppen") is None


async def test_erster_kontakt_hinweis_dann_antwort(manager, parts, repo):
    parts.generator.replies = ["Hey Max! Wie geht's dir?"]
    await say(manager, "hallo")

    assert parts.gateway.texts() == [TEST_DISCLOSURE, "Hey Max! | Wie geht's dir?"]
    user = await repo.get_user(42)
    assert user.credits == 1 and user.disclosed_at is not None
    history = await repo.get_recent_messages(42)
    assert [(m.role, m.content) for m in history] == [
        ("user", "hallo"),
        ("assistant", "Hey Max! Wie geht's dir?"),
    ]

    await say(manager, "gut und dir?")
    assert parts.gateway.texts()[2:] == ["alles klar"]  # kein zweiter Hinweis


async def test_debounce_ein_stapel_eine_antwort(manager, parts, repo):
    for text in ("hey", "bist du da?", "hallo??"):
        await say(manager, text, drain=False)
        await asyncio.sleep(0.01)
    await manager.drain()

    assert len(parts.generator.calls) == 1
    _, history = parts.generator.calls[0]
    assert [m.content for m in history] == ["hey", "bist du da?", "hallo??"]
    assert (await repo.get_user(42)).credits == 1


async def test_bezahlschranke_cooldown_und_zahlung(manager, parts, repo):
    await say(manager, "eins")
    await say(manager, "zwei")
    assert (await repo.get_user(42)).credits == 0

    await say(manager, "drei")
    paywall = parts.gateway.texts()[-1]
    assert "Kleines Paket" in paywall and "2,99 €" in paywall and "https://example.test/payments/dummy/" in paywall
    assert await repo.last_offer_at(42) is not None
    generated = len(parts.generator.calls)

    await say(manager, "vier")  # im Cooldown: still, keine KI-Antwort
    assert parts.gateway.texts()[-1] == paywall
    assert len(parts.generator.calls) == generated

    ref = paywall.rsplit("/", 1)[-1]
    parts.generator.replies = ["sorry, war kurz weg"]
    await manager.handle_payment(PaymentEvent("dummy", ref, 299, "EUR"))
    await manager.drain()

    assert parts.gateway.texts()[-2] == "Danke, die Zahlung ist angekommen! Dein Guthaben: 50 Antworten."
    assert parts.gateway.texts()[-1] == "sorry, war kurz weg"  # offene Nachricht wird beantwortet
    assert (await repo.get_user(42)).credits == 49

    # Doppelter Webhook: keine zweite Bestätigung, kein zweites Guthaben
    await manager.handle_payment(PaymentEvent("dummy", ref, 299, "EUR"))
    await manager.drain()
    assert (await repo.get_user(42)).credits == 49
    assert parts.gateway.texts()[-1] == "sorry, war kurz weg"


async def test_bezahlschranke_festes_paket(repo, parts):
    m = ConversationManager(
        repo, parts.gateway, parts.generator, parts.facts, parts.payments,
        make_config(free_credits=0, paywall_product_id="big"),
    )
    await say(m, "hi")
    assert "Großes Paket" in parts.gateway.texts()[-1]
    await m.shutdown()


async def test_ohne_pakete_kein_link(repo, parts):
    await repo.upsert_product("small", "Kleines Paket", 299, 50, is_active=False)
    await repo.upsert_product("big", "Großes Paket", 999, 250, is_active=False)
    m = ConversationManager(repo, parts.gateway, parts.generator, parts.facts, parts.payments, make_config(free_credits=0))
    await say(m, "hi")
    assert parts.gateway.texts() == [TEST_DISCLOSURE]
    await m.shutdown()


async def test_stop_start_balance(manager, parts, repo):
    await say(manager, "/stop")
    assert (await repo.get_user(42)).ai_enabled is False
    await say(manager, "hallo?")
    assert parts.generator.calls == []
    assert [m.content for m in await repo.get_recent_messages(42)] == ["hallo?"]  # trotzdem gespeichert

    await say(manager, "/balance")
    assert parts.gateway.texts()[-1] == "Dein Guthaben: 2 Antworten."
    await say(manager, "/start")
    await say(manager, "jetzt aber")
    assert len(parts.generator.calls) == 1


async def test_loeschen_mit_bestaetigung(manager, parts, repo):
    await say(manager, "hallo")
    await say(manager, "/delete")
    assert "/delete confirm" in parts.gateway.texts()[-1]
    assert await repo.get_user(42) is not None

    await say(manager, "/delete confirm")
    assert parts.gateway.texts()[-1] == "Erledigt, alle deine Daten sind gelöscht."
    assert await repo.get_user(42) is None
    assert await repo.get_recent_messages(42) == []


async def test_befehl_mitten_im_stapel_haelt_reihenfolge(manager, parts, repo):
    await say(manager, "erste", drain=False)
    await say(manager, "/balance", drain=False)
    await say(manager, "zweite", drain=False)
    await manager.drain()
    texts = parts.gateway.texts()
    assert texts[0] == TEST_DISCLOSURE
    assert texts[1:] == ["alles klar", "Dein Guthaben: 1 Antworten.", "alles klar"]


async def test_pausiert_guthaben_zurueck(manager, parts, repo):
    await say(manager, "hallo")  # Hinweis + Antwort
    parts.gateway.paused.add(42)
    await say(manager, "noch da?")
    user = await repo.get_user(42)
    assert user.credits == 1  # zurückgebucht
    assert [m.role for m in await repo.get_recent_messages(42)] == ["user", "assistant", "user"]


async def test_modellfehler_und_gatewayfehler_guthaben_zurueck(manager, parts, repo):
    parts.generator.replies = [GenerationError("weg")]
    await say(manager, "hallo")
    assert (await repo.get_user(42)).credits == 2

    parts.gateway.fail = True
    await say(manager, "nochmal")
    assert (await repo.get_user(42)).credits == 2


async def test_hinweis_scheitert_kein_verbrauch(manager, parts, repo):
    parts.gateway.paused.add(42)
    await say(manager, "hallo")
    user = await repo.get_user(42)
    assert user.disclosed_at is None and user.credits == 2
    parts.gateway.paused.clear()
    await say(manager, "hallo?")
    assert parts.gateway.texts()[0] == TEST_DISCLOSURE


async def test_fakten_alle_n_nachrichten(repo, parts):
    parts.facts.facts = ["wohnt in Köln", "fährt Motorrad"]
    m = ConversationManager(repo, parts.gateway, parts.generator, parts.facts, parts.payments, make_config(free_credits=10))
    await say(m, "eins")
    await say(m, "zwei")
    assert parts.facts.calls == []
    await say(m, "drei")
    await m.drain()
    assert len(parts.facts.calls) == 1
    assert parts.facts.calls[0][0] == {"role": "user", "content": "eins"}
    ctx = await repo.get_user_context(42)
    assert sorted(x.fact for x in ctx.memories) == ["fährt Motorrad", "wohnt in Köln"]
    await m.shutdown()


async def test_mehrere_user_parallel(manager, parts, repo):
    await asyncio.gather(say(manager, "a", user_id=1, drain=False), say(manager, "b", user_id=2, drain=False))
    await manager.drain()
    assert {uid for uid, _, _ in parts.gateway.sent} == {1, 2}
    assert len(parts.generator.calls) == 2


async def test_shutdown_bucht_zurueck(repo, parts):
    class SlowGenerator:
        async def generate(self, ctx, history):
            await asyncio.sleep(10)

    m = ConversationManager(repo, parts.gateway, SlowGenerator(), parts.facts, parts.payments, make_config())
    await say(m, "hallo", drain=False)
    await asyncio.sleep(0.3)
    assert (await repo.get_user(42)).credits == 1
    await m.shutdown()
    assert (await repo.get_user(42)).credits == 2


async def test_inbound_validierung():
    import pytest

    for bad in (
        [],
        event(user_id="42"),
        event(user_id=True),
        event(message_text="  "),
        event(timestamp="x"),
    ):
        with pytest.raises(ValueError):
            Inbound.from_payload(bad)
    msg = Inbound.from_payload(event(username=None, first_name=""))
    assert msg.username is None and msg.first_name is None


# ------------------------------------------------------------------ Shop-Modus

SHOP_ITEMS = [
    {"id": "jacke-1", "name": "Winterjacke Nordlicht", "url": "https://shop.test/jacke", "price": "89,90 €"},
    {"id": "muetze-2", "name": "Strickmütze", "url": "https://shop.test/muetze"},
]


def shop_manager(repo, parts, **overrides):
    from orchestrator.catalog import Catalog

    cfg = make_config(mode="shop", shop_name="Nordwind", free_credits=0, **overrides)
    catalog = Catalog.from_list(SHOP_ITEMS, link_query="utm_source=telegram")
    return ConversationManager(repo, parts.gateway, parts.generator, parts.facts, None, cfg, catalog)


async def test_shop_ohne_guthaben_mit_produktlinks(repo, parts):
    m = shop_manager(repo, parts)
    parts.generator.replies = [
        "Für den Winter würde ich [[jacke-1]] nehmen. Siehe auch https://fake.test/gratis!",
        "Dann vielleicht [[gibtsnicht]] oder [[muetze-2]]",
    ]
    await say(m, "brauche was warmes")
    texts = parts.gateway.texts()
    assert texts[1] == (
        "Für den Winter würde ich Winterjacke Nordlicht nehmen | "
        "Winterjacke Nordlicht: https://shop.test/jacke?utm_source=telegram"
    )
    assert "fake.test" not in " ".join(texts)

    await say(m, "und für den kopf?")
    assert parts.gateway.texts()[-1] == "Dann vielleicht oder Strickmütze | Strickmütze: https://shop.test/muetze?utm_source=telegram"

    user = await repo.get_user(42)
    assert user.credits == 0  # Guthaben spielt keine Rolle
    assert await repo.last_offer_at(42) is None  # keine Bezahlschranke
    history = await repo.get_recent_messages(42)
    assert history[1].content.startswith("Für den Winter würde ich [[jacke-1]]")  # Rohantwort im Verlauf
    await m.shutdown()


async def test_shop_befehle(repo, parts):
    m = shop_manager(repo, parts)
    await say(m, "/balance")  # kein Befehl im Shop: geht an die KI
    assert len(parts.generator.calls) == 1
    await say(m, "/delete")
    assert "Guthaben" not in parts.gateway.texts()[-1] and "/delete confirm" in parts.gateway.texts()[-1]
    await say(m, "/stop")
    assert (await repo.get_user(42)).ai_enabled is False
    await m.shutdown()


async def test_shop_braucht_katalog_chat_braucht_zahlung(repo, parts):
    import pytest

    with pytest.raises(ValueError):
        ConversationManager(repo, parts.gateway, parts.generator, parts.facts, None, make_config(mode="shop"))
    with pytest.raises(ValueError):
        ConversationManager(repo, parts.gateway, parts.generator, parts.facts, None, make_config())


async def test_tageslimit(repo, parts):
    m = shop_manager(repo, parts, daily_reply_limit=2)
    for text in ("a", "b", "c", "d"):
        await say(m, text)
    assert len(parts.generator.calls) == 2
    await m.shutdown()


async def test_chat_modus_entfernt_links_aus_antworten(manager, parts):
    parts.generator.replies = ["Klar! Schau auf www.irgendwas.test mal rein. Und [[x]] sonst?"]
    await say(manager, "hi")
    assert parts.gateway.texts()[-1] == "Klar! | Und sonst?"
