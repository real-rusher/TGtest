"""Fakten-Extraktion über das konfigurierte Sprachmodell.

Nutzt Prompt, Transkript-Format und Bereinigung aus fact_extractor.py, ruft das
Modell aber über die gemeinsame LLM-Schnittstelle auf. Dadurch funktioniert die
Extraktion mit jedem Anbieter, nicht nur mit OpenAI.
"""

from __future__ import annotations

import json
import logging

from .fact_extractor import SYSTEM_PROMPT, _format_transcript, _normalize
from .llm import LLM

log = logging.getLogger(__name__)

JSON_HINT = '\nAntworte ausschliesslich mit JSON in der Form {"facts": ["...", "..."]}, ohne weiteren Text.'


def parse_facts(raw: str) -> list[str]:
    """Liest {"facts": [...]} aus der Modellantwort, auch wenn Text drumherum steht."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return []
    return _normalize(data.get("facts") if isinstance(data, dict) else None)


async def extract_facts(llm: LLM, chat_history_slice: list[dict]) -> list[str]:
    """Extrahiert dauerhafte Fakten über den User. Bei Fehlern: leere Liste."""
    transcript = _format_transcript(chat_history_slice)
    if "[user]:" not in transcript:
        return []
    try:
        raw = await llm.chat(
            SYSTEM_PROMPT + JSON_HINT,
            [{"role": "user", "content": f"<transcript>\n{transcript}\n</transcript>"}],
        )
    except Exception as exc:  # Extraktion ist Zusatz, darf den Chat nie stören
        log.warning("Fakten-Extraktion fehlgeschlagen: %s", exc)
        return []
    return parse_facts(raw)
