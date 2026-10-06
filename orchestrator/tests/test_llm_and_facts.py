import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from botdb import ChatMessage, Memory, User, UserContext
from orchestrator.facts import FactExtractor, format_transcript, normalize_facts
from orchestrator.llm import BASE_RULES, GenerationError, ResponseGenerator

from conftest import FakeLLM, completion

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def ctx(**user):
    data = dict(
        telegram_id=1, username="u", first_name="Max", ai_enabled=True, credits=5,
        disclosed_at=NOW, created_at=NOW, last_active=NOW,
    )
    data.update(user)
    return UserContext(
        user=User(**data),
        memories=[
            Memory(uuid4(), "fährt Motorrad", "general", NOW),  # neuester zuerst
            Memory(uuid4(), "wohnt in Köln", "general", NOW),
        ],
    )


def msg(i, role, content):
    return ChatMessage(i, role, content, NOW)


# ------------------------------------------------------------------ Antwort-Generator


def test_prompt_enthaelt_persona_regeln_und_fakten():
    gen = ResponseGenerator(FakeLLM(), "m", "Du bist Mia.")
    prompt = gen.system_prompt(ctx())
    assert prompt.startswith("Du bist Mia.")
    assert BASE_RULES in prompt
    assert "Behaupte nie, ein Mensch zu sein" in prompt
    assert prompt.index("wohnt in Köln") < prompt.index("fährt Motorrad")
    assert "- Vorname: Max" in prompt


def test_aufeinanderfolgende_rollen_werden_zusammengefasst():
    gen = ResponseGenerator(FakeLLM(), "m", "P")
    history = [msg(1, "user", "hi"), msg(2, "user", "bist du da?"), msg(3, "assistant", "ja"), msg(4, "user", "cool")]
    messages = gen.build_messages(ctx(), history)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "hi\nbist du da?"


async def test_generate_ruft_modell_auf():
    llm = FakeLLM(["  hey du  "])
    gen = ResponseGenerator(llm, "modell-x", "P", max_tokens=123, temperature=0.5)
    assert await gen.generate(ctx(), [msg(1, "user", "hi")]) == "hey du"
    call = llm.calls[0]
    assert call["model"] == "modell-x" and call["max_tokens"] == 123 and call["temperature"] == 0.5


@pytest.mark.parametrize(
    "reply",
    [RuntimeError("netz"), completion(""), completion(None, refusal="nein")],
)
async def test_generate_fehler(reply):
    gen = ResponseGenerator(FakeLLM([reply]), "m", "P")
    with pytest.raises(GenerationError):
        await gen.generate(ctx(), [msg(1, "user", "hi")])


async def test_generate_braucht_user_als_letzte_nachricht():
    gen = ResponseGenerator(FakeLLM(), "m", "P")
    with pytest.raises(GenerationError):
        await gen.generate(ctx(), [msg(1, "user", "hi"), msg(2, "assistant", "ho")])


# ------------------------------------------------------------------ Fakten


async def test_fakten_extraktion():
    llm = FakeLLM([json.dumps({"facts": ["fährt Motorrad", "Fährt Motorrad.", " wohnt  in Berlin "]})])
    facts = await FactExtractor(llm, "m").extract([
        {"role": "system", "content": "ignorieren"},
        {"role": "user", "content": "Ich fahr Motorrad"},
        {"role": "assistant", "content": "Wohnst du in Berlin?"},
        {"role": "user", "content": [{"type": "text", "text": "ja"}]},
    ])
    assert facts == ["fährt Motorrad", "wohnt in Berlin"]
    call = llm.calls[0]
    assert call["temperature"] == 0 and call["response_format"]["type"] == "json_schema"
    transcript = call["messages"][1]["content"]
    assert "ignorieren" not in transcript and "[user]: ja" in transcript
    assert "Gesundheit" in call["messages"][0]["content"]  # besondere Kategorien ausgeschlossen


async def test_fakten_ohne_user_nachricht_kein_aufruf():
    llm = FakeLLM()
    assert await FactExtractor(llm, "m").extract([{"role": "assistant", "content": "hi"}]) == []
    assert await FactExtractor(llm, "m").extract([]) == []
    assert llm.calls == []


async def test_fakten_fehler_und_verweigerung():
    assert await FactExtractor(FakeLLM([RuntimeError("x")]), "m").extract([{"role": "user", "content": "a"}]) == []
    assert await FactExtractor(FakeLLM([completion(None, refusal="no")]), "m").extract(
        [{"role": "user", "content": "a"}]
    ) == []
    assert await FactExtractor(FakeLLM(["kein json"]), "m").extract([{"role": "user", "content": "a"}]) == []
    with pytest.raises(RuntimeError):
        await FactExtractor(FakeLLM([RuntimeError("x")]), "m").extract(
            [{"role": "user", "content": "a"}], raise_on_error=True
        )


def test_normalize_grenzen():
    assert normalize_facts("nope") == []
    assert normalize_facts(["x" * 201, 5, "", "ok"]) == ["ok"]
    assert len(normalize_facts([f"f{i}" for i in range(50)])) == 20


def test_transcript():
    assert format_transcript([{"role": "user", "content": " a "}, "kaputt", {"role": "tool", "content": "x"}]) == "[user]: a"
