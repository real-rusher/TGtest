"""Telegram-Client-Adapter (Telethon, Bot-Account, nur private Chats).

Aufgaben
  * Inbound:   private Textnachrichten -> {"user_id", "text", "timestamp"} an die Sink
  * Outbound:  send_message_chunks() mit Tippen-Status und Delays pro Chunk
  * Zahlungen: Telegram Stars (pre_checkout_query + successful_payment)
  * Admin:     /pause <id>, /resume <id>, /paused sperren Inbound pro User
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Sequence

from telethon import TelegramClient, events, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.tl import functions, types

from .config import Config
from .sinks import Sink, make_sink
from .state import State

log = logging.getLogger(__name__)

TG_TEXT_LIMIT = 4096
TYPING_REFRESH = 4.0          # Telegram lässt "tippt..." nach ca. 5 s auslaufen
STARS_CURRENCY = "XTR"


class AdapterError(Exception):
    """Fehler, die der HTTP-Layer als 4xx an den Core zurückgibt."""


class UnknownUserError(AdapterError):
    pass


def split_text(text: str, limit: int = TG_TEXT_LIMIT) -> list[str]:
    """Teilt zu lange Chunks, bevorzugt an Zeilenumbrüchen oder Leerzeichen."""
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n ")
    if text:
        parts.append(text)
    return parts


def validate_outbound(user_id, text_chunks, delays) -> None:
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise AdapterError("user_id muss eine positive Ganzzahl sein")
    if not isinstance(text_chunks, list) or not isinstance(delays, list):
        raise AdapterError("text_chunks und delays müssen Listen sein")
    if not text_chunks:
        raise AdapterError("text_chunks ist leer")
    if len(text_chunks) != len(delays):
        raise AdapterError(
            f"text_chunks ({len(text_chunks)}) und delays ({len(delays)}) sind unterschiedlich lang"
        )
    for c in text_chunks:
        if not isinstance(c, str) or not c.strip():
            raise AdapterError("Jeder Chunk muss ein nicht leerer String sein")
    for d in delays:
        if isinstance(d, bool) or not isinstance(d, (int, float)) or d < 0:
            raise AdapterError("Jedes Delay muss eine Zahl >= 0 sein")


class TelegramAdapter:
    def __init__(
        self,
        config: Config,
        client: TelegramClient | None = None,
        state: State | None = None,
        inbound_sink: Sink | None = None,
        payment_sink: Sink | None = None,
    ):
        self.cfg = config
        self.client = client or TelegramClient(config.session_path, config.api_id, config.api_hash)
        self.state = state or State(config.state_path)
        self.inbound = inbound_sink or make_sink(config.inbound_url, config.inbound_token, "inbound")
        self.payments = payment_sink or make_sink(config.payment_url, config.inbound_token, "payment")
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ------------------------------------------------------------------ Lifecycle
    async def start(self) -> None:
        await self.client.start(bot_token=self.cfg.bot_token)
        self.client.add_event_handler(
            self.on_message, events.NewMessage(incoming=True, func=lambda e: e.is_private)
        )
        self.client.add_event_handler(
            self.on_raw, events.Raw([types.UpdateBotPrecheckoutQuery, types.UpdateNewMessage])
        )
        me = await self.client.get_me()
        log.info("Verbunden als @%s (id %s), Admins: %s", me.username, me.id, sorted(self.cfg.admin_ids))
        if not self.cfg.admin_ids:
            log.warning("ADMIN_IDS ist leer: /pause und /resume sind deaktiviert")

    async def stop(self) -> None:
        await self.inbound.close()
        await self.payments.close()
        await self.client.disconnect()

    # ------------------------------------------------------------------ Inbound
    async def on_message(self, event) -> None:
        msg = event.message
        user_id = event.sender_id
        text = (msg.message or "").strip()
        if not user_id or not text:
            return  # Sticker, Medien ohne Bildunterschrift etc.

        if user_id in self.cfg.admin_ids and text.startswith("/"):
            if await self._handle_admin(event, text):
                return

        if self.state.is_paused(user_id):
            log.debug("User %s pausiert, Nachricht verworfen", user_id)
            return

        await self.inbound.emit(
            {"user_id": user_id, "text": text, "timestamp": int(msg.date.timestamp())}
        )

    async def _handle_admin(self, event, text: str) -> bool:
        """Gibt True zurück, wenn es ein Admin-Befehl war (wird dann nicht weitergeleitet)."""
        parts = text.split()
        cmd = parts[0].split("@", 1)[0].lower()
        if cmd not in ("/pause", "/resume", "/paused"):
            return False

        if cmd == "/paused":
            ids = sorted(self.state.paused)
            await event.reply("Pausiert: " + (", ".join(map(str, ids)) if ids else "niemand"))
            return True

        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await event.reply(f"Benutzung: {cmd} <user_id>")
            return True

        target = int(parts[1])
        if cmd == "/pause":
            changed = self.state.pause(target)
            reply = f"User {target} pausiert." if changed else f"User {target} war schon pausiert."
        else:
            changed = self.state.resume(target)
            reply = f"User {target} wieder aktiv." if changed else f"User {target} war nicht pausiert."
        log.info("Admin %s: %s %s", event.sender_id, cmd, target)
        await event.reply(reply)
        return True

    # ------------------------------------------------------------------ Outbound
    async def send_message_chunks(
        self, user_id: int, text_chunks: Sequence[str], delays: Sequence[float]
    ) -> int:
        """Sendet die Chunks nacheinander. Vor jedem Chunk: Tippen-Status setzen,
        Delay abwarten (Status wird alle 4 s erneuert), dann senden.
        Aufrufe für denselben User laufen strikt nacheinander, nie verschachtelt.
        Gibt die Zahl gesendeter Telegram-Nachrichten zurück."""
        text_chunks, delays = list(text_chunks), list(delays)
        validate_outbound(user_id, text_chunks, delays)
        peer = await self._resolve(user_id)

        sent = 0
        async with self._locks[user_id]:
            for chunk, delay in zip(text_chunks, delays):
                await self._type_for(peer, min(float(delay), self.cfg.max_delay))
                for part in split_text(chunk):
                    await self._call_with_flood_retry(self.client.send_message, peer, part)
                    sent += 1
        return sent

    async def _type_for(self, peer, seconds: float) -> None:
        await self._set_typing(peer)
        remaining = seconds
        while remaining > 0:
            step = min(TYPING_REFRESH, remaining)
            await asyncio.sleep(step)
            remaining -= step
            if remaining > 0:
                await self._set_typing(peer)

    async def _set_typing(self, peer) -> None:
        try:
            await self.client(functions.messages.SetTypingRequest(peer, types.SendMessageTypingAction()))
        except RPCError as e:  # Tippen-Status ist kosmetisch, nie den Versand blockieren
            log.debug("SetTyping fehlgeschlagen: %r", e)

    async def _resolve(self, user_id: int):
        try:
            return await self.client.get_input_entity(user_id)
        except (ValueError, TypeError):
            raise UnknownUserError(
                f"User {user_id} unbekannt: er muss dem Bot zuerst selbst geschrieben haben"
            ) from None

    @staticmethod
    async def _call_with_flood_retry(fn, *args, attempts: int = 3, **kwargs):
        for i in range(attempts):
            try:
                return await fn(*args, **kwargs)
            except FloodWaitError as e:
                if i == attempts - 1 or e.seconds > 300:
                    raise
                log.warning("FloodWait %ss, warte", e.seconds)
                await asyncio.sleep(e.seconds + 1)

    # ------------------------------------------------------------------ Zahlungen (Stars)
    async def on_raw(self, update) -> None:
        if isinstance(update, types.UpdateBotPrecheckoutQuery):
            await self.on_pre_checkout(update)
        elif isinstance(update, types.UpdateNewMessage):
            msg = update.message
            if isinstance(msg, types.MessageService) and isinstance(
                msg.action, types.MessageActionPaymentSentMe
            ):
                await self.on_successful_payment(msg)

    async def on_pre_checkout(self, q: types.UpdateBotPrecheckoutQuery) -> None:
        """Muss innerhalb von 10 s beantwortet werden, sonst bricht Telegram ab."""
        error = None
        if q.currency != STARS_CURRENCY:
            error = "Nur Telegram Stars werden akzeptiert."
        elif q.total_amount <= 0:
            error = "Ungültiger Betrag."
        elif not q.payload:
            error = "Ungültige Rechnung."

        if error:
            log.warning("Pre-Checkout abgelehnt (user %s): %s", q.user_id, error)
            await self.client(functions.messages.SetBotPrecheckoutResultsRequest(q.query_id, error=error))
        else:
            await self.client(functions.messages.SetBotPrecheckoutResultsRequest(q.query_id, success=True))
            log.info("Pre-Checkout ok: user %s, %s %s", q.user_id, q.total_amount, q.currency)

    async def on_successful_payment(self, msg: types.MessageService) -> None:
        action: types.MessageActionPaymentSentMe = msg.action
        user_id = utils.get_peer_id(msg.peer_id)
        charge_id = action.charge.id
        if not self.state.mark_charge(charge_id):
            log.info("Zahlung %s bereits verbucht, ignoriert", charge_id)
            return
        event = {
            "event": "successful_payment",
            "user_id": user_id,
            "currency": action.currency,
            "total_amount": action.total_amount,
            "payload": action.payload.decode("utf-8", "replace"),
            "charge_id": charge_id,
            "provider_charge_id": action.charge.provider_charge_id,
            "timestamp": int(msg.date.timestamp()) if msg.date else int(time.time()),
        }
        log.info("Zahlung erhalten: %s", event)
        # Zahlungen werden auch für pausierte User gemeldet, damit kein Geld verloren geht.
        await self.payments.emit(event)

    async def send_stars_invoice(
        self, user_id: int, title: str, description: str, payload: str, amount: int
    ) -> None:
        if not 1 <= len(title) <= 32:
            raise AdapterError("title: 1 bis 32 Zeichen")
        if not 1 <= len(description) <= 255:
            raise AdapterError("description: 1 bis 255 Zeichen")
        if not 1 <= len(payload.encode()) <= 128:
            raise AdapterError("payload: 1 bis 128 Bytes")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
            raise AdapterError("amount muss eine ganze Zahl >= 1 (Stars) sein")

        peer = await self._resolve(user_id)
        media = types.InputMediaInvoice(
            title=title,
            description=description,
            invoice=types.Invoice(currency=STARS_CURRENCY, prices=[types.LabeledPrice(title, amount)]),
            payload=payload.encode(),
            provider_data=types.DataJSON("{}"),
            provider="",
        )
        await self.client(functions.messages.SendMediaRequest(peer=peer, media=media, message=""))

    async def refund_stars(self, user_id: int, charge_id: str) -> None:
        peer = await self._resolve(user_id)
        await self.client(
            functions.payments.RefundStarsChargeRequest(utils.get_input_user(peer), charge_id)
        )
        log.info("Erstattet: user %s, charge %s", user_id, charge_id)
