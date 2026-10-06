"""Einmaliger interaktiver Login. Fragt Telefonnummer, Code und ggf. 2FA-Passwort ab
und legt die Session-Datei an, die der Service danach verwendet."""

import asyncio
from pathlib import Path

from telethon import TelegramClient

from gateway.config import Config


async def main() -> None:
    cfg = Config.from_env(require_api_token=False)
    Path(cfg.session_path).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(cfg.session_path, cfg.api_id, cfg.api_hash)
    await client.start()
    me = await client.get_me()
    print(f"Eingeloggt als {me.first_name} (@{me.username}), id {me.id}")
    print(f"Session gespeichert unter {cfg.session_path}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
