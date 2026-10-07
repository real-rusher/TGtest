"""Kern des Gateways: Telegram-Events rein, Sendeaufträge raus."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable

from telethon import errors, events

from .config import Config
from .models import IncomingMessage, SendJob
from .pauselist import PauseList

log = logging.getLogger("gateway")

InboundHandler = Callable[[IncomingMessage], Awaitable[None]]

COMMAND_RE = re.compile(r"^/(pause|resume)$", re.IGNORECASE)

# Offizieller Telegram-Servicekontakt (Login-Codes, Sicherheitshinweise).
# Diese Nachrichten dürfen niemals an das System weitergereicht werden.
TELEGRAM_SERVICE_ID = 777000


class GatewayError(Exception):
    pass


class UserPaused(GatewayError):
    def __init__(self, user_id: int) -> None:
        super().__init__(f"Chat {user_id} ist pausiert")
        self.user_id = user_id


class InvalidFile(GatewayError):
    """Datei fehlt oder liegt außerhalb des Content-Ordners."""


def resolve_content_path(content_dir: str, name: Any) -> Path:
    """Löst einen relativen Dateinamen sicher innerhalb von content_dir auf."""
    if not isinstance(name, str) or not name.strip():
        raise InvalidFile("file muss ein nicht leerer Dateiname sein")
    base = Path(content_dir).resolve()
    path = (base / name).resolve()
    if base != path and base not in path.parents:
        raise InvalidFile("file muss innerhalb des Content-Ordners liegen")
    if not path.is_file():
        raise InvalidFile(f"Datei nicht gefunden: {name}")
    return path


class UnknownUser(GatewayError):
    def __init__(self, user_id: int) -> None:
        super().__init__(
            f"User {user_id} ist dem Account unbekannt (noch nie geschrieben?)"
        )
        self.user_id = user_id


class TelegramGateway:
    def __init__(
        self,
        client: Any,
        config: Config,
        pauses: PauseList,
        on_message: InboundHandler,
    ) -> None:
        self._client = client
        self._cfg = config
        self._pauses = pauses
        self._on_message = on_message

        # Ein Lock pro Chat: Sendeaufträge und eingehende Events bleiben in Reihenfolge.
        self._out_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._in_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._active: defaultdict[int, set[asyncio.Task]] = defaultdict(set)
        self._background: set[asyncio.Task] = set()

    @property
    def pauses(self) -> PauseList:
        return self._pauses

    # ------------------------------------------------------------------ Setup

    def register(self) -> None:
        self._client.add_event_handler(
            self._on_command,
            events.NewMessage(outgoing=True, func=self._is_command),
        )
        self._client.add_event_handler(
            self._on_incoming,
            events.NewMessage(incoming=True, func=lambda e: e.is_private),
        )

    async def shutdown(self) -> None:
        tasks = list(self._background)
        for user_tasks in self._active.values():
            tasks.extend(user_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------- Admin-Steuerung

    @staticmethod
    def _is_command(event: Any) -> bool:
        return bool(event.is_private and COMMAND_RE.match((event.raw_text or "").strip()))

    async def _on_command(self, event: Any) -> None:
        user_id = event.chat_id
        command = COMMAND_RE.match(event.raw_text.strip()).group(1).lower()

        # Befehl für beide Seiten löschen, der Chatpartner soll ihn nie sehen.
        try:
            await event.delete()
        except errors.RPCError as exc:
            log.warning("Befehl in Chat %s konnte nicht gelöscht werden: %s", user_id, exc)

        if command == "pause":
            self._pauses.pause(user_id)
            cancelled = self.cancel_outbound(user_id)
            log.info(
                "Chat %s pausiert (%d laufende Sendeaufträge abgebrochen)",
                user_id,
                cancelled,
            )
        else:
            self._pauses.resume(user_id)
            log.info("Chat %s fortgesetzt", user_id)

    def cancel_outbound(self, user_id: int) -> int:
        tasks = list(self._active.get(user_id, ()))
        for task in tasks:
            task.cancel()
        return len(tasks)

    # ------------------------------------------------------------- Eingang

    async def _on_incoming(self, event: Any) -> None:
        text = event.raw_text
        if not text or not text.strip():
            return  # Medien ohne Text, Sticker, Servicenachrichten

        user_id = event.sender_id
        if user_id is None or user_id == TELEGRAM_SERVICE_ID:
            return
        if self._pauses.is_paused(user_id):
            return

        sender = await event.get_sender()
        if sender is None or any(
            getattr(sender, flag, False) for flag in ("bot", "is_self", "deleted", "support")
        ):
            return

        message = IncomingMessage(
            user_id=user_id,
            username=getattr(sender, "username", None),
            first_name=getattr(sender, "first_name", None),
            message_text=text,
            timestamp=int(event.date.timestamp()),
        )
        self._spawn(self._dispatch(message))

    async def _dispatch(self, message: IncomingMessage) -> None:
        async with self._in_locks[message.user_id]:
            # Falls während der Warteschlange pausiert wurde.
            if self._pauses.is_paused(message.user_id):
                return
            try:
                await self._on_message(message)
            except Exception:
                log.exception("Weiterleitung für User %s fehlgeschlagen", message.user_id)

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # ------------------------------------------------------------- Ausgang

    def submit(self, job: SendJob) -> asyncio.Task:
        """Startet einen Sendeauftrag im Hintergrund und gibt den Task zurück."""
        self._ensure_not_paused(job.user_id)
        task = asyncio.ensure_future(self.send_natural_chunks(job))
        self._active[job.user_id].add(task)
        task.add_done_callback(lambda t: self._finish(job.user_id, t))
        return task

    def _finish(self, user_id: int, task: asyncio.Task) -> None:
        self._discard(user_id, task)
        if task.cancelled():
            log.info("Sendeauftrag für %s abgebrochen", user_id)
        elif task.exception() is not None:
            log.error("Sendeauftrag für %s fehlgeschlagen: %r", user_id, task.exception())

    def _discard(self, user_id: int, task: asyncio.Task) -> None:
        tasks = self._active.get(user_id)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                self._active.pop(user_id, None)

    def _ensure_not_paused(self, user_id: int) -> None:
        if self._cfg.pause_blocks_outbound and self._pauses.is_paused(user_id):
            raise UserPaused(user_id)

    async def send_natural_chunks(self, job: SendJob) -> int:
        """Tipp-Status setzen, exakt `delay` Sekunden warten, Chunk senden.

        Gibt die Anzahl gesendeter Chunks zurück.
        """
        user_id = job.user_id
        self._ensure_not_paused(user_id)
        task = asyncio.current_task()
        if task is not None:
            self._active[user_id].add(task)
        try:
            async with self._out_locks[user_id]:
                self._ensure_not_paused(user_id)
                entity = await self._resolve(user_id)
                if self._cfg.auto_read:
                    await self._mark_read(entity)

                sent = 0
                for chunk, delay in zip(job.chunks, job.delays):
                    await self._type_for(entity, delay)
                    await self._send_text(entity, chunk)
                    sent += 1
                log.info("%d Chunks an %s gesendet", sent, user_id)
                return sent
        finally:
            if task is not None:
                self._discard(user_id, task)

    async def send_file(self, user_id: int, file: str, caption: str = "") -> None:
        """Sendet eine Datei aus CONTENT_DIR, in Reihenfolge mit den Textaufträgen."""
        path = resolve_content_path(self._cfg.content_dir, file)
        self._ensure_not_paused(user_id)
        async with self._out_locks[user_id]:
            self._ensure_not_paused(user_id)
            entity = await self._resolve(user_id)
            for attempt in range(3):
                try:
                    await self._client.send_file(entity, str(path), caption=caption or None)
                    return
                except errors.FloodWaitError as exc:
                    if exc.seconds > self._cfg.max_flood_wait or attempt == 2:
                        raise
                    await asyncio.sleep(exc.seconds + 1)

    async def _type_for(self, entity: Any, seconds: float) -> None:
        # Telethon frischt den Status alle 4 s auf, Telegram zeigt ihn nur ~5 s an.
        try:
            async with self._client.action(entity, "typing", auto_cancel=False):
                await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._client.action(entity, "cancel")
            raise

    async def _send_text(self, entity: Any, text: str) -> None:
        for attempt in range(3):
            try:
                await self._client.send_message(
                    entity,
                    text,
                    parse_mode=None,  # Text 1:1, Unterstriche in Links bleiben heil
                    link_preview=self._cfg.link_preview,
                )
                return
            except errors.FloodWaitError as exc:
                if exc.seconds > self._cfg.max_flood_wait or attempt == 2:
                    raise
                log.warning("FloodWait %ss, warte und versuche erneut", exc.seconds)
                await asyncio.sleep(exc.seconds + 1)

    async def _mark_read(self, entity: Any) -> None:
        try:
            await self._client.send_read_acknowledge(entity)
        except errors.RPCError as exc:
            log.debug("Lesebestätigung fehlgeschlagen: %s", exc)

    async def _resolve(self, user_id: int) -> Any:
        try:
            return await self._client.get_input_entity(user_id)
        except ValueError:
            pass
        # Entity nicht im Session-Cache: Dialogliste einmal laden und erneut versuchen.
        await self._client.get_dialogs()
        try:
            return await self._client.get_input_entity(user_id)
        except ValueError:
            raise UnknownUser(user_id) from None
