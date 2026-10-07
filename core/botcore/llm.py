"""Anbindung an das Sprachmodell.

Zwei Anbieter-Familien, umschaltbar über LLM_PROVIDER in der .env:
  * anthropic: Claude über das offizielle Anthropic-SDK
  * openai:    OpenAI sowie jeder OpenAI-kompatible Dienst (OpenRouter, Groq, Together,
               Mistral, lokale Server wie Ollama oder vLLM) über LLM_BASE_URL

Beide liefern über dieselbe Methode `chat(system, messages) -> str` reinen Text.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .config import LLMSettings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Das Modell hat keine verwertbare Antwort geliefert."""


class LLMRefusal(LLMError):
    """Das Modell hat die Antwort aus Sicherheitsgründen abgelehnt."""


class LLM(Protocol):
    async def chat(self, system: str, messages: list[dict]) -> str: ...


def normalize_history(messages: list[dict]) -> list[dict]:
    """Bereitet den Verlauf für die APIs vor.

    Fasst aufeinanderfolgende Nachrichten derselben Rolle zusammen (mehrere kurze
    Nachrichten des Users hintereinander sind im Messenger normal) und entfernt
    Assistent-Nachrichten am Anfang, weil der Verlauf mit dem User beginnen muss.
    """
    merged: list[dict] = []
    for msg in messages:
        content = (msg.get("content") or "").strip()
        if not content or msg.get("role") not in ("user", "assistant"):
            continue
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] += "\n" + content
        else:
            merged.append({"role": msg["role"], "content": content})
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    return merged


class AnthropicLLM:
    """Claude über das Anthropic-SDK."""

    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, settings: LLMSettings, model: str | None = None, client=None):
        if client is None:
            import anthropic

            kwargs = {"api_key": settings.api_key}
            if settings.base_url:
                kwargs["base_url"] = settings.base_url
            client = anthropic.AsyncAnthropic(**kwargs)
        self._client = client
        self._settings = settings
        self._model = model or settings.model

    async def chat(self, system: str, messages: list[dict]) -> str:
        params = dict(
            model=self._model,
            max_tokens=self._settings.max_tokens,
            system=system,
            messages=normalize_history(messages),
            output_config={"effort": self._settings.effort},
            cache_control={"type": "ephemeral"},  # Systemprompt + Verlauf werden zwischengespeichert
        )
        if self._settings.fallbacks:
            # Lehnt das Modell ab, beantwortet Anthropic die Anfrage serverseitig mit einem passenden Modell.
            response = await self._client.beta.messages.create(
                betas=[self.FALLBACK_BETA], fallbacks="default", **params
            )
        else:
            response = await self._client.messages.create(**params)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMRefusal(f"Claude hat abgelehnt ({getattr(details, 'category', None)})")
        if response.stop_reason == "max_tokens":
            log.warning("Antwort wurde bei max_tokens abgeschnitten (LLM_MAX_TOKENS erhöhen)")
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
        if not text:
            raise LLMError(f"Leere Antwort (stop_reason={response.stop_reason})")
        return text


class OpenAICompatibleLLM:
    """OpenAI oder ein OpenAI-kompatibler Endpunkt."""

    def __init__(self, settings: LLMSettings, model: str | None = None, client=None):
        if client is None:
            import openai

            client = openai.AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url)
        self._client = client
        self._model = model or settings.model

    async def chat(self, system: str, messages: list[dict]) -> str:
        # Bewusst nur die Pflichtparameter: so funktioniert es mit möglichst vielen Anbietern.
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": system}, *normalize_history(messages)],
        )
        message = response.choices[0].message
        if getattr(message, "refusal", None):
            raise LLMRefusal(f"Modell hat abgelehnt: {message.refusal}")
        text = (message.content or "").strip()
        if not text:
            raise LLMError("Leere Antwort vom Modell")
        return text


def make_llm(settings: LLMSettings, *, for_facts: bool = False) -> LLM:
    model = settings.facts_model if for_facts and settings.facts_model else settings.model
    if settings.provider == "anthropic":
        return AnthropicLLM(settings, model)
    return OpenAICompatibleLLM(settings, model)
