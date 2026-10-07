"""Datenobjekte an den Schnittstellen des Gateways."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

TELEGRAM_MAX_TEXT = 4096


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """Eingang 1: eingehende private Textnachricht."""

    user_id: int
    username: str | None
    first_name: str | None
    message_text: str
    timestamp: int  # Unix-Sekunden

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SendJob:
    """Ausgang: Sendeauftrag, chunks und delays werden paarweise abgearbeitet."""

    user_id: int
    chunks: tuple[str, ...]
    delays: tuple[float, ...]

    @classmethod
    def from_dict(cls, data: Any, *, max_delay: float = 120.0) -> "SendJob":
        if not isinstance(data, dict):
            raise ValueError("Sendeauftrag muss ein JSON-Objekt sein")

        user_id = data.get("user_id")
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ValueError("user_id muss eine Ganzzahl sein")

        chunks = data.get("chunks")
        delays = data.get("delays")
        if not isinstance(chunks, list) or not chunks:
            raise ValueError("chunks muss eine nicht leere Liste sein")
        if not isinstance(delays, list):
            raise ValueError("delays muss eine Liste sein")
        if len(chunks) != len(delays):
            raise ValueError(
                f"chunks ({len(chunks)}) und delays ({len(delays)}) müssen gleich lang sein"
            )

        clean_chunks: list[str] = []
        for i, chunk in enumerate(chunks):
            if not isinstance(chunk, str) or not chunk.strip():
                raise ValueError(f"chunks[{i}] muss ein nicht leerer Text sein")
            if len(chunk) > TELEGRAM_MAX_TEXT:
                raise ValueError(
                    f"chunks[{i}] ist länger als {TELEGRAM_MAX_TEXT} Zeichen"
                )
            clean_chunks.append(chunk)

        clean_delays: list[float] = []
        for i, delay in enumerate(delays):
            if isinstance(delay, bool) or not isinstance(delay, (int, float)):
                raise ValueError(f"delays[{i}] muss eine Zahl sein")
            delay = float(delay)
            if not math.isfinite(delay) or delay < 0:
                raise ValueError(f"delays[{i}] muss >= 0 sein")
            if delay > max_delay:
                raise ValueError(f"delays[{i}] ist größer als MAX_DELAY ({max_delay}s)")
            clean_delays.append(delay)

        return cls(user_id=user_id, chunks=tuple(clean_chunks), delays=tuple(clean_delays))
