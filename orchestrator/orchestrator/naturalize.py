"""Zerlegt eine KI-Antwort in kurze, messengertypische Chunks mit Schreibverzögerung.

    Delay (Sekunden) = base_delay + Zeichen * per_char, gedeckelt auf max_delay

Rückgabe: {"chunks": [...], "delays": [...]}, passend zum Sendeauftrag des Gateways.
"""

from __future__ import annotations

import re

BASE_DELAY = 1.0
DELAY_PER_CHAR = 0.05
MAX_DELAY = 25.0          # muss <= MAX_DELAY des Gateways sein
MAX_CHUNK_CHARS = 1000    # längere Absätze werden an Leerzeichen geteilt (Telegram-Limit: 4096)

# Trennt nach . ! ? (auch "...", "?!"), aber nur wenn danach Leerraum folgt.
# Dadurch bleiben Dezimalzahlen ("3.5") und URLs intakt.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Endet ein Teil auf eine dieser Abkürzungen, gehört der nächste Teil noch zum Satz.
_ABBREVIATIONS = {
    "z.b.", "d.h.", "u.a.", "o.ä.", "u.u.", "v.a.", "z.t.", "i.d.r.", "bzw.", "ca.", "usw.",
    "etc.", "evtl.", "ggf.", "inkl.", "vgl.", "nr.", "dr.", "prof.", "hr.", "fr.", "str.",
    "e.g.", "i.e.", "vs.", "mr.", "mrs.", "ms.", "st.",
}
_ORDINAL = re.compile(r"(?:^|\s)\d{1,3}\.$")          # "am 3. Mai"
_INITIAL = re.compile(r"(?:^|\s)[A-Za-zÄÖÜäöü]\.$")   # "Peter M. Müller"
# Fett-Markierungen, Überschriften und Aufzählungszeichen passen nicht in einen Messenger.
# Einzelne *Sternchen* bleiben stehen (z. B. für *lacht*).
_MARKDOWN = re.compile(r"\*\*|__|^\s*#{1,6}\s+|^\s*[-•]\s+", re.MULTILINE)


def _ends_with_abbreviation(text: str) -> bool:
    last_word = text.rsplit(None, 1)[-1].lower() if text.strip() else ""
    return (
        last_word in _ABBREVIATIONS
        or bool(_ORDINAL.search(text))
        or bool(_INITIAL.search(text))
    )


def _split_sentences(line: str) -> list[str]:
    parts = _SENTENCE_SPLIT.split(line)
    merged: list[str] = []
    for part in parts:
        if merged and _ends_with_abbreviation(merged[-1]):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged


def _split_long(chunk: str, limit: int) -> list[str]:
    out: list[str] = []
    while len(chunk) > limit:
        cut = chunk.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        out.append(chunk[:cut].rstrip())
        chunk = chunk[cut:].lstrip()
    if chunk:
        out.append(chunk)
    return out


def _clean_chunk(chunk: str) -> str:
    """Messenger-Stil: einzelnen Schlusspunkt entfernen, "..." / "!" / "?" bleiben."""
    chunk = chunk.strip()
    if chunk.endswith(".") and not chunk.endswith("..") and not _ends_with_abbreviation(chunk):
        chunk = chunk[:-1].rstrip()
    return chunk


def calc_delay(
    chunk: str,
    *,
    base_delay: float = BASE_DELAY,
    per_char: float = DELAY_PER_CHAR,
    max_delay: float = MAX_DELAY,
) -> float:
    return round(min(base_delay + len(chunk) * per_char, max_delay), 2)


def naturalize_response(
    raw_text: str,
    *,
    base_delay: float = BASE_DELAY,
    per_char: float = DELAY_PER_CHAR,
    max_delay: float = MAX_DELAY,
    max_chunk_chars: int = MAX_CHUNK_CHARS,
) -> dict:
    """Zerlegt raw_text an Zeilenumbrüchen und Satzenden und berechnet pro Chunk ein Delay.

    Leerer Text ergibt leere Listen; der Aufrufer darf dann nichts senden.
    """
    if not isinstance(raw_text, str) or not raw_text.strip():
        return {"chunks": [], "delays": []}

    text = _MARKDOWN.sub("", raw_text)
    chunks: list[str] = []
    for line in text.splitlines():
        for sentence in _split_sentences(line):
            cleaned = _clean_chunk(sentence)
            if cleaned:
                chunks.extend(_split_long(cleaned, max_chunk_chars))

    delays = [
        calc_delay(c, base_delay=base_delay, per_char=per_char, max_delay=max_delay) for c in chunks
    ]
    return {"chunks": chunks, "delays": delays}
