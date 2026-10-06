import json

import pytest

from orchestrator.config import (
    DEFAULT_DISCLOSURE,
    DEFAULT_SHOP_DELETE_WARN,
    Config,
    ConfigError,
)

BASE = {
    "DATABASE_URL": "postgresql://x",
    "INBOUND_TOKEN": "a",
    "GATEWAY_URL": "http://gw/",
    "GATEWAY_TOKEN": "b",
    "LLM_API_KEY": "k",
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    for key in list(__import__("os").environ):
        if key.startswith(("TEXT_", "BOT_", "SHOP_", "STRIPE_", "PAYMENT_", "CATALOG_", "PUBLIC_", "DAILY_")):
            monkeypatch.delenv(key, raising=False)
    persona = tmp_path / "persona.md"
    persona.write_text("Du bist Test.", "utf-8")
    catalog = tmp_path / "katalog.json"
    catalog.write_text(json.dumps([{"id": "a", "name": "A", "url": "https://x"}]), "utf-8")
    values = {**BASE, "PERSONA_FILE": str(persona)}

    def apply(**extra):
        for k, v in {**values, **extra}.items():
            monkeypatch.setenv(k, v)
        return Config.from_env()

    apply.catalog = str(catalog)
    return apply


def test_chat_modus_standard(env):
    cfg = env(PAYMENT_PROVIDER="dummy", PUBLIC_BASE_URL="https://pub.test/")
    assert cfg.mode == "chat" and cfg.gateway_url == "http://gw"
    assert cfg.texts.disclosure == DEFAULT_DISCLOSURE  # leerer TEXT_DISCLOSURE = Standardtext
    assert cfg.public_base_url == "https://pub.test"


def test_leere_texte_nutzen_standard(env):
    cfg = env(PAYMENT_PROVIDER="dummy", PUBLIC_BASE_URL="https://p", TEXT_DISCLOSURE="  ", TEXT_PAYWALL="")
    assert cfg.texts.disclosure == DEFAULT_DISCLOSURE and "{link}" in cfg.texts.paywall


def test_eigene_texte_mit_zeilenumbruch(env):
    cfg = env(PAYMENT_PROVIDER="dummy", PUBLIC_BASE_URL="https://p", TEXT_DISCLOSURE="Ich bin eine KI.\\nViel Spaß!")
    assert cfg.texts.disclosure == "Ich bin eine KI.\nViel Spaß!"


def test_chat_modus_pruefungen(env):
    with pytest.raises(ConfigError, match="STRIPE_SECRET_KEY"):
        env()
    with pytest.raises(ConfigError, match="PUBLIC_BASE_URL"):
        env(PAYMENT_PROVIDER="dummy")
    with pytest.raises(ConfigError, match="link"):
        env(PAYMENT_PROVIDER="dummy", PUBLIC_BASE_URL="https://p", TEXT_PAYWALL="Zahl mal")
    with pytest.raises(ConfigError, match="BOT_MODE"):
        env(BOT_MODE="irgendwas")


def test_shop_modus(env):
    cfg = env(BOT_MODE="SHOP", SHOP_NAME="Nordwind", CATALOG_FILE=env.catalog, LINK_QUERY="ref=tg",
              DAILY_REPLY_LIMIT="150")
    assert cfg.mode == "shop" and cfg.shop_name == "Nordwind"
    assert "KI-Assistent von Nordwind" in cfg.texts.disclosure
    assert cfg.texts.delete_warn == DEFAULT_SHOP_DELETE_WARN
    assert cfg.daily_reply_limit == 150 and cfg.link_query == "ref=tg"


def test_shop_modus_pruefungen(env):
    with pytest.raises(ConfigError, match="SHOP_NAME, CATALOG_FILE"):
        env(BOT_MODE="shop")
    cfg = env(BOT_MODE="shop", SHOP_NAME="X", CATALOG_FILE=env.catalog, TEXT_PAYWALL="ohne Link geht im Shop")
    assert cfg.mode == "shop"
    with pytest.raises(ConfigError, match="DAILY_REPLY_LIMIT"):
        env(BOT_MODE="shop", SHOP_NAME="X", CATALOG_FILE=env.catalog, DAILY_REPLY_LIMIT="-1")


def test_persona_pflicht(env, monkeypatch, tmp_path):
    monkeypatch.setenv("PERSONA_FILE", str(tmp_path / "fehlt.md"))
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ConfigError, match="PERSONA_FILE"):
        Config.from_env()
