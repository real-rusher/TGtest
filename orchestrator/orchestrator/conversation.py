"""Gesprächssteuerung: vom eingehenden Event bis zur verschickten Antwort.

Pro User läuft höchstens ein Worker. Nachrichten, die kurz hintereinander kommen,
werden gesammelt (Debounce) und mit einer einzigen Antwort beantwortet.

Ablauf für einen Stapel Nachrichten:
    1. User anlegen/aktualisieren (Gratis-Guthaben beim ersten Kontakt)
    2. Befehle (/stop, /start, /delete, /balance) direkt ausführen
    3. Textnachrichten im Verlauf speichern
    4. KI aus (/stop)?          -> nichts senden
    5. Erster Kontakt?          -> KI-Hinweis senden
    6. Kein Guthaben?           -> Zahlungslink (mit Cooldown), sonst still
    7. Antwort generieren, in Chunks zerlegen, über das Gateway senden
    8. Alle FACT_EVERY Nachrichten Fakten im Hintergrund extrahieren
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from botdb import BotRepository, Product

from .config import Config
from .facts import FactExtractor
from .gateway_client import GatewayClient, GatewayError, GatewayPaused
from .llm import GenerationError, ResponseGenerator
from .naturalize import naturalize_response
from .payments import PaymentEvent, PaymentProvider, format_price

log = logging.getLogger(__name__)

_COMMAND_RE = re.compile(r"^/(stop|start|delete|balance)(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE)
NOTICE_DELAY = 1.5  # Tipp-Dauer für Systemtexte (Hinweis, Zahlungslink, Befehle)


@dataclass(frozen=True, slots=True)
class Inbound:
    user_id: int
    username: str | None
    first_name: str | None
    text: str
    timestamp: int

    @classmethod
    def from_payload(cls, data: Any) -> "Inbound":
        if not isinstance(data, dict):
            raise ValueError("Event muss ein JSON-Objekt sein")
        user_id = data.get("user_id")
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ValueError("user_id muss eine Ganzzahl sein")
        text = data.get("message_text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("message_text fehlt oder ist leer")
        timestamp = data.get("timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise ValueError("timestamp muss eine Ganzzahl sein")

        def opt(key: str) -> str | None:
            value = data.get(key)
            return value if isinstance(value, str) and value else None

        return cls(user_id, opt("username"), opt("first_name"), text, timestamp)


def parse_command(text: str) -> tuple[str, str] | None:
    match = _COMMAND_RE.match(text.strip())
    if not match:
        return None
    return match.group(1).lower(), (match.group(2) or "").strip().lower()


@dataclass
class _UserState:
    # None im Stapel = "weiterantworten, falls die letzte Nachricht vom User offen ist"
    queue: list[Inbound | None] = field(default_factory=list)
    last_arrival: float = 0.0
    task: asyncio.Task | None = None
    since_facts: int = 0


class ConversationManager:
    def __init__(
        self,
        repo: BotRepository,
        gateway: GatewayClient,
        generator: ResponseGenerator,
        facts: FactExtractor,
        payments: PaymentProvider,
        config: Config,
    ) -> None:
        self._repo = repo
        self._gateway = gateway
        self._generator = generator
        self._facts = facts
        self._payments = payments
        self._cfg = config
        self._states: dict[int, _UserState] = {}
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ Eingang

    def enqueue(self, msg: Inbound) -> None:
        """Nimmt ein Event an und kehrt sofort zurück (der Gateway-Webhook darf nicht warten)."""
        state = self._states.setdefault(msg.user_id, _UserState())
        state.queue.append(msg)
        state.last_arrival = time.monotonic()
        self._ensure_worker(msg.user_id, state)

    def _enqueue_resume(self, user_id: int) -> None:
        """Nach einer Zahlung die zuletzt unbeantwortete Nachricht beantworten (ohne Debounce)."""
        state = self._states.setdefault(user_id, _UserState())
        state.queue.append(None)
        self._ensure_worker(user_id, state)

    def _ensure_worker(self, user_id: int, state: _UserState) -> None:
        if state.task is None or state.task.done():
            state.task = asyncio.ensure_future(self._worker(user_id))

    async def drain(self) -> None:
        """Wartet, bis alle Worker und Hintergrundaufgaben fertig sind (für Tests)."""
        while True:
            tasks = [s.task for s in self._states.values() if s.task and not s.task.done()]
            tasks += [t for t in self._background if not t.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        tasks = [s.task for s in self._states.values() if s.task and not s.task.done()]
        tasks += list(self._background)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _worker(self, user_id: int) -> None:
        state = self._states[user_id]
        try:
            while state.queue:
                # Debounce: warten, bis der User eine Weile nichts mehr geschickt hat
                while (wait := state.last_arrival + self._cfg.debounce_seconds - time.monotonic()) > 0:
                    await asyncio.sleep(wait)
                batch, state.queue = state.queue, []
                try:
                    await self._process(user_id, batch)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Verarbeitung für User %s fehlgeschlagen", user_id)
        finally:
            if not state.queue and state.since_facts == 0:
                self._states.pop(user_id, None)

    # ------------------------------------------------------------------ Verarbeitung

    async def _process(self, user_id: int, batch: list[Inbound | None]) -> None:
        inbound = [m for m in batch if m is not None]
        if inbound:
            latest = inbound[-1]
            await self._repo.upsert_user(
                user_id, latest.username, latest.first_name, initial_credits=self._cfg.free_credits
            )
        elif await self._repo.get_user(user_id) is None:
            return

        pending_text = 0
        resume = False
        for msg in batch:
            if msg is None:
                resume = True
                continue
            command = parse_command(msg.text)
            if command is None:
                await self._repo.add_message(user_id, "user", msg.text)
                pending_text += 1
                continue
            if pending_text:
                await self._reply(user_id, pending_text)
                pending_text = 0
            deleted = await self._handle_command(user_id, *command)
            if deleted:
                return  # alles danach im Stapel gehört zu gelöschten Daten
        if pending_text:
            await self._reply(user_id, pending_text)
        elif resume:
            recent = await self._repo.get_recent_messages(user_id, 1)
            if recent and recent[0].role == "user":
                await self._reply(user_id, 0)

    async def _handle_command(self, user_id: int, command: str, arg: str) -> bool:
        texts = self._cfg.texts
        user = await self._repo.get_user(user_id)
        if command == "stop":
            await self._repo.set_ai_enabled(user_id, False)
            await self._notice(user_id, texts.stop)
        elif command == "start":
            await self._repo.set_ai_enabled(user_id, True)
            await self._notice(user_id, texts.start)
        elif command == "balance":
            await self._notice(user_id, texts.balance.format(balance=user.credits))
        elif command == "delete":
            if arg != "confirm":
                await self._notice(user_id, texts.delete_warn.format(balance=user.credits))
                return False
            await self._repo.delete_user(user_id)
            state = self._states.get(user_id)
            if state:
                state.since_facts = 0
            log.info("Daten von User %s auf Wunsch gelöscht", user_id)
            await self._notice(user_id, texts.delete_done)
            return True
        return False

    async def _reply(self, user_id: int, new_messages: int) -> None:
        user = await self._repo.get_user(user_id)
        if user is None or not user.ai_enabled:
            return

        if user.disclosed_at is None:
            if not await self._notice(user_id, self._cfg.texts.disclosure):
                return  # pausiert oder Gateway nicht erreichbar: später erneut versuchen
            await self._repo.mark_disclosed(user_id)

        balance = await self._repo.consume_credit(user_id)
        if balance is None:
            await self._paywall(user_id)
            return

        sent = False
        try:
            ctx = await self._repo.get_user_context(user_id)
            history = await self._repo.get_recent_messages(user_id, self._cfg.history_limit)
            reply = await self._generator.generate(ctx, history)
            plan = naturalize_response(reply)
            if not plan["chunks"]:
                raise GenerationError("Antwort ergab keine sendbaren Chunks")
            await self._gateway.send(user_id, plan["chunks"], plan["delays"], wait=True)
            sent = True
            await self._repo.add_message(user_id, "assistant", reply)
        except GatewayPaused:
            log.info("Antwort an %s verworfen: Chat pausiert", user_id)
        except (GenerationError, GatewayError) as exc:
            log.error("Antwort an %s fehlgeschlagen: %s", user_id, exc)
        finally:
            if not sent:
                # Guthaben zurück, auch bei Abbruch durch Shutdown
                await asyncio.shield(self._refund(user_id))

        if sent:
            self._maybe_extract_facts(user_id, new_messages)

    async def _refund(self, user_id: int) -> None:
        try:
            await self._repo.add_credits(user_id, 1)
        except Exception as exc:  # User inzwischen gelöscht o. ä.
            log.warning("Guthaben für %s nicht zurückgebucht: %s", user_id, exc)

    async def _notice(self, user_id: int, text: str) -> bool:
        """Systemtext als eine Nachricht senden (ohne Zerlegen, Links bleiben heil)."""
        try:
            await self._gateway.send(user_id, [text], [NOTICE_DELAY], wait=True)
            return True
        except GatewayPaused:
            return False
        except GatewayError as exc:
            log.error("Systemtext an %s fehlgeschlagen: %s", user_id, exc)
            return False

    # ------------------------------------------------------------------ Bezahlschranke

    async def _paywall_product(self) -> Product | None:
        if self._cfg.paywall_product_id:
            product = await self._repo.get_product(self._cfg.paywall_product_id)
            if product and product.is_active:
                return product
            log.error("PAYWALL_PRODUCT_ID %s fehlt oder ist inaktiv", self._cfg.paywall_product_id)
        products = await self._repo.list_active_products()
        return products[0] if products else None  # günstigstes aktives Paket

    async def _paywall(self, user_id: int) -> None:
        last = await self._repo.last_offer_at(user_id)
        cooldown = timedelta(hours=self._cfg.offer_cooldown_hours)
        if last is not None and datetime.now(timezone.utc) - last < cooldown:
            return  # Link wurde kürzlich geschickt, nicht nerven

        product = await self._paywall_product()
        if product is None:
            log.error("Kein aktives Paket vorhanden, Bezahlschranke kann keinen Link senden")
            return
        try:
            link = await self._payments.create_checkout(user_id, product)
        except Exception as exc:
            log.error("Zahlungslink für %s nicht erzeugt: %s", user_id, exc)
            return
        await self._repo.create_payment(link.provider, link.ref, user_id, product)
        text = self._cfg.texts.paywall.format(
            link=link.url,
            title=product.title,
            credits=product.credits,
            price=format_price(product.price_cents, product.currency),
        )
        if await self._notice(user_id, text):
            await self._repo.record_offer(user_id, product.product_id)

    async def handle_payment(self, event: PaymentEvent) -> None:
        """Bestätigte Zahlung verbuchen und dem User Bescheid geben.

        Wirft PaymentNotFound / PaymentMismatch aus dem Repository weiter.
        """
        result = await self._repo.complete_payment(
            event.provider, event.ref, event.amount_cents, event.currency
        )
        if not result.newly_paid:
            return
        log.info(
            "Zahlung %s:%s verbucht, %s Antworten für User %s",
            event.provider,
            event.ref,
            result.payment.credits,
            result.payment.user_id,
        )
        if result.payment.user_id is not None and result.new_balance is not None:
            self._spawn(self._after_payment(result.payment.user_id, result.new_balance))

    async def _after_payment(self, user_id: int, balance: int) -> None:
        await self._notice(user_id, self._cfg.texts.payment_confirm.format(balance=balance))
        self._enqueue_resume(user_id)

    # ------------------------------------------------------------------ Gedächtnis

    def _maybe_extract_facts(self, user_id: int, new_messages: int) -> None:
        state = self._states.setdefault(user_id, _UserState())
        state.since_facts += new_messages
        if state.since_facts < self._cfg.fact_every:
            return
        state.since_facts = 0
        self._spawn(self._extract_facts(user_id))

    async def _extract_facts(self, user_id: int) -> None:
        history = await self._repo.get_recent_messages(user_id, self._cfg.fact_window)
        facts = await self._facts.extract([{"role": m.role, "content": m.content} for m in history])
        for fact in facts:
            try:
                await self._repo.record_memory(user_id, fact)
            except Exception as exc:  # User gelöscht, ungültiger Fakt
                log.warning("Fakt für %s nicht gespeichert: %s", user_id, exc)
                return
        if facts:
            log.info("%d Fakten für User %s verarbeitet", len(facts), user_id)

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
