"""
Builder 4: Naturalizing Engine & Delay Calculator

Zerlegt Fließtext in kurze, messengertypische Chunks und berechnet
für jeden Chunk eine Schreibverzögerung:

    Delay (Sekunden) = 1.0 + (N * 0.05)    # N = Zeichen im Chunk
"""

import re

BASE_DELAY = 1.0
DELAY_PER_CHAR = 0.05

# Trennt nach . ! ? (auch "...", "?!" usw.), aber nur wenn danach Leerraum folgt.
# Dadurch bleiben Dezimalzahlen ("3.5"), URLs und "z.B." am Wortende intakt.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _clean_chunk(chunk: str) -> str:
    """Messenger-Stil: einzelnen Schlusspunkt entfernen, "..." / "!" / "?" bleiben."""
    chunk = chunk.strip()
    if chunk.endswith(".") and not chunk.endswith(".."):
        chunk = chunk[:-1].rstrip()
    return chunk


def _calc_delay(chunk: str) -> float:
    return round(BASE_DELAY + len(chunk) * DELAY_PER_CHAR, 2)


def naturalize_response(raw_text: str) -> dict:
    """
    Zerlegt raw_text an Zeilenumbrüchen und Satzzeichen (. ! ?) in Chunks
    und berechnet pro Chunk eine Verzögerung in Sekunden.

    Rückgabe:
        {"chunks": [str, ...], "delays": [float, ...]}
    """
    if not isinstance(raw_text, str) or not raw_text.strip():
        return {"chunks": [], "delays": []}

    chunks = []
    for line in raw_text.splitlines():
        for part in _SENTENCE_SPLIT.split(line):
            cleaned = _clean_chunk(part)
            if cleaned:
                chunks.append(cleaned)

    delays = [_calc_delay(c) for c in chunks]
    return {"chunks": chunks, "delays": delays}


if __name__ == "__main__":
    demo = "Hahaha okay. und was hast du danach gemacht?\nWar echt krass... Erzähl!"
    print(naturalize_response(demo))
