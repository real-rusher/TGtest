"""Antwort-Generator: baut aus Persona, Gedächtnis, Verlauf und ggf. Katalog den Prompt und ruft das Modell auf."""

from __future__ import annotations

import logging
from typing import Any, Sequence

from botdb import ChatMessage, UserContext

from .catalog import Catalog

log = logging.getLogger(__name__)

STYLE_RULES = """\
SO SCHREIBST DU
- Du chattest in einem Messenger. Schreib kurz und locker, wie in einem echten Chat.
- Kein Markdown, keine Überschriften, keine Aufzählungen.
- Jede Zeile wird als eigene Nachricht verschickt. Nutze Zeilenumbrüche sparsam.
- Antworte in der Sprache, in der der Nutzer schreibt."""

CHAT_RULES = """\
LINKS
- Schreib keine Links."""

SHOP_RULES = """\
PRODUKTBERATUNG FÜR {shop_name}
- Du berätst im Auftrag von {shop_name}. Empfiehl nur Produkte aus dem KATALOG unten.
- Wenn du ein Produkt nennst, schreib seine ID in doppelten eckigen Klammern, z. B. [[abc-123]].
  Name und Link werden automatisch eingesetzt. Schreib selbst keine Links.
- Erfinde keine Produkte, Preise, Rabatte, Lieferzeiten oder Eigenschaften. Was nicht im
  Katalog steht, weißt du nicht. Sag das ehrlich und verweise auf den Shop.
- Empfiehl höchstens drei Produkte pro Antwort und nur, wenn sie zur Frage passen.
- Dräng niemanden zum Kauf und erzeuge keinen künstlichen Zeitdruck."""

# Gilt in jedem Modus und ist nicht abschaltbar: Der Account sieht aus wie ein normaler
# Nutzer, deshalb darf die KI nie leugnen, eine zu sein.
HONESTY_RULES = """\
GRENZEN
- Wenn der Nutzer ernsthaft wissen will, ob er mit einem Menschen oder einer KI schreibt,
  sag ehrlich, dass du eine KI bist. Behaupte nie, ein Mensch zu sein.
- Frag nicht nach Passwörtern, Kontodaten, Adressen oder Ausweisdaten."""

QUERY_MESSAGES = 3  # so viele letzte Nutzernachrichten bestimmen die Katalogauswahl


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
        catalog: Catalog | None = None,
        shop_name: str | None = None,
        catalog_prompt_limit: int = 40,
    ) -> None:
        self._client = client  # AsyncOpenAI oder kompatibel
        self._model = model
        self._persona = persona.strip()
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._catalog = catalog
        self._shop_name = shop_name or "den Shop"
        self._catalog_limit = catalog_prompt_limit

    @property
    def catalog(self) -> Catalog | None:
        return self._catalog

    def system_prompt(self, ctx: UserContext, history: Sequence[ChatMessage] = ()) -> str:
        parts = [self._persona, STYLE_RULES]
        if self._catalog is None:
            parts.append(CHAT_RULES)
        else:
            parts.append(SHOP_RULES.replace("{shop_name}", self._shop_name))
        parts.append(HONESTY_RULES)

        about: list[str] = []
        if ctx.user.first_name:
            about.append(f"- Vorname: {ctx.user.first_name}")
        # Neueste zuerst gespeichert, für den Prompt chronologisch
        about.extend(f"- {m.fact}" for m in reversed(ctx.memories))
        if about:
            parts.append("WAS DU ÜBER DEN NUTZER WEISST\n" + "\n".join(about))

        if self._catalog is not None:
            recent = [m.content for m in history if m.role == "user"][-QUERY_MESSAGES:]
            items = self._catalog.select(" ".join(recent), self._catalog_limit)
            lines = "\n".join(item.prompt_line() for item in items)
            note = "" if len(items) == len(self._catalog) else " (Auswahl passend zur Anfrage)"
            parts.append(f"KATALOG{note}\n{lines}")
        return "\n\n".join(parts)

    def build_messages(self, ctx: UserContext, history: Sequence[ChatMessage]) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": self.system_prompt(ctx, history)}]
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
