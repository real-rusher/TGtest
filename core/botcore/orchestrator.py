"""Der Orchestrator: verbindet Telegram-Modul, Datenbank und Sprachmodell.

Ablauf pro eingehender Nachricht:
  1. User anlegen/aktualisieren, Nachricht speichern
  2. kurz warten, ob weitere Nachrichten folgen (reply_delay_seconds)
  3. Kontext laden, Antwort vom Sprachmodell holen, Angebots-Markierung auswerten
  4. Antwort in Chunks mit Tipp-Verzögerung senden, ggf. Angebot hinterher
  5. alle paar Nachrichten im Hintergrund Fakten über den User extrahieren
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict

from botdb import BotRepository, UserNotFound

from .config import BotConfig, Product
from .facts import extract_facts
from .llm import LLM, LLMError
from .naturalize import _calc_delay, naturalize_response
from .payments import payment_link
from .prompt import build_system_prompt, split_offer
from .transport import TransportClient, TransportError, UserUnreachable

log = logging.getLogger(__name__)

FACT_WINDOW = 20  # so viele letzte Nachrichten sieht die Fakten-Extraktion


class Orchestrator:
    def __init__(
        self,
        cfg: BotConfig,
        repo: BotRepository,
        llm: LLM,
        transport: TransportClient,
        facts_llm: LLM | None = None,
    ):
        self.cfg = cfg
        self.repo = repo
        self.llm = llm
        self.facts_llm = facts_llm or llm
        self.transport = transport

        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._timers: dict[int, asyncio.Task] = {}
        self._answered: dict[int, int] = {}  # user_id -> höchste beantwortete Nachrichten-id
        self._since_facts: defaultdict[int, int] = defaultdict(int)
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ Lifecycle

    async def sync_products(self) -> None:
        """Überträgt die Produkte aus bot.yaml in die Datenbank."""
        for p in self.cfg.products:
            await self.repo.upsert_product(
                p.id,
                p.title,
                p.price_stars,
                p.description,
                [d.file for d in p.deliver if d.file],
            )
        removed = await self.repo.deactivate_products_except([p.id for p in self.cfg.products])
        log.info("%d Produkte geladen, %d deaktiviert", len(self.cfg.products), removed)

    async def shutdown(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ Eingang

    async def handle_inbound(self, user_id: int, text: str, username: str | None, first_name: str | None) -> None:
        """Wird für jede eingehende Nachricht aufgerufen (vom HTTP-Server)."""
        await self.repo.upsert_user(user_id, username, first_name)
        text = text.strip()

        if text.split("@", 1)[0].lower() == "/start" and self.cfg.start_message:
            self._spawn(self._send_start(user_id))
            return

        await self.repo.add_message(user_id, "user", text)
        self._since_facts[user_id] += 1

        # Debounce: weitere Nachrichten innerhalb der Wartezeit werden gemeinsam beantwortet.
        timer = self._timers.get(user_id)
        if timer is not None and not timer.done():
            timer.cancel()
        self._timers[user_id] = self._spawn(self._reply_later(user_id))

    async def _reply_later(self, user_id: int) -> None:
        # Nur die Wartezeit ist abbrechbar; die eigentliche Antwort läuft als eigener Task,
        # damit eine neue Nachricht nie eine halb gesendete Antwort abbricht.
        await asyncio.sleep(self.cfg.reply_delay_seconds)
        self._spawn(self._reply(user_id))

    async def _reply(self, user_id: int) -> None:
        async with self._locks[user_id]:
            try:
                await self._respond(user_id)
            except Exception:
                log.exception("Antwort an %s fehlgeschlagen", user_id)
        if self.cfg.memory_enabled and self._since_facts[user_id] >= self.cfg.memory_extract_every:
            self._since_facts[user_id] = 0
            self._spawn(self._extract_facts(user_id))

    async def _send_start(self, user_id: int) -> None:
        async with self._locks[user_id]:
            chunks = [self.cfg.start_message]
            if self.cfg.disclosure_mode == "first_message" and await self.repo.mark_disclosed(user_id):
                chunks.append(self.cfg.disclosure_text)
            await self.repo.add_message(user_id, "assistant", "\n".join(chunks))
            try:
                await self.send_text(user_id, "\n".join(chunks))
            except TransportError as exc:
                log.warning("Startnachricht an %s fehlgeschlagen: %s", user_id, exc)

    # ------------------------------------------------------------------ Antworten

    async def _respond(self, user_id: int) -> None:
        ctx = await self.repo.get_user_context(user_id)
        if ctx is None or not ctx.user.ai_enabled:
            return  # KI für diesen User abgeschaltet (Übernahme durch einen Menschen)

        history = await self.repo.recent_messages(user_id, self.cfg.history_messages)
        user_ids = [m.id for m in history if m.role == "user"]
        if not user_ids or max(user_ids) <= self._answered.get(user_id, 0):
            return  # nichts Neues zu beantworten
        if history[-1].role != "user":
            # Neue Nachricht kam, während die letzte Antwort entstand; sie ist damit beantwortet.
            self._answered[user_id] = max(user_ids)
            return

        system = build_system_prompt(self.cfg, ctx)
        try:
            raw = await self.llm.chat(system, [{"role": m.role, "content": m.content} for m in history])
        except LLMError as exc:
            log.warning("Keine Antwort für %s: %s", user_id, exc)
            self._answered[user_id] = max(user_ids)
            return

        reply, offer_id = split_offer(raw)
        self._answered[user_id] = max(user_ids)

        if reply:
            # Vor dem Senden speichern: Nachrichten, die während des Tippens kommen,
            # stehen so im Verlauf nach dieser Antwort und werden danach beantwortet.
            await self.repo.add_message(user_id, "assistant", reply)
            if self.cfg.disclosure_mode == "first_message" and await self.repo.mark_disclosed(user_id):
                reply = self.cfg.disclosure_text + "\n" + reply
            try:
                await self.send_text(user_id, reply)
            except UserUnreachable as exc:
                log.info("Antwort an %s nicht zugestellt: %s", user_id, exc)
                return

        if offer_id:
            await self.make_offer(user_id, offer_id, ctx.purchased_product_ids)

    async def send_text(self, user_id: int, text: str, *, verbatim: bool = False) -> None:
        """Sendet Text mit Tipp-Verzögerung.

        Normalerweise wird der Text in Messenger-Chunks zerlegt (naturalize). Mit
        verbatim=True geht er unverändert als eine Nachricht raus (z. B. von Hand geschrieben).
        """
        if verbatim:
            natural = {"chunks": [text.strip()], "delays": [_calc_delay(text.strip())]}
        else:
            natural = naturalize_response(text)
        delays = [
            min(self.cfg.max_delay_seconds, round(d * self.cfg.typing_speed, 2)) for d in natural["delays"]
        ]
        await self.transport.send_chunks(user_id, natural["chunks"], delays)

    # ------------------------------------------------------------------ Angebote

    async def make_offer(self, user_id: int, product_id: str, owned: set[str] | frozenset[str] = frozenset()) -> bool:
        product = self.cfg.product(product_id)
        if not self.cfg.sells or product is None:
            log.info("KI wollte unbekanntes Produkt %r anbieten, ignoriert", product_id)
            return False
        if product.id in owned:
            return False
        cooldown = int(self.cfg.offer_cooldown_hours * 3600)
        if not await self.repo.try_offer(user_id, product.id, cooldown):
            log.info("Angebot %s an %s übersprungen (gekauft oder Sperrzeit)", product.id, user_id)
            return False

        try:
            if self.cfg.payment_method == "stars":
                await self.transport.send_invoice(
                    user_id, product.title, product.description, product.id, product.price_stars
                )
            else:
                link = payment_link(self.cfg.payment_method, product, user_id) or ""
                text = (
                    self.cfg.offer_text.replace("{title}", product.title)
                    .replace("{price}", product.price_label or "")
                    .replace("{description}", product.description)
                    .replace("{link}", link)
                )
                await self.transport.send_chunks(user_id, [text], [1.0])
        except TransportError as exc:
            log.warning("Angebot %s an %s nicht zugestellt: %s", product.id, user_id, exc)
            return False
        log.info("Angebot %s an %s gesendet", product.id, user_id)
        return True

    # ------------------------------------------------------------------ Zahlungen

    async def complete_purchase(
        self,
        user_id: int,
        product_id: str,
        payment_id: str | None,
        provider: str,
        amount: int | None = None,
        currency: str | None = None,
    ) -> bool:
        """Verbucht eine Zahlung und liefert das Produkt aus.

        Idempotent über payment_id: Doppelt gemeldete Zahlungen werden nur einmal ausgeliefert.
        Gibt True zurück, wenn die Zahlung neu war.
        """
        product = self.cfg.product(product_id)
        if product is None:
            log.error("Zahlung %s für unbekanntes Produkt %r (User %s)", payment_id, product_id, user_id)
            return False
        payment_id = payment_id or f"manual-{uuid.uuid4()}"
        try:
            if not await self.repo.record_payment(payment_id, provider, user_id, product_id, amount, currency):
                log.info("Zahlung %s bereits verbucht", payment_id)
                return False
        except UserNotFound:
            log.error("Zahlung %s von unbekanntem User %s", payment_id, user_id)
            return False
        await self.repo.record_purchase(user_id, product_id)
        log.info("Kauf verbucht: User %s, Produkt %s, Zahlung %s (%s)", user_id, product_id, payment_id, provider)
        self._spawn(self.deliver(user_id, product))
        return True

    async def deliver(self, user_id: int, product: Product) -> None:
        """Schickt den Inhalt eines gekauften Produkts."""
        async with self._locks[user_id]:
            try:
                for item in product.deliver:
                    if item.file:
                        await self.transport.send_file(user_id, item.file, item.caption)
                    if item.text:
                        await self.transport.send_chunks(user_id, [item.text], [1.0])
                if product.thank_you:
                    await self.send_text(user_id, product.thank_you)
                    await self.repo.add_message(user_id, "assistant", product.thank_you)
            except TransportError as exc:
                # Zahlung ist verbucht; per /admin/deliver kann erneut ausgeliefert werden.
                log.error("Auslieferung von %s an %s fehlgeschlagen: %s", product.id, user_id, exc)

    # ------------------------------------------------------------------ Gedächtnis

    async def _extract_facts(self, user_id: int) -> None:
        history = await self.repo.recent_messages(user_id, FACT_WINDOW)
        facts = await extract_facts(self.facts_llm, [{"role": m.role, "content": m.content} for m in history])
        created = 0
        for fact in facts:
            try:
                created += (await self.repo.record_memory(user_id, fact)).created
            except Exception as exc:
                log.debug("Fakt verworfen: %s", exc)
        if created:
            log.info("%d neue Fakten über %s gespeichert", created, user_id)
