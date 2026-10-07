"""Unit-Tests ohne Datenbank und ohne Netz."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from botcore.config import BotConfig, ConfigError, Product, Settings, load_bot_config
from botcore.facts import parse_facts
from botcore.llm import AnthropicLLM, LLMRefusal, OpenAICompatibleLLM, normalize_history
from botcore.payments import (
    SignatureError,
    parse_reference_id,
    payment_link,
    reference_id,
    verify_stripe_signature,
)
from botcore.prompt import build_system_prompt, split_offer
from botcore.transport import TransportClient, UserUnreachable, split_long
from botdb import Memory, PurchaseEntry, User, UserContext

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- Konfiguration


@pytest.mark.parametrize("path", sorted((ROOT / "examples").glob("*.yaml")) + [ROOT / "config" / "bot.yaml"])
def test_mitgelieferte_konfigurationen_sind_gueltig(path):
    cfg = load_bot_config(path, transport="bot", content_dir=ROOT / "config" / "content")
    assert cfg.persona


def write(tmp_path, text):
    p = tmp_path / "bot.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_stars_nur_mit_bot(tmp_path):
    p = write(tmp_path, "persona: x\npayments: {method: stars}\nproducts: [{id: a, title: A, price_stars: 5}]\n")
    assert load_bot_config(p, transport="bot").payment_method == "stars"
    with pytest.raises(ConfigError, match="Bot-Account"):
        load_bot_config(p, transport="userbot")


@pytest.mark.parametrize(
    "yaml_text, fehler",
    [
        ("bot: {}\n", "persona"),
        ("persona: x\npayments: {method: paypal}\n", "payments.method"),
        ("persona: x\npayments: {method: stars}\nproducts: [{id: a, title: A}]\n", "price_stars"),
        ("persona: x\npayments: {method: stripe}\nproducts: [{id: a, title: A}]\n", "stripe_link"),
        ("persona: x\npayments: {method: link}\nproducts: [{id: a, title: A}]\n", "link"),
        ("persona: x\nproducts: [{id: 'a b', title: A}]\n", "id"),
        ("persona: x\nproducts: [{id: a, title: A}, {id: a, title: B}]\n", "eindeutig"),
        ("persona: x\nproducts: [{id: a, title: A, deliver: [{file: ../x.pdf}]}]\n", "liegt nicht"),
        ("persona: x\nstyle: {max_delay_seconds: 500}\n", "120"),
        ("persona: x\ndisclosure: {mode: immer}\n", "disclosure.mode"),
        ("persona: [kaputt\n", "YAML"),
    ],
)
def test_konfigurationsfehler(tmp_path, yaml_text, fehler):
    (tmp_path / "content").mkdir()
    with pytest.raises(ConfigError, match=fehler):
        load_bot_config(write(tmp_path, yaml_text), content_dir=tmp_path / "content")


def test_settings_aus_env(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith(("LLM_", "TELEGRAM_", "INTERNAL_", "DATABASE_")):
            monkeypatch.delenv(k, raising=False)
    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        Settings.from_env()
    monkeypatch.setenv("LLM_API_KEY", "k")
    with pytest.raises(ConfigError, match="INTERNAL_TOKEN"):
        Settings.from_env()
    monkeypatch.setenv("INTERNAL_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    s = Settings.from_env()
    assert (s.llm.provider, s.llm.model, s.transport) == ("anthropic", "claude-opus-5-5", "bot")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(ConfigError, match="LLM_MODEL"):
        Settings.from_env()
    monkeypatch.setenv("LLM_MODEL", "llama3")
    monkeypatch.delenv("LLM_API_KEY")
    monkeypatch.setenv("LLM_BASE_URL", "http://ollama:11434/v1")
    assert Settings.from_env().llm.api_key  # lokaler Server ohne Key erlaubt


# ---------------------------------------------------------------- Prompt


def ctx_with(memories=(), purchased=(), offered=()):
    now = datetime.now(timezone.utc)
    entry = lambda pid, st: PurchaseEntry(uuid4(), pid, pid.title(), 5, st, now)  # noqa: E731
    return UserContext(
        user=User(1, "maxi", "Max", True, now),
        memories=[Memory(uuid4(), f, "general", now) for f in memories],
        purchases=[entry(p, "purchased") for p in purchased],
        open_offers=[entry(p, "offered") for p in offered],
    )


SALES = BotConfig(
    persona="Du bist Lena.",
    payment_method="stars",
    products=(
        Product("guide", "Guide", "Ein PDF", "9 €", 50),
        Product("pack", "Pack", "", "", 120),
    ),
)


def test_system_prompt():
    prompt = build_system_prompt(SALES, ctx_with(["wohnt in Köln"], purchased=["pack"], offered=["guide"]),
                                 today=date(2026, 10, 7))
    assert prompt.startswith("Du bist Lena.")
    assert "Name: Max" in prompt and "wohnt in Köln" in prompt
    assert "- guide: Guide | 9 € | Ein PDF (wurde kürzlich angeboten" in prompt
    assert "- pack:" not in prompt and "Bereits gekauft (nicht erneut anbieten): Pack" in prompt
    assert "behaupte nicht, ein echter Mensch zu sein" in prompt
    assert "07.10.2026" in prompt


def test_system_prompt_ohne_verkauf():
    prompt = build_system_prompt(BotConfig(persona="Rollenspiel"), None)
    assert "OFFER" not in prompt and "Produkte" not in prompt
    assert "echter Mensch" in prompt  # Ehrlichkeitsregel gilt immer


def test_split_offer():
    assert split_offer("Klingt gut!\nSchau mal hier\n[[OFFER:guide]]") == ("Klingt gut!\nSchau mal hier", "guide")
    assert split_offer("Hey [[ offer : pack ]] du") == ("Hey  du", "pack")
    assert split_offer("Nur Text") == ("Nur Text", None)
    assert split_offer("[[irgendwas]] Hallo") == ("Hallo", None)


# ---------------------------------------------------------------- Zahlungen


def test_reference_roundtrip():
    assert parse_reference_id(reference_id(42, "big_pack")) == (42, "big_pack")
    for bad in (None, "", "x42-a", "u-a", "uabc-a", "u42-"):
        assert parse_reference_id(bad) is None


def test_payment_links():
    p = Product("guide", "G", stripe_link="https://buy.stripe.com/abc?prefilled_email=a@b.de",
                link="https://pay.example/{product_id}?user={user_id}&ref={ref}")
    assert payment_link("stripe", p, 7) == (
        "https://buy.stripe.com/abc?prefilled_email=a%40b.de&client_reference_id=u7-guide"
    )
    assert payment_link("link", p, 7) == "https://pay.example/guide?user=7&ref=u7-guide"
    assert payment_link("stars", p, 7) is None


def sign(payload: bytes, secret="whsec_test", t=None):
    t = int(time.time()) if t is None else t
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def test_stripe_signature():
    body = json.dumps({"type": "checkout.session.completed"}).encode()
    assert verify_stripe_signature(body, sign(body), "whsec_test")["type"] == "checkout.session.completed"
    with pytest.raises(SignatureError):
        verify_stripe_signature(body, sign(body, "falsch"), "whsec_test")
    with pytest.raises(SignatureError):
        verify_stripe_signature(body, sign(body, t=int(time.time()) - 3600), "whsec_test")
    with pytest.raises(SignatureError):
        verify_stripe_signature(body, None, "whsec_test")
    with pytest.raises(SignatureError):
        verify_stripe_signature(body + b" ", sign(body), "whsec_test")


# ---------------------------------------------------------------- Sprachmodell


def test_normalize_history():
    msgs = [
        {"role": "assistant", "content": "Willkommen"},
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "bist du da?"},
        {"role": "assistant", "content": "ja"},
        {"role": "user", "content": "  "},
    ]
    assert normalize_history(msgs) == [
        {"role": "user", "content": "hi\nbist du da?"},
        {"role": "assistant", "content": "ja"},
    ]


class FakeAnthropicMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def anthropic_response(text, stop="end_turn"):
    return SimpleNamespace(
        stop_reason=stop,
        stop_details=SimpleNamespace(category="cyber") if stop == "refusal" else None,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
    )


async def test_anthropic_llm_parameter():
    from botcore.config import LLMSettings

    messages = FakeAnthropicMessages(anthropic_response(" Hallo! "))
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages), messages=messages)
    llm = AnthropicLLM(LLMSettings("anthropic", "k", "claude-opus-5-5"), client=client)
    assert await llm.chat("sys", [{"role": "user", "content": "hi"}]) == "Hallo!"
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["system"] == "sys"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"] == {"effort": "low"}
    assert "temperature" not in call and "thinking" not in call

    no_fb = AnthropicLLM(LLMSettings("anthropic", "k", "m", fallbacks=False), client=client)
    await no_fb.chat("sys", [{"role": "user", "content": "hi"}])
    assert "fallbacks" not in messages.calls[1]

    messages.response = anthropic_response("", stop="refusal")
    with pytest.raises(LLMRefusal):
        await llm.chat("sys", [{"role": "user", "content": "hi"}])


async def test_openai_llm():
    from botcore.config import LLMSettings

    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Yo", refusal=None))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    llm = OpenAICompatibleLLM(LLMSettings("openai", "k", "gpt-x"), client=client)
    assert await llm.chat("sys", [{"role": "user", "content": "hi"}]) == "Yo"
    assert calls[0]["messages"][0] == {"role": "system", "content": "sys"}


def test_parse_facts():
    assert parse_facts('Hier: {"facts": ["fährt Motorrad", "fährt motorrad", "wohnt in Köln."]}') == [
        "fährt Motorrad",
        "wohnt in Köln",
    ]
    assert parse_facts("kein json") == []
    assert parse_facts('{"facts": "x"}') == []


# ---------------------------------------------------------------- Transport


def test_split_long():
    chunks, delays = split_long(["a" * 5000, "kurz"], [2.0, 1.0])
    assert [len(c) for c in chunks] == [4096, 904, 4]
    assert delays == [2.0, 0.5, 1.0]


@pytest.mark.parametrize("kind, key", [("bot", "text_chunks"), ("userbot", "chunks")])
async def test_transport_feldnamen(kind, key):
    received = []

    async def send(request):
        assert request.headers["Authorization"] == "Bearer tok"
        assert request.query.get("wait") == "1"
        body = await request.json()
        received.append(body)
        if body["user_id"] == 404:
            return web.json_response({"error": "unbekannt"}, status=404)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/send", send)
    server = TestServer(app)
    await server.start_server()
    client = TransportClient(kind, str(server.make_url("")), "tok")
    try:
        await client.send_chunks(1, ["a", "b"], [1.0, 2.0])
        assert received[0] == {"user_id": 1, key: ["a", "b"], "delays": [1.0, 2.0]}
        with pytest.raises(UserUnreachable):
            await client.send_chunks(404, ["a"], [0])
    finally:
        await client.close()
        await server.close()
