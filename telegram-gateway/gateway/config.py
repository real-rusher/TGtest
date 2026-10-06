"""Konfiguration aus Umgebungsvariablen (optional über eine .env-Datei)."""

from __future__ import annotations

import os
from dataclasses import dataclass

try:  # .env ist optional, im Container kommen die Werte meist direkt aus env_file
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


class ConfigError(RuntimeError):
    """Fehlende oder ungültige Konfiguration."""


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _bool(name: str, default: bool) -> bool:
    value = _get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on", "ja"}


def _number(name: str, default: str, cast):
    raw = _get(name, default)
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} muss eine Zahl sein, ist aber {raw!r}") from None


def _optional_path(name: str, default: str) -> str | None:
    value = _get(name, default)
    if value and value.lower() in {"off", "none", "false"}:
        return None
    return value


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_path: str = "data/gateway"
    pause_file: str | None = "data/paused.json"

    http_host: str = "127.0.0.1"
    http_port: int = 8080
    api_token: str | None = None

    inbound_webhook_url: str | None = None
    inbound_webhook_token: str | None = None

    max_delay: float = 120.0
    max_flood_wait: int = 300
    auto_read: bool = True
    link_preview: bool = True
    pause_blocks_outbound: bool = True
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, *, require_api_token: bool = True) -> "Config":
        api_id = _get("TG_API_ID")
        api_hash = _get("TG_API_HASH")
        if not api_id or not api_hash:
            raise ConfigError(
                "TG_API_ID und TG_API_HASH müssen gesetzt sein (von my.telegram.org)."
            )
        try:
            api_id_int = int(api_id)
        except ValueError:
            raise ConfigError("TG_API_ID muss eine Zahl sein.") from None

        api_token = _get("API_TOKEN")
        if require_api_token and not api_token:
            raise ConfigError(
                "API_TOKEN muss gesetzt sein. Ohne Token könnte jeder, der den "
                "HTTP-Port erreicht, über deinen Account Nachrichten senden."
            )

        return cls(
            api_id=api_id_int,
            api_hash=api_hash,
            session_path=_get("SESSION_PATH", "data/gateway"),
            pause_file=_optional_path("PAUSE_FILE", "data/paused.json"),
            http_host=_get("HTTP_HOST", "127.0.0.1"),
            http_port=_number("HTTP_PORT", "8080", int),
            api_token=api_token,
            inbound_webhook_url=_get("INBOUND_WEBHOOK_URL"),
            inbound_webhook_token=_get("INBOUND_WEBHOOK_TOKEN"),
            max_delay=_number("MAX_DELAY", "120", float),
            max_flood_wait=_number("MAX_FLOOD_WAIT", "300", int),
            auto_read=_bool("AUTO_READ", True),
            link_preview=_bool("LINK_PREVIEW", True),
            pause_blocks_outbound=_bool("PAUSE_BLOCKS_OUTBOUND", True),
            log_level=_get("LOG_LEVEL", "INFO").upper(),
        )
