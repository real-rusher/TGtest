"""Startet den Orchestrator: python -m orchestrator"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from aiohttp import web
from openai import AsyncOpenAI

from botdb import BotRepository

from .app import build_app
from .catalog import Catalog, CatalogError
from .config import Config, ConfigError
from .conversation import ConversationManager
from .facts import FactExtractor
from .gateway_client import GatewayClient
from .llm import ResponseGenerator
from .payments import PaymentProvider
from .payments.dummy import DummyProvider
from .payments.stripe import StripeProvider

log = logging.getLogger("orchestrator")


def make_payment_provider(cfg: Config) -> PaymentProvider:
    if cfg.payment_provider == "dummy":
        return DummyProvider(cfg.public_base_url)
    return StripeProvider(
        cfg.stripe_secret_key,
        cfg.stripe_webhook_secret,
        cfg.payment_success_url,
        cfg.payment_cancel_url,
    )


async def seed_products(repo: BotRepository, path: str) -> None:
    """Pakete aus einer JSON-Datei anlegen oder aktualisieren."""
    items = json.loads(Path(path).read_text("utf-8"))
    if not isinstance(items, list):
        raise ConfigError(f"{path} muss eine JSON-Liste enthalten")
    for item in items:
        try:
            await repo.upsert_product(
                str(item["product_id"]),
                str(item["title"]),
                int(item["price_cents"]),
                int(item["credits"]),
                currency=str(item.get("currency", "EUR")),
                description=str(item.get("description", "")),
                is_active=bool(item.get("is_active", True)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"Ungültiges Paket in {path}: {item!r} ({exc})") from None
    log.info("%d Pakete aus %s übernommen", len(items), path)


def load_catalog(cfg: Config) -> Catalog:
    try:
        catalog = Catalog.from_file(cfg.catalog_file, link_query=cfg.link_query)
    except CatalogError as exc:
        raise ConfigError(str(exc)) from None
    log.info("Katalog mit %d Produkten geladen", len(catalog))
    return catalog


async def run(cfg: Config) -> int:
    log.info("Modus: %s", cfg.mode)
    catalog = load_catalog(cfg) if cfg.mode == "shop" else None
    repo = await BotRepository.connect(cfg.database_url, cfg.redis_url)

    payments: PaymentProvider | None = None
    if cfg.mode == "chat":
        if cfg.products_file:
            await seed_products(repo, cfg.products_file)
        if not await repo.list_active_products():
            log.warning("Keine aktiven Pakete: Wer kein Guthaben mehr hat, bekommt keinen Zahlungslink.")
        payments = make_payment_provider(cfg)

    llm = AsyncOpenAI(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url)
    gateway = GatewayClient(cfg.gateway_url, cfg.gateway_token)
    await gateway.start()

    manager = ConversationManager(
        repo,
        gateway,
        ResponseGenerator(
            llm,
            cfg.llm_model,
            cfg.persona,
            max_tokens=cfg.llm_max_tokens,
            temperature=cfg.llm_temperature,
            catalog=catalog,
            shop_name=cfg.shop_name,
            catalog_prompt_limit=cfg.catalog_prompt_limit,
        ),
        FactExtractor(llm, cfg.fact_model),
        payments,
        cfg,
        catalog,
    )

    runner = web.AppRunner(build_app(manager, payments, repo, cfg), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.http_host, cfg.http_port).start()
    log.info("Orchestrator läuft auf %s:%s", cfg.http_host, cfg.http_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass
    await stop.wait()

    log.info("Fahre herunter ...")
    await runner.cleanup()
    await manager.shutdown()
    if payments is not None:
        await payments.close()
    await gateway.close()
    await llm.close()
    await repo.close()
    return 0


def main() -> None:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        sys.exit(asyncio.run(run(cfg)))
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
