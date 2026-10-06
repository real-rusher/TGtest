"""Sperrliste für /pause und /resume.

Liegt im Speicher und wird zusätzlich in eine kleine JSON-Datei geschrieben,
damit ein Neustart des Service keine pausierten Chats wieder freigibt.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("gateway.pauses")


class PauseList:
    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path) if path else None
        self._ids: set[int] = set()
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text("utf-8"))
            self._ids = {int(x) for x in data.get("paused", [])}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # Lieber laut abbrechen als pausierte Chats still wieder freizugeben.
            raise RuntimeError(
                f"Sperrliste {self._path} ist nicht lesbar: {exc}. "
                "Datei reparieren oder löschen, dann neu starten."
            ) from exc
        log.info("Sperrliste geladen: %d pausierte Chats", len(self._ids))

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps({"paused": sorted(self._ids)}), "utf-8")
        os.replace(tmp, self._path)

    def pause(self, user_id: int) -> bool:
        """True, wenn der Chat neu pausiert wurde."""
        if user_id in self._ids:
            return False
        self._ids.add(user_id)
        self._save()
        return True

    def resume(self, user_id: int) -> bool:
        """True, wenn der Chat vorher pausiert war."""
        if user_id not in self._ids:
            return False
        self._ids.discard(user_id)
        self._save()
        return True

    def is_paused(self, user_id: int) -> bool:
        return user_id in self._ids

    __contains__ = is_paused

    def snapshot(self) -> list[int]:
        return sorted(self._ids)

    def __len__(self) -> int:
        return len(self._ids)
