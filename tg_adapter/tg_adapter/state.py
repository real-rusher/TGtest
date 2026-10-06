"""Persistenter Zustand: pausierte User und bereits verbuchte Zahlungen."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class State:
    def __init__(self, path: str | None):
        self._path = Path(path) if path else None
        self.paused: set[int] = set()
        self.charges: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        data = json.loads(self._path.read_text("utf-8"))
        self.paused = {int(x) for x in data.get("paused", [])}
        self.charges = set(data.get("charges", []))

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"paused": sorted(self.paused), "charges": sorted(self.charges)}
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self._path)  # atomar, keine halb geschriebene Datei

    # --- Pause ---------------------------------------------------------
    def is_paused(self, user_id: int) -> bool:
        return user_id in self.paused

    def pause(self, user_id: int) -> bool:
        if user_id in self.paused:
            return False
        self.paused.add(user_id)
        self._save()
        return True

    def resume(self, user_id: int) -> bool:
        if user_id not in self.paused:
            return False
        self.paused.discard(user_id)
        self._save()
        return True

    # --- Zahlungen (Idempotenz) ---------------------------------------
    def mark_charge(self, charge_id: str) -> bool:
        """True, wenn die Zahlung neu ist; False bei Duplikat."""
        if charge_id in self.charges:
            return False
        self.charges.add(charge_id)
        self._save()
        return True
