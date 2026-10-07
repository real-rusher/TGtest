"""Start: python -m botcore"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

import asyncpg
from aiohttp import web

from botdb import BotRepository

from .config import ConfigError, Settings, load_bot_config
from .llm import make_llm
from .orchestrator import Orchestrator
from .server import build_app
from .transport import TransportClient

log = logging.getLogger("botcore")


async def run(settings: Settings) -> int:
    content_dir = os.environ.get("CONTENT_DIR", os.path.join(os.path.dirname(settings.config_file), "content"))
    bot_cfg = load_bot_config(settings.config_file, transport=settings.transport, content_dir=content_dir)
    log.info(
        "Bot %r: Telegram-Modus %s, Modell %s (%s), Zahlungsart %s, Kennzeichnung %s",
        bot_cfg.name, settings.transport, settings.llm.model, settings.llm.provider,
        bot_cfg.payment_method, bot_cfg.disclosure_mode,
    )

    repo = None
    for attempt in range(1, 31):  # Datenbank braucht beim ersten Start etwas
        try:
            repo = await BotRepository.connect(settings.database_url, settings.redis_url)
            break
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
            log.info("Datenbank noch nicht erreichbar (%s), Versuch %d/30", exc.__class__.__name__, attempt)
            await asyncio.sleep(2)
    if repo is None:
        log.error("Datenbank nicht erreichbar, beende.")
        return 1

    transport = TransportClient(settings.transport, settings.transport_url, settings.internal_token)
    orch = Orchestrator(
        bot_cfg, repo, make_llm(settings.llm), transport, facts_llm=make_llm(settings.llm, for_facts=True)
    )
    await orch.sync_products()

    runner = web.AppRunner(build_app(orch, settings), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, settings.http_host, settings.http_port).start()
    log.info("Core läuft auf %s:%s", settings.http_host, settings.http_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass
    await stop.wait()

    log.info("Fahre herunter ...")
    await runner.cleanup()
    await orch.shutdown()
    await transport.close()
    await repo.close()
    return 0


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        sys.exit(asyncio.run(run(settings)))
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
