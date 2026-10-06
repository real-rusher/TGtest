"""Kapselndes Repository: der einzige Ort, an dem SQL steht.

Bereiche:
    User          upsert_user, get_user_context, set_ai_enabled, mark_disclosed, delete_user
    Gedaechtnis   record_memory
    Chatverlauf   add_message, get_recent_messages
    Guthaben      consume_credit, add_credits
    Katalog       upsert_product, get_product, list_active_products
    Angebote      record_offer, last_offer_at
    Zahlungen     create_payment, complete_payment, get_payment
"""

from __future__ import annotations

import logging
from datetime import datetime
from importlib.resources import files
from typing import Any

import asyncpg
from redis.asyncio import Redis

from .cache import ContextCache
from .errors import (
    InvalidFact,
    InvalidMessage,
    PaymentMismatch,
    PaymentNotFound,
    ProductNotFound,
    UserNotFound,
)
from .models import (
    ChatMessage,
    Memory,
    MemoryResult,
    Payment,
    PaymentResult,
    Product,
    PurchaseEntry,
    User,
    UserContext,
)

log = logging.getLogger(__name__)

MAX_FACT_LENGTH = 2000
MAX_MESSAGE_LENGTH = 20000
DEFAULT_MEMORY_LIMIT = 50
DEFAULT_PURCHASE_LIMIT = 20

_USER_COLS = "telegram_id, username, first_name, ai_enabled, credits, disclosed_at, created_at, last_active"
_PRODUCT_COLS = "product_id, title, description, price_cents, currency, credits, is_active"
_PAYMENT_COLS = (
    "id, provider, provider_ref, user_id, product_id, amount_cents, currency, credits, "
    "status::text AS status, created_at, paid_at"
)


def _fk_error(exc: asyncpg.ForeignKeyViolationError, user_id: int, product_id: str | None = None) -> Exception:
    name = exc.constraint_name or ""
    if "product_id" in name and product_id is not None:
        return ProductNotFound(product_id)
    return UserNotFound(user_id)


def _user(row: asyncpg.Record) -> User:
    return User(**dict(row))


def _memory(row: asyncpg.Record) -> Memory:
    return Memory(id=row["id"], fact=row["fact"], category=row["category"], created_at=row["created_at"])


def _product(row: asyncpg.Record) -> Product:
    return Product(**{**dict(row), "currency": row["currency"].strip()})


def _payment(row: asyncpg.Record) -> Payment:
    return Payment(**{**dict(row), "currency": row["currency"].strip()})


