"""Antwort-Generator: baut aus Persona, Gedächtnis und Verlauf den Prompt und ruft das Modell auf."""

from __future__ import annotations

import logging
from typing import Any, Sequence

from botdb import ChatMessage, UserContext

log = logging.getLogger(__name__)

# Feste Regeln, die jede Persona ergänzen. Die letzte Regel ist nicht abschaltbar:
# Der Account sieht aus wie ein normaler Nutzer, deshalb darf die KI nie leugnen, eine zu sein.
BASE_RULES = """\
SO SCHREIBST DU
- Du chattest in einem Messenger. Schreib kurz und locker, wie in einem echten Chat.
- Kein Markdown, keine Überschriften, keine Aufzählungen, keine Links.
- Jede Zeile wird als eigene Nachricht verschickt. Nutze Zeilenumbrüche sparsam.
- Antworte in der Sprache, in der der Nutzer schreibt.

GRENZEN
- Wenn der Nutzer ernsthaft wissen will, ob er mit einem Menschen oder einer KI schreibt,
  sag ehrlich, dass du eine KI bist. Behaupte nie, ein Mensch zu sein.
- Frag nicht nach Passwörtern, Kontodaten, Adressen oder Ausweisdaten."""


class GenerationError(RuntimeError):
    pass


class ResponseGenerator:
    def __init__(
        self,
        client: Any,
        model: str,
        persona: str,
        *,
        max_tokens: int = 400,
        temperature: float = 0.9,
    ) -> None:
        self._client = client  # AsyncOpenAI oder kompatibel
        self._model = model
        self._persona = persona.strip()
        self._max_tokens = max_tokens
        self._temperature = temperature

    def system_prompt(self, ctx: UserContext) -> str:
        parts = [self._persona, BASE_RULES]
        about: list[str] = []
        if ctx.user.first_name:
            about.append(f"- Vorname: {ctx.user.first_name}")
        # Neueste zuerst gespeichert, für den Prompt chronologisch
        about.extend(f"- {m.fact}" for m in reversed(ctx.memories))
        if about:
            parts.append("WAS DU ÜBER DEN NUTZER WEISST\n" + "\n".join(about))
        return "\n\n".join(parts)

    def build_messages(self, ctx: UserContext, history: Sequence[ChatMessage]) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": self.system_prompt(ctx)}]
        for msg in history:
            # Aufeinanderfolgende Nachrichten derselben Rolle zusammenfassen (mehrere kurze Nachrichten)
            if len(messages) > 1 and messages[-1]["role"] == msg.role:
                messages[-1]["content"] += "\n" + msg.content
            else:
                messages.append({"role": msg.role, "content": msg.content})
        return messages

    async def generate(self, ctx: UserContext, history: Sequence[ChatMessage]) -> str:
        messages = self.build_messages(ctx, history)
        if messages[-1]["role"] != "user":
            raise GenerationError("Letzte Nachricht im Verlauf ist nicht vom Nutzer")
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        except Exception as exc:
            raise GenerationError(f"Modellaufruf fehlgeschlagen: {exc}") from exc

        choice = response.choices[0]
        message = choice.message
        if getattr(message, "refusal", None):
            raise GenerationError(f"Modell hat verweigert: {message.refusal}")
        text = (message.content or "").strip()
        if not text:
            raise GenerationError("Leere Antwort vom Modell")
        if getattr(choice, "finish_reason", None) == "length":
            log.warning("Antwort wurde bei max_tokens abgeschnitten")
        return text
