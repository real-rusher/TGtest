"""Start: python -m tg_adapter"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from aiohttp import web

from .adapter import TelegramAdapter
from .api import make_app
from .config import Config


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("tg_adapter")
    cfg = Config.from_env()
    if not cfg.outbound_token and cfg.http_host not in ("127.0.0.1", "localhost"):
        log.warning("OUTBOUND_TOKEN fehlt: die /send-API ist ohne Passwort erreichbar!")

    os.makedirs(os.path.dirname(cfg.session_path) or ".", exist_ok=True)
    adapter = TelegramAdapter(cfg)
    await adapter.start()

    runner = web.AppRunner(make_app(adapter, cfg.outbound_token))
    await runner.setup()
    await web.TCPSite(runner, cfg.http_host, cfg.http_port).start()
    log.info("Outbound-API auf http://%s:%s", cfg.http_host, cfg.http_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    disconnected = asyncio.ensure_future(adapter.client.disconnected)
    await asyncio.wait([disconnected, asyncio.ensure_future(stop.wait())], return_when=asyncio.FIRST_COMPLETED)

    log.info("Fahre herunter")
    await runner.cleanup()
    await adapter.stop()


if __name__ == "__main__":
    asyncio.run(main())
