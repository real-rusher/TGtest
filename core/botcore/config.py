"""Konfiguration des Bot-Cores.

Zwei Quellen, klar getrennt:
  * Umgebungsvariablen (.env):  Geheimnisse und Infrastruktur (API-Keys, Tokens, Datenbank)
  * config/bot.yaml:            Verhalten des Bots (Persona, Kennzeichnung, Zahlungen, Produkte)

Alle Fehler werden beim Start mit einer verständlichen Meldung gemeldet (ConfigError),
damit Fehlkonfigurationen nicht erst mitten im Betrieb auffallen.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(RuntimeError):
    """Fehlende oder ungültige Konfiguration."""


PAYMENT_METHODS = ("none", "stars", "stripe", "link")
DISCLOSURE_MODES = ("off", "first_message")
TRANSPORTS = ("bot", "userbot")
PROVIDERS = ("anthropic", "openai")
PRODUCT_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


# --------------------------------------------------------------------------- Umgebungsvariablen


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on", "ja"}


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    api_key: str
    model: str
    base_url: str | None = None
    facts_model: str | None = None  # eigenes (günstigeres) Modell für die Fakten-Extraktion
    effort: str = "low"  # nur Anthropic: low | medium | high
    fallbacks: bool = True  # nur Anthropic: bei Ablehnung automatisch anderes Modell nutzen
    max_tokens: int = 8000  # enthält bei Claude auch das interne Nachdenken


@dataclass(frozen=True)
class Settings:
    """Alles aus der .env-Datei."""

    llm: LLMSettings
    database_url: str
    redis_url: str | None
    transport: str
    transport_url: str
    internal_token: str
    admin_token: str | None
    payment_webhook_token: str | None
    stripe_webhook_secret: str | None
    config_file: str
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        provider = (_env("LLM_PROVIDER", "anthropic") or "").lower()
        if provider not in PROVIDERS:
            raise ConfigError(f"LLM_PROVIDER muss einer von {PROVIDERS} sein, ist aber {provider!r}")
        api_key = _env("LLM_API_KEY")
        base_url = _env("LLM_BASE_URL")
        if not api_key:
            if provider == "openai" and base_url:
                api_key = "nicht-benoetigt"  # lokale Server (z. B. Ollama) brauchen keinen Key
            else:
                raise ConfigError("LLM_API_KEY fehlt. Trage den API-Key deines Sprachmodell-Anbieters in die .env ein.")
        model = _env("LLM_MODEL")
        if not model:
            if provider == "anthropic":
                model = "claude-opus-5-5"
            else:
                raise ConfigError("LLM_MODEL fehlt. Bei LLM_PROVIDER=openai muss der Modellname gesetzt sein.")
        effort = (_env("LLM_EFFORT", "low") or "low").lower()
        if effort not in ("low", "medium", "high"):
            raise ConfigError("LLM_EFFORT muss low, medium oder high sein")
        try:
            max_tokens = int(_env("LLM_MAX_TOKENS", "8000"))
        except ValueError:
            raise ConfigError("LLM_MAX_TOKENS muss eine Zahl sein") from None

        transport = (_env("TELEGRAM_MODE", "bot") or "").lower()
        if transport not in TRANSPORTS:
            raise ConfigError(f"TELEGRAM_MODE muss 'bot' oder 'userbot' sein, ist aber {transport!r}")

        internal_token = _env("INTERNAL_TOKEN")
        if not internal_token:
            raise ConfigError("INTERNAL_TOKEN fehlt (z. B. mit `openssl rand -hex 32` erzeugen).")
        database_url = _env("DATABASE_URL")
        if not database_url:
            raise ConfigError("DATABASE_URL fehlt.")
        try:
            port = int(_env("HTTP_PORT", "8000"))
        except ValueError:
            raise ConfigError("HTTP_PORT muss eine Zahl sein") from None

        return cls(
            llm=LLMSettings(
                provider=provider,
                api_key=api_key,
                model=model,
                base_url=base_url,
                facts_model=_env("FACTS_MODEL"),
                effort=effort,
                fallbacks=_env_bool("LLM_FALLBACKS", True),
                max_tokens=max_tokens,
            ),
            database_url=database_url,
            redis_url=_env("REDIS_URL"),
            transport=transport,
            transport_url=(_env("TRANSPORT_URL", "http://telegram:8080") or "").rstrip("/"),
            internal_token=internal_token,
            admin_token=_env("ADMIN_TOKEN"),
            payment_webhook_token=_env("PAYMENT_WEBHOOK_TOKEN"),
            stripe_webhook_secret=_env("STRIPE_WEBHOOK_SECRET"),
            config_file=_env("CONFIG_FILE", "config/bot.yaml"),
            http_host=_env("HTTP_HOST", "0.0.0.0"),
            http_port=port,
            log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        )


# --------------------------------------------------------------------------- bot.yaml


@dataclass(frozen=True)
class Delivery:
    """Ein Bestandteil der Auslieferung: Datei aus config/content oder Text."""

    file: str | None = None
    text: str | None = None
    caption: str = ""


@dataclass(frozen=True)
class Product:
    id: str
    title: str
    description: str = ""
    price_label: str = ""  # Anzeigepreis, z. B. "9,99 €" (für KI und Angebotstext)
    price_stars: int | None = None  # Preis in Telegram Stars (nur Zahlungsart stars)
    stripe_link: str | None = None  # Stripe Payment Link (nur Zahlungsart stripe)
    link: str | None = None  # beliebiger Bezahllink (nur Zahlungsart link)
    deliver: tuple[Delivery, ...] = ()
    thank_you: str = ""


@dataclass(frozen=True)
class BotConfig:
    persona: str
    name: str = "Bot"
    start_message: str = ""
    reply_delay_seconds: float = 4.0
    typing_speed: float = 1.0
    max_delay_seconds: float = 20.0
    history_messages: int = 30
    disclosure_mode: str = "first_message"
    disclosure_text: str = "Kurzer Hinweis: Du schreibst hier mit einem KI-Assistenten."
    memory_enabled: bool = True
    memory_extract_every: int = 4
    payment_method: str = "none"
    offer_cooldown_hours: float = 168.0
    offer_text: str = "{title} ({price})\n{link}"
    products: tuple[Product, ...] = ()

    def product(self, product_id: str) -> Product | None:
        return next((p for p in self.products if p.id == product_id), None)

    @property
    def sells(self) -> bool:
        return self.payment_method != "none" and bool(self.products)


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"bot.yaml: '{key}' muss ein Abschnitt (Schlüssel: Wert) sein")
    return value


def _num(section: dict[str, Any], key: str, default: float, *, where: str, minimum: float = 0) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ConfigError(f"bot.yaml: {where}.{key} muss eine Zahl >= {minimum} sein")
    return float(value)


def _text(value: Any, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ConfigError(f"bot.yaml: {where} muss ein Text sein")
    return str(value).strip()


def _parse_product(raw: Any, index: int, method: str, content_dir: Path | None) -> Product:
    where = f"products[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"bot.yaml: {where} muss ein Abschnitt sein")
    pid = _text(raw.get("id"), f"{where}.id")
    if not PRODUCT_ID_RE.match(pid):
        raise ConfigError(f"bot.yaml: {where}.id {pid!r} darf nur Buchstaben, Ziffern und _ enthalten (max. 64)")
    title = _text(raw.get("title"), f"{where}.title")
    if not title:
        raise ConfigError(f"bot.yaml: {where}.title fehlt")

    price_stars = raw.get("price_stars")
    if price_stars is not None and (isinstance(price_stars, bool) or not isinstance(price_stars, int) or price_stars < 1):
        raise ConfigError(f"bot.yaml: {where}.price_stars muss eine ganze Zahl >= 1 sein")

    deliver: list[Delivery] = []
    for j, item in enumerate(raw.get("deliver") or []):
        w = f"{where}.deliver[{j}]"
        if not isinstance(item, dict) or not (item.get("file") or item.get("text")):
            raise ConfigError(f"bot.yaml: {w} braucht 'file' oder 'text'")
        file = _text(item.get("file"), f"{w}.file") or None
        if file and content_dir is not None:
            base = content_dir.resolve()
            path = (base / file).resolve()
            if base not in path.parents or not path.is_file():
                raise ConfigError(f"bot.yaml: {w}.file {file!r} liegt nicht in {content_dir}")
        deliver.append(Delivery(file=file, text=_text(item.get("text"), f"{w}.text") or None,
                                caption=_text(item.get("caption"), f"{w}.caption")))

    product = Product(
        id=pid,
        title=title,
        description=_text(raw.get("description"), f"{where}.description"),
        price_label=_text(raw.get("price_label"), f"{where}.price_label"),
        price_stars=price_stars,
        stripe_link=_text(raw.get("stripe_link"), f"{where}.stripe_link") or None,
        link=_text(raw.get("link"), f"{where}.link") or None,
        deliver=tuple(deliver),
        thank_you=_text(raw.get("thank_you"), f"{where}.thank_you"),
    )
    if method == "stars" and product.price_stars is None:
        raise ConfigError(f"bot.yaml: {where} braucht price_stars (Zahlungsart stars)")
    if method == "stripe" and not product.stripe_link:
        raise ConfigError(f"bot.yaml: {where} braucht stripe_link (Zahlungsart stripe)")
    if method == "link" and not product.link:
        raise ConfigError(f"bot.yaml: {where} braucht link (Zahlungsart link)")
    return product


def load_bot_config(path: str | Path, *, transport: str = "bot", content_dir: str | Path | None = None) -> BotConfig:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigError(f"Konfigurationsdatei {path} nicht gefunden") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} ist kein gültiges YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: oberste Ebene muss aus Abschnitten bestehen")

    persona = _text(data.get("persona"), "persona")
    if not persona:
        raise ConfigError("bot.yaml: 'persona' fehlt. Beschreibe dort, wer der Bot ist und wie er schreibt.")

    bot = _section(data, "bot")
    style = _section(data, "style")
    disclosure = _section(data, "disclosure")
    memory = _section(data, "memory")
    payments = _section(data, "payments")

    disclosure_mode = _text(disclosure.get("mode", "first_message"), "disclosure.mode").lower()
    if disclosure_mode not in DISCLOSURE_MODES:
        raise ConfigError(f"bot.yaml: disclosure.mode muss einer von {DISCLOSURE_MODES} sein")

    method = _text(payments.get("method", "none"), "payments.method").lower()
    if method not in PAYMENT_METHODS:
        raise ConfigError(f"bot.yaml: payments.method muss einer von {PAYMENT_METHODS} sein")
    if method == "stars" and transport != "bot":
        raise ConfigError("Telegram Stars funktionieren nur mit TELEGRAM_MODE=bot (Bot-Account).")

    max_delay = _num(style, "max_delay_seconds", 20, where="style")
    if max_delay > 120:
        raise ConfigError("bot.yaml: style.max_delay_seconds darf höchstens 120 sein")

    content = Path(content_dir) if content_dir is not None else None
    raw_products = data.get("products") or []
    if not isinstance(raw_products, list):
        raise ConfigError("bot.yaml: 'products' muss eine Liste sein")
    products = tuple(_parse_product(p, i, method, content) for i, p in enumerate(raw_products))
    ids = [p.id for p in products]
    if len(ids) != len(set(ids)):
        raise ConfigError("bot.yaml: Produkt-IDs müssen eindeutig sein")

    disclosure_text = _text(disclosure.get("text"), "disclosure.text") or BotConfig.disclosure_text
    return BotConfig(
        persona=persona,
        name=_text(bot.get("name"), "bot.name") or "Bot",
        start_message=_text(data.get("start_message"), "start_message"),
        reply_delay_seconds=_num(style, "reply_delay_seconds", 4, where="style"),
        typing_speed=_num(style, "typing_speed", 1.0, where="style"),
        max_delay_seconds=max_delay,
        history_messages=int(_num(style, "history_messages", 30, where="style", minimum=2)),
        disclosure_mode=disclosure_mode,
        disclosure_text=disclosure_text,
        memory_enabled=bool(memory.get("enabled", True)),
        memory_extract_every=int(_num(memory, "extract_every", 4, where="memory", minimum=1)),
        payment_method=method,
        offer_cooldown_hours=_num(payments, "offer_cooldown_hours", 168, where="payments"),
        offer_text=_text(payments.get("offer_text"), "payments.offer_text") or BotConfig.offer_text,
        products=products,
    )
