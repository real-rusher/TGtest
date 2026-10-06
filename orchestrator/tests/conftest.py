"""Gemeinsame Fakes und Fixtures.

Die Flow- und App-Tests brauchen PostgreSQL (TEST_PG_DSN), der Rest läuft ohne.
Achtung: Die Tests leeren das Schema 'public' der Testdatenbank.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

from orchestrator.config import Config, Texts
from orchestrator.conversation import ConversationManager
from orchestrator.gateway_client import GatewayError, GatewayPaused
from orchestrator.payments.dummy import DummyProvider

DSN = os.environ.get("TEST_PG_DSN")
needs_db = pytest.mark.skipif(not DSN, reason="TEST_PG_DSN nicht gesetzt")


def completion(content: str | None = None, refusal: str | None = None, finish_reason: str = "stop"):
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])


class FakeLLM:
    """Minimaler AsyncOpenAI-Ersatz: liefert vorgegebene Antworten und merkt sich die Aufrufe."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0) if self.replies else "okay"
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, SimpleNamespace):
            return reply
        return completion(reply)


class FakeGateway:
    def __init__(self):
        self.sent: list[tuple[int, list[str], list[float]]] = []
        self.paused: set[int] = set()
        self.fail = False

    async def send(self, user_id, chunks, delays, *, wait=True):
        if self.fail:
            raise GatewayError("kaputt")
        if user_id in self.paused:
            raise GatewayPaused("pausiert")
        self.sent.append((user_id, list(chunks), list(delays)))
        return len(chunks)

    def texts(self, user_id=None):
        return [" | ".join(c) for uid, c, _ in self.sent if user_id is None or uid == user_id]


class FakeFacts:
    def __init__(self, facts=None):
        self.facts = list(facts or [])
        self.calls: list[list[dict]] = []

    async def extract(self, messages, **_):
        self.calls.append(messages)
        return list(self.facts)


class FakeGenerator:
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls: list[tuple] = []

    async def generate(self, ctx, history):
        self.calls.append((ctx, list(history)))
        reply = self.replies.pop(0) if self.replies else "alles klar"
        if isinstance(reply, Exception):
            raise reply
        return reply


TEST_DISCLOSURE = "Hinweis: Hier antwortet eine KI."
TEST_PAYWALL = "Guthaben leer. {title} mit {credits} Antworten für {price}: {link}"


def make_config(**overrides) -> Config:
    base = Config(
        database_url=DSN or "postgresql://unused",
        inbound_token="in-secret",
        gateway_url="http://gateway",
        gateway_token="gw-secret",
        persona="Du bist Mia, 24, aus Hamburg.",
        llm_api_key="sk-test",
        debounce_seconds=0.05,
        free_credits=2,
        fact_every=3,
        payment_provider="dummy",
        public_base_url="https://example.test",
        offer_cooldown_hours=12,
    )
    if "texts" not in overrides:
        overrides["texts"] = replace(
            Texts.for_mode(overrides.get("mode", "chat")), disclosure=TEST_DISCLOSURE, paywall=TEST_PAYWALL
        )
    return replace(base, **overrides)


@pytest_asyncio.fixture
async def repo():
    if not DSN:
        pytest.skip("TEST_PG_DSN nicht gesetzt")
    from botdb import BotRepository

    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    r = await BotRepository.connect(DSN, None)
    await r.upsert_product("small", "Kleines Paket", 299, 50)
    await r.upsert_product("big", "Großes Paket", 999, 250)
    yield r
    await r.close()


@pytest.fixture
def parts():
    return SimpleNamespace(
        gateway=FakeGateway(),
        generator=FakeGenerator(),
        facts=FakeFacts(),
        payments=DummyProvider("https://example.test"),
    )


@pytest_asyncio.fixture
async def manager(repo, parts):
    m = ConversationManager(repo, parts.gateway, parts.generator, parts.facts, parts.payments, make_config())
    yield m
    await m.shutdown()


def event(user_id=42, text="hallo", **extra):
    payload = {
        "user_id": user_id,
        "username": "nutzer",
        "first_name": "Max",
        "message_text": text,
        "timestamp": 1791302400,
    }
    payload.update(extra)
    return payload


def dumps(obj) -> bytes:
    return json.dumps(obj).encode()
