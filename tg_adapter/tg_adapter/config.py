"""Konfiguration aus Umgebungsvariablen."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(";", ",").split(",") if x.strip())


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    admin_ids: frozenset[int] = field(default_factory=frozenset)

    # Inbound: wohin eingehende Nachrichten / Zahlungen gehen
    inbound_url: str = ""
    payment_url: str = ""
    inbound_token: str = ""

    # Outbound: HTTP-API, über die der Core Befehle schickt
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    outbound_token: str = ""

    session_path: str = "data/bot"
    state_path: str = "data/state.json"
    max_delay: float = 30.0

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        missing = [k for k in ("TG_API_ID", "TG_API_HASH", "TG_BOT_TOKEN") if not env.get(k)]
        if missing:
            raise SystemExit(f"Fehlende Umgebungsvariablen: {', '.join(missing)}")
        return cls(
            api_id=int(env["TG_API_ID"]),
            api_hash=env["TG_API_HASH"],
            bot_token=env["TG_BOT_TOKEN"],
            admin_ids=_ids(env.get("ADMIN_IDS", "")),
            inbound_url=env.get("INBOUND_URL", ""),
            payment_url=env.get("PAYMENT_URL", ""),
            inbound_token=env.get("INBOUND_TOKEN", ""),
            http_host=env.get("HTTP_HOST", "0.0.0.0"),
            http_port=int(env.get("HTTP_PORT", "8080")),
            outbound_token=env.get("OUTBOUND_TOKEN", ""),
            session_path=env.get("SESSION_PATH", "data/bot"),
            state_path=env.get("STATE_PATH", "data/state.json"),
            max_delay=float(env.get("MAX_DELAY", "30")),
        )
