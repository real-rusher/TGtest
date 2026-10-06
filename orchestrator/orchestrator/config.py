"""Konfiguration aus Umgebungsvariablen (optional über eine .env-Datei)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


class ConfigError(RuntimeError):
    """Fehlende oder ungültige Konfiguration."""


DEFAULT_DISCLOSURE = (
   
)
DEFAULT_PAYWALL = (

)
DEFAULT_PAYMENT_CONFIRM = "Danke, die Zahlung ist angekommen! Dein Guthaben: {balance} Antworten."
DEFAULT_STOP = "Okay, ab jetzt antwortet hier keine KI mehr. Mit /start geht es wieder weiter."
DEFAULT_START = "Die KI-Antworten sind wieder an."
DEFAULT_BALANCE = "Dein Guthaben: {balance} Antworten."
DEFAULT_DELETE_WARN = (
    "Das löscht deinen Chatverlauf, alles was über dich gespeichert ist und dein Guthaben "
    "({balance} Antworten). Zum Bestätigen schick: /delete confirm"
)
DEFAULT_DELETE_DONE = "Erledigt, alle deine Daten sind gelöscht."


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _require(name: str) -> str:
    value = _get(name)
    if not value:
        raise ConfigError(f"{name} muss gesetzt sein.")
    return value


def _number(name: str, default: str, cast):
    raw = _get(name, default)
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} muss eine Zahl sein, ist aber {raw!r}") from None


def _text(name: str, default: str) -> str:
    # Erlaubt \n in .env-Werten für mehrzeilige Texte
    return (_get(name, default) or default).replace("\\n", "\n")


@dataclass(frozen=True)
class Texts:
    disclosure: str = DEFAULT_DISCLOSURE
    paywall: str = DEFAULT_PAYWALL
    payment_confirm: str = DEFAULT_PAYMENT_CONFIRM
    stop: str = DEFAULT_STOP
    start: str = DEFAULT_START
    balance: str = DEFAULT_BALANCE
    delete_warn: str = DEFAULT_DELETE_WARN
    delete_done: str = DEFAULT_DELETE_DONE


@dataclass(frozen=True)
class Config:
    database_url: str
    inbound_token: str
    gateway_url: str
    gateway_token: str
    persona: str

    redis_url: str | None = None
    http_host: str = "0.0.0.0"
    http_port: int = 8090

    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_max_tokens: int = 400
    llm_temperature: float = 0.9
    fact_model: str = "gpt-4o-mini"
    fact_every: int = 6
    fact_window: int = 20

    history_limit: int = 30
    debounce_seconds: float = 3.0
    free_credits: int = 20

    paywall_product_id: str | None = None
    offer_cooldown_hours: float = 12.0
    products_file: str | None = None

    payment_provider: str = "stripe"
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    payment_success_url: str | None = None
    payment_cancel_url: str | None = None
    public_base_url: str | None = None

    texts: Texts = field(default_factory=Texts)
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        persona_file = _require("PERSONA_FILE")
        try:
            persona = Path(persona_file).read_text("utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"PERSONA_FILE {persona_file} nicht lesbar: {exc}") from None
        if not persona:
            raise ConfigError("PERSONA_FILE ist leer.")

        texts = Texts(
            disclosure=_text("TEXT_DISCLOSURE", DEFAULT_DISCLOSURE),
            paywall=_text("TEXT_PAYWALL", DEFAULT_PAYWALL),
            payment_confirm=_text("TEXT_PAYMENT_CONFIRM", DEFAULT_PAYMENT_CONFIRM),
            stop=_text("TEXT_STOP", DEFAULT_STOP),
            start=_text("TEXT_START", DEFAULT_START),
            balance=_text("TEXT_BALANCE", DEFAULT_BALANCE),
            delete_warn=_text("TEXT_DELETE_WARN", DEFAULT_DELETE_WARN),
            delete_done=_text("TEXT_DELETE_DONE", DEFAULT_DELETE_DONE),
        )
        if "{link}" not in texts.paywall:
            raise ConfigError("TEXT_PAYWALL muss den Platzhalter {link} enthalten.")

        llm_model = _get("LLM_MODEL", "gpt-4o-mini")
        cfg = cls(
            database_url=_require("DATABASE_URL"),
            inbound_token=_require("INBOUND_TOKEN"),
            gateway_url=_require("GATEWAY_URL").rstrip("/"),
            gateway_token=_require("GATEWAY_TOKEN"),
            persona=persona,
            redis_url=_get("REDIS_URL"),
            http_host=_get("HTTP_HOST", "0.0.0.0"),
            http_port=_number("HTTP_PORT", "8090", int),
            llm_api_key=_get("LLM_API_KEY") or _get("OPENAI_API_KEY"),
            llm_base_url=_get("LLM_BASE_URL"),
            llm_model=llm_model,
            llm_max_tokens=_number("LLM_MAX_TOKENS", "400", int),
            llm_temperature=_number("LLM_TEMPERATURE", "0.9", float),
            fact_model=_get("FACT_MODEL", llm_model),
            fact_every=_number("FACT_EVERY", "6", int),
            fact_window=_number("FACT_WINDOW", "20", int),
            history_limit=_number("HISTORY_LIMIT", "30", int),
            debounce_seconds=_number("DEBOUNCE_SECONDS", "3", float),
            free_credits=_number("FREE_CREDITS", "20", int),
            paywall_product_id=_get("PAYWALL_PRODUCT_ID"),
            offer_cooldown_hours=_number("OFFER_COOLDOWN_HOURS", "12", float),
            products_file=_get("PRODUCTS_FILE"),
            payment_provider=(_get("PAYMENT_PROVIDER", "stripe") or "stripe").lower(),
            stripe_secret_key=_get("STRIPE_SECRET_KEY"),
            stripe_webhook_secret=_get("STRIPE_WEBHOOK_SECRET"),
            payment_success_url=_get("PAYMENT_SUCCESS_URL"),
            payment_cancel_url=_get("PAYMENT_CANCEL_URL"),
            public_base_url=(_get("PUBLIC_BASE_URL") or "").rstrip("/") or None,
            texts=texts,
            log_level=(_get("LOG_LEVEL", "INFO") or "INFO").upper(),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.llm_api_key:
            raise ConfigError("LLM_API_KEY (oder OPENAI_API_KEY) muss gesetzt sein.")
        if self.free_credits < 0:
            raise ConfigError("FREE_CREDITS darf nicht negativ sein.")
        if self.fact_every < 1:
            raise ConfigError("FACT_EVERY muss mindestens 1 sein.")
        if self.payment_provider == "stripe":
            missing = [
                name
                for name, value in (
                    ("STRIPE_SECRET_KEY", self.stripe_secret_key),
                    ("STRIPE_WEBHOOK_SECRET", self.stripe_webhook_secret),
                    ("PAYMENT_SUCCESS_URL", self.payment_success_url),
                )
                if not value
            ]
            if missing:
                raise ConfigError(f"Für PAYMENT_PROVIDER=stripe fehlen: {', '.join(missing)}")
        elif self.payment_provider == "dummy":
            if not self.public_base_url:
                raise ConfigError("Für PAYMENT_PROVIDER=dummy muss PUBLIC_BASE_URL gesetzt sein.")
        else:
            raise ConfigError(f"Unbekannter PAYMENT_PROVIDER {self.payment_provider!r} (stripe oder dummy).")
