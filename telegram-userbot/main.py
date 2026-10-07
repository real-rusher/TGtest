"""Startet das Telegram I/O Gateway als eigenständigen Service."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from aiohttp import web
from telethon import TelegramClient

from gateway import Config, ConfigError, PauseList, TelegramGateway
from gateway.http_bridge import WebhookForwarder, build_app, log_only_handler

log = logging.getLogger("gateway.main")


async def run(cfg: Config) -> int:
    Path(cfg.session_path).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        cfg.session_path,
        cfg.api_id,
        cfg.api_hash,
        connection_retries=None,  # bei Netzproblemen endlos neu verbinden
        retry_delay=5,
        auto_reconnect=True,
    )
    await client.connect()
    if not await client.is_user_authorized():
        log.error("Session ist nicht eingeloggt. Einmalig `python login.py` ausführen.")
        await client.disconnect()
        return 2

    me = await client.get_me()
    log.info("Eingeloggt als %s (id %s)", me.first_name, me.id)

    pauses = PauseList(cfg.pause_file)

    forwarder: WebhookForwarder | None = None
    if cfg.inbound_webhook_url:
        forwarder = WebhookForwarder(cfg.inbound_webhook_url, cfg.inbound_webhook_token)
        await forwarder.start()
        handler = forwarder
    else:
        log.warning("INBOUND_WEBHOOK_URL nicht gesetzt, Events werden nur geloggt.")
        handler = log_only_handler

    gateway = TelegramGateway(client, cfg, pauses, handler)
    gateway.register()

    runner = web.AppRunner(build_app(gateway, cfg, client), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.http_host, cfg.http_port).start()
    log.info("HTTP-Schnittstelle auf %s:%s", cfg.http_host, cfg.http_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass

    stop_task = asyncio.ensure_future(stop.wait())
    disconnected = asyncio.ensure_future(client.disconnected)
    done, _ = await asyncio.wait({stop_task, disconnected}, return_when=asyncio.FIRST_COMPLETED)
    exit_code = 0 if stop_task in done else 1
    if exit_code:
        log.error("Verbindung zu Telegram endgültig verloren, beende (Neustart durch Supervisor).")

    log.info("Fahre herunter ...")
    stop_task.cancel()
    await gateway.shutdown()
    await runner.cleanup()
    if forwarder is not None:
        await forwarder.close()
    await client.disconnect()
    return exit_code


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
    logging.getLogger("telethon").setLevel(logging.WARNING)
    sys.exit(asyncio.run(run(cfg)))


if __name__ == "__main__":
    main()