class BotRepository:
    def __init__(self, pool: asyncpg.Pool, redis: Redis | None = None, cache_ttl: int = 300):
        self._pool = pool
        self._redis = redis
        self._cache = ContextCache(redis, ttl_seconds=cache_ttl)

    # ------------------------------------------------------------------ Lifecycle

    @classmethod
    async def connect(
        cls,
        dsn: str,
        redis_url: str | None = None,
        *,
        cache_ttl: int = 300,
        min_size: int = 2,
        max_size: int = 10,
        apply_schema: bool = True,
    ) -> "BotRepository":
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        redis = Redis.from_url(redis_url, decode_responses=True) if redis_url else None
        repo = cls(pool, redis, cache_ttl)
        if apply_schema:
            await repo.init_schema()
        return repo

    async def init_schema(self) -> None:
        sql = files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")
        async with self._pool.acquire() as conn:
            # Advisory Lock, damit parallel startende Instanzen sich nicht stoeren
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext('botdb_schema'))")
                await conn.execute(sql)

    async def close(self) -> None:
        await self._pool.close()
        if self._redis is not None:
            await self._redis.aclose()

    # ------------------------------------------------------------------ User

    async def upsert_user(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
        *,
        initial_credits: int = 0,
    ) -> tuple[User, bool]:
        """Bei jeder eingehenden Nachricht aufrufen. Gibt (User, neu_angelegt) zurueck.

        `initial_credits` wird nur beim Anlegen gutgeschrieben (Gratis-Kontingent).
        Der Cache wird nur bei neuem User oder geaendertem Namen geleert, last_active
        im gecachten Kontext darf also bis zur TTL alt sein.
        """
        if initial_credits < 0:
            raise ValueError("initial_credits darf nicht negativ sein")
        row = await self._pool.fetchrow(
            "WITH old AS (SELECT username, first_name FROM users WHERE telegram_id = $1), "
            "up AS ("
            "  INSERT INTO users (telegram_id, username, first_name, credits) VALUES ($1, $2, $3, $4) "
            "  ON CONFLICT (telegram_id) DO UPDATE "
            "    SET username = EXCLUDED.username, first_name = EXCLUDED.first_name, last_active = now() "
            f"  RETURNING {_USER_COLS}"
            ") "
            "SELECT up.*, NOT EXISTS (SELECT 1 FROM old) AS created, "
            "  EXISTS (SELECT 1 FROM old WHERE old.username IS DISTINCT FROM up.username "
            "          OR old.first_name IS DISTINCT FROM up.first_name) AS renamed "
            "FROM up",
            telegram_id,
            username,
            first_name,
            initial_credits,
        )
        data: dict[str, Any] = dict(row)
        created = data.pop("created")
        if data.pop("renamed") or created:
            await self._cache.invalidate(telegram_id)
        return User(**data), created

    async def get_user(self, user_id: int) -> User | None:
        row = await self._pool.fetchrow(f"SELECT {_USER_COLS} FROM users WHERE telegram_id = $1", user_id)
        return _user(row) if row else None

    async def get_user_context(self, user_id: int, memory_limit: int = DEFAULT_MEMORY_LIMIT) -> UserContext | None:
        """Profil, neueste Memories, bezahlte Kaeufe, letztes Angebot. None bei unbekanntem User.

        Ergebnisse mit dem Standardlimit werden in Redis gecacht.
        """
        use_cache = memory_limit == DEFAULT_MEMORY_LIMIT
        if use_cache:
            cached = await self._cache.get(user_id)
            if cached is not None:
                return cached

        async with self._pool.acquire() as conn:
            # Ein Snapshot fuer alle Abfragen, damit der Kontext in sich stimmig ist
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                urow = await conn.fetchrow(f"SELECT {_USER_COLS} FROM users WHERE telegram_id = $1", user_id)
                if urow is None:
                    return None
                mrows = await conn.fetch(
                    "SELECT id, fact, category, created_at FROM memories "
                    "WHERE user_id = $1 ORDER BY created_at DESC, id LIMIT $2",
                    user_id,
                    memory_limit,
                )
                prows = await conn.fetch(
                    "SELECT pay.id AS payment_id, pay.product_id, p.title, pay.amount_cents, "
                    "  pay.currency, pay.credits, pay.paid_at "
                    "FROM payments pay JOIN products p USING (product_id) "
                    "WHERE pay.user_id = $1 AND pay.status = 'paid' "
                    "ORDER BY pay.paid_at DESC LIMIT $2",
                    user_id,
                    DEFAULT_PURCHASE_LIMIT,
                )
                last_offer = await conn.fetchval(
                    "SELECT max(offered_at) FROM offers WHERE user_id = $1", user_id
                )

        ctx = UserContext(
            user=_user(urow),
            memories=[_memory(r) for r in mrows],
            purchases=[
                PurchaseEntry(**{**dict(r), "currency": r["currency"].strip()}) for r in prows
            ],
            last_offer_at=last_offer,
        )
        if use_cache:
            await self._cache.set(ctx)
        return ctx

    async def set_ai_enabled(self, user_id: int, enabled: bool) -> None:
        result = await self._pool.execute(
            "UPDATE users SET ai_enabled = $2 WHERE telegram_id = $1", user_id, enabled
        )
        if result.endswith(" 0"):
            raise UserNotFound(user_id)
        await self._cache.invalidate(user_id)

    async def mark_disclosed(self, user_id: int) -> bool:
        """Vermerkt, dass der KI-Hinweis verschickt wurde. True beim ersten Mal."""
        changed = await self._pool.fetchval(
            "UPDATE users SET disclosed_at = now() "
            "WHERE telegram_id = $1 AND disclosed_at IS NULL RETURNING true",
            user_id,
        )
        if changed:
            await self._cache.invalidate(user_id)
        return bool(changed)

    async def delete_user(self, user_id: int) -> bool:
        """Loescht User, Memories, Chatverlauf und Angebote. Zahlungen bleiben anonymisiert erhalten."""
        result = await self._pool.execute("DELETE FROM users WHERE telegram_id = $1", user_id)
        await self._cache.invalidate(user_id)
        return not result.endswith(" 0")

    # ------------------------------------------------------------------ Gedaechtnis

    async def record_memory(self, user_id: int, fact_text: str, category: str = "general") -> MemoryResult:
        """Speichert einen extrahierten Fakt. Exakte Duplikate werden nicht doppelt angelegt."""
        fact = " ".join((fact_text or "").split())
        if not fact:
            raise InvalidFact("Fakt ist leer")
        if len(fact) > MAX_FACT_LENGTH:
            raise InvalidFact(f"Fakt ist laenger als {MAX_FACT_LENGTH} Zeichen")
        category = (category or "general").strip().lower()[:64] or "general"

        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    "INSERT INTO memories (user_id, fact, category) VALUES ($1, $2, $3) "
                    "ON CONFLICT (user_id, md5(lower(btrim(fact)))) DO NOTHING "
                    "RETURNING id, fact, category, created_at",
                    user_id,
                    fact,
                    category,
                )
            except asyncpg.ForeignKeyViolationError as exc:
                raise _fk_error(exc, user_id) from exc

            if row is None:
                existing = await conn.fetchrow(
                    "SELECT id, fact, category, created_at FROM memories "
                    "WHERE user_id = $1 AND md5(lower(btrim(fact))) = md5(lower(btrim($2::text)))",
                    user_id,
                    fact,
                )
                return MemoryResult(memory=_memory(existing), created=False)

        await self._cache.invalidate(user_id)
        return MemoryResult(memory=_memory(row), created=True)

    # ------------------------------------------------------------------ Chatverlauf

    async def add_message(self, user_id: int, role: str, content: str) -> ChatMessage:
        if role not in ("user", "assistant"):
            raise InvalidMessage(f"Unbekannte Rolle {role!r}")
        if not content or not content.strip():
            raise InvalidMessage("Nachricht ist leer")
        if len(content) > MAX_MESSAGE_LENGTH:
            content = content[:MAX_MESSAGE_LENGTH]
        try:
            row = await self._pool.fetchrow(
                "INSERT INTO messages (user_id, role, content) VALUES ($1, $2, $3) "
                "RETURNING id, role, content, created_at",
                user_id,
                role,
                content,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise UserNotFound(user_id) from exc
        return ChatMessage(**dict(row))

    async def get_recent_messages(self, user_id: int, limit: int = 30) -> list[ChatMessage]:
        """Die letzten `limit` Nachrichten, aelteste zuerst."""
        rows = await self._pool.fetch(
            "SELECT id, role, content, created_at FROM ("
            "  SELECT id, role, content, created_at FROM messages "
            "  WHERE user_id = $1 ORDER BY id DESC LIMIT $2"
            ") recent ORDER BY id",
            user_id,
            limit,
        )
        return [ChatMessage(**dict(r)) for r in rows]

    # ------------------------------------------------------------------ Guthaben

    async def consume_credit(self, user_id: int) -> int | None:
        """Zieht atomar 1 Guthaben ab. Gibt den neuen Stand zurueck oder None, wenn nichts mehr da war."""
        balance = await self._pool.fetchval(
            "UPDATE users SET credits = credits - 1 "
            "WHERE telegram_id = $1 AND credits > 0 RETURNING credits",
            user_id,
        )
        if balance is not None:
            await self._cache.invalidate(user_id)
        return balance

    async def add_credits(self, user_id: int, amount: int) -> int:
        if amount <= 0:
            raise ValueError("amount muss positiv sein")
        balance = await self._pool.fetchval(
            "UPDATE users SET credits = credits + $2 WHERE telegram_id = $1 RETURNING credits",
            user_id,
            amount,
        )
        if balance is None:
            raise UserNotFound(user_id)
        await self._cache.invalidate(user_id)
        return balance

    # ------------------------------------------------------------------ Katalog

    async def upsert_product(
        self,
        product_id: str,
        title: str,
        price_cents: int,
        credits: int,
        *,
        currency: str = "EUR",
        description: str = "",
        is_active: bool = True,
    ) -> Product:
        row = await self._pool.fetchrow(
            "INSERT INTO products (product_id, title, description, price_cents, currency, credits, is_active) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT (product_id) DO UPDATE SET title = EXCLUDED.title, "
            "  description = EXCLUDED.description, price_cents = EXCLUDED.price_cents, "
            "  currency = EXCLUDED.currency, credits = EXCLUDED.credits, is_active = EXCLUDED.is_active "
            f"RETURNING {_PRODUCT_COLS}",
            product_id,
            title,
            description,
            price_cents,
            currency.upper(),
            credits,
            is_active,
        )
        return _product(row)

    async def get_product(self, product_id: str) -> Product | None:
        row = await self._pool.fetchrow(f"SELECT {_PRODUCT_COLS} FROM products WHERE product_id = $1", product_id)
        return _product(row) if row else None

    async def list_active_products(self) -> list[Product]:
        rows = await self._pool.fetch(
            f"SELECT {_PRODUCT_COLS} FROM products WHERE is_active ORDER BY price_cents, product_id"
        )
        return [_product(r) for r in rows]

    # ------------------------------------------------------------------ Angebote

    async def record_offer(self, user_id: int, product_id: str) -> None:
        try:
            await self._pool.execute(
                "INSERT INTO offers (user_id, product_id) VALUES ($1, $2)", user_id, product_id
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise _fk_error(exc, user_id, product_id) from exc
        await self._cache.invalidate(user_id)

    async def last_offer_at(self, user_id: int) -> datetime | None:
        return await self._pool.fetchval("SELECT max(offered_at) FROM offers WHERE user_id = $1", user_id)

    # ------------------------------------------------------------------ Zahlungen

    async def create_payment(self, provider: str, provider_ref: str, user_id: int, product: Product) -> Payment:
        """Legt einen offenen Zahlungsauftrag an, sobald ein Zahlungslink erzeugt wurde."""
        try:
            row = await self._pool.fetchrow(
                "INSERT INTO payments (provider, provider_ref, user_id, product_id, amount_cents, currency, credits) "
                f"VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING {_PAYMENT_COLS}",
                provider,
                provider_ref,
                user_id,
                product.product_id,
                product.price_cents,
                product.currency,
                product.credits,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise _fk_error(exc, user_id, product.product_id) from exc
        return _payment(row)

    async def get_payment(self, provider: str, provider_ref: str) -> Payment | None:
        row = await self._pool.fetchrow(
            f"SELECT {_PAYMENT_COLS} FROM payments WHERE provider = $1 AND provider_ref = $2",
            provider,
            provider_ref,
        )
        return _payment(row) if row else None

    async def complete_payment(
        self, provider: str, provider_ref: str, amount_cents: int, currency: str
    ) -> PaymentResult:
        """Verbucht eine bestaetigte Zahlung und schreibt das Guthaben gut.

        Idempotent: doppelt zugestellte Webhooks verbuchen nur einmal (newly_paid=False).
        Wirft PaymentNotFound oder PaymentMismatch (Betrag/Waehrung weichen ab).
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"SELECT {_PAYMENT_COLS} FROM payments "
                    "WHERE provider = $1 AND provider_ref = $2 FOR UPDATE",
                    provider,
                    provider_ref,
                )
                if row is None:
                    raise PaymentNotFound(provider, provider_ref)
                payment = _payment(row)
                if amount_cents != payment.amount_cents or currency.upper() != payment.currency:
                    raise PaymentMismatch(
                        f"erwartet {payment.amount_cents} {payment.currency}, "
                        f"erhalten {amount_cents} {currency.upper()}"
                    )
                if payment.status == "paid":
                    balance = None
                    if payment.user_id is not None:
                        balance = await conn.fetchval(
                            "SELECT credits FROM users WHERE telegram_id = $1", payment.user_id
                        )
                    return PaymentResult(payment=payment, newly_paid=False, new_balance=balance)

                row = await conn.fetchrow(
                    "UPDATE payments SET status = 'paid', paid_at = now() WHERE id = $1 "
                    f"RETURNING {_PAYMENT_COLS}",
                    payment.id,
                )
                payment = _payment(row)
                balance = None
                if payment.user_id is not None:
                    balance = await conn.fetchval(
                        "UPDATE users SET credits = credits + $2 WHERE telegram_id = $1 RETURNING credits",
                        payment.user_id,
                        payment.credits,
                    )

        if payment.user_id is not None:
            await self._cache.invalidate(payment.user_id)
        return PaymentResult(payment=payment, newly_paid=True, new_balance=balance)
