"""Kapselndes Repository: der einzige Ort, an dem SQL fuer den Bot steht.

Kern-API laut Spezifikation:
    get_user_context(user_id)            -> UserContext | None
    record_purchase(user_id, product_id) -> PurchaseResult
    record_memory(user_id, fact_text)    -> MemoryResult

Dazu Hilfsfunktionen, ohne die die Kern-API nicht nutzbar waere
(User anlegen, Produkte pflegen, Angebote vermerken, KI an/aus).
"""

from __future__ import annotations

import json
import logging
from importlib.resources import files
from typing import Any

import asyncpg
from redis.asyncio import Redis

from .cache import ContextCache
from .errors import InvalidFact, ProductNotFound, UserNotFound
from .models import ChatMessage, Memory, MemoryResult, PurchaseEntry, PurchaseResult, User, UserContext

log = logging.getLogger(__name__)

MAX_FACT_LENGTH = 2000
DEFAULT_MEMORY_LIMIT = 50


async def _init_connection(conn: asyncpg.Connection) -> None:
    # JSONB direkt als Python-Objekte statt als String
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


def _fk_error(exc: asyncpg.ForeignKeyViolationError, user_id: int, product_id: str | None = None) -> Exception:
    name = exc.constraint_name or ""
    if "product_id" in name and product_id is not None:
        return ProductNotFound(product_id)
    return UserNotFound(user_id)


def _entry(row: asyncpg.Record) -> PurchaseEntry:
    return PurchaseEntry(
        id=row["id"],
        product_id=row["product_id"],
        title=row["title"],
        price_stars=row["price_stars"],
        status=row["status"],
        updated_at=row["updated_at"],
    )


def _memory(row: asyncpg.Record) -> Memory:
    return Memory(id=row["id"], fact=row["fact"], category=row["category"], created_at=row["created_at"])


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
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size, init=_init_connection)
        redis = Redis.from_url(redis_url, decode_responses=True) if redis_url else None
        repo = cls(pool, redis, cache_ttl)
        if apply_schema:
            await repo.init_schema()
        return repo

    async def init_schema(self) -> None:
        sql = files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")
        async with self._pool.acquire() as conn:
            # Advisory Lock, damit parallel startende Bot-Instanzen sich nicht stoeren
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext('botdb_schema'))")
                await conn.execute(sql)

    async def close(self) -> None:
        await self._pool.close()
        if self._redis is not None:
            await self._redis.aclose()

    # ------------------------------------------------------------------ Kern-API

    async def get_user_context(self, user_id: int, memory_limit: int = DEFAULT_MEMORY_LIMIT) -> UserContext | None:
        """Userprofil, die neuesten Memories und die Kaufhistorie. None, wenn der User unbekannt ist.

        Ergebnisse mit dem Standardlimit werden in Redis gecacht.
        """
        use_cache = memory_limit == DEFAULT_MEMORY_LIMIT
        if use_cache:
            cached = await self._cache.get(user_id)
            if cached is not None:
                return cached

        async with self._pool.acquire() as conn:
            # Ein Snapshot fuer alle drei Abfragen, damit der Kontext in sich stimmig ist
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                urow = await conn.fetchrow(
                    "SELECT telegram_id, username, first_name, ai_enabled, last_active "
                    "FROM users WHERE telegram_id = $1",
                    user_id,
                )
                if urow is None:
                    return None
                mrows = await conn.fetch(
                    "SELECT id, fact, category, created_at FROM memories "
                    "WHERE user_id = $1 ORDER BY created_at DESC, id LIMIT $2",
                    user_id,
                    memory_limit,
                )
                prows = await conn.fetch(
                    "SELECT op.id, op.product_id, p.title, p.price_stars, op.status::text AS status, op.updated_at "
                    "FROM offers_and_purchases op JOIN products p USING (product_id) "
                    "WHERE op.user_id = $1 ORDER BY op.updated_at DESC",
                    user_id,
                )

        entries = [_entry(r) for r in prows]
        ctx = UserContext(
            user=User(**dict(urow)),
            memories=[_memory(r) for r in mrows],
            purchases=[e for e in entries if e.status == "purchased"],
            open_offers=[e for e in entries if e.status == "offered"],
        )
        if use_cache:
            await self._cache.set(ctx)
        return ctx

    async def record_purchase(self, user_id: int, product_id: str) -> PurchaseResult:
        """Setzt den Status auf 'purchased'. Gab es vorher kein Angebot, wird der Eintrag angelegt.

        Idempotent: ein zweiter Aufruf (z. B. doppelt zugestelltes Telegram-Update)
        aendert nichts und liefert newly_purchased=False.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                try:
                    changed = await conn.fetchval(
                        "INSERT INTO offers_and_purchases (user_id, product_id, status) "
                        "VALUES ($1, $2, 'purchased') "
                        "ON CONFLICT (user_id, product_id) DO UPDATE "
                        "  SET status = 'purchased', updated_at = now() "
                        "  WHERE offers_and_purchases.status <> 'purchased' "
                        "RETURNING id",
                        user_id,
                        product_id,
                    )
                except asyncpg.ForeignKeyViolationError as exc:
                    raise _fk_error(exc, user_id, product_id) from exc

                row = await conn.fetchrow(
                    "SELECT op.id, op.product_id, p.title, p.price_stars, op.status::text AS status, op.updated_at "
                    "FROM offers_and_purchases op JOIN products p USING (product_id) "
                    "WHERE op.user_id = $1 AND op.product_id = $2",
                    user_id,
                    product_id,
                )

        if changed is not None:
            await self._cache.invalidate(user_id)
        return PurchaseResult(entry=_entry(row), newly_purchased=changed is not None)

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

    # ------------------------------------------------------------------ Hilfsfunktionen

    async def upsert_user(self, telegram_id: int, username: str | None, first_name: str | None) -> User:
        """Bei jeder eingehenden Nachricht aufrufen: legt den User an oder aktualisiert Name und last_active.

        Der Cache wird nur bei neuem User oder geaendertem Namen geleert, damit nicht
        jede Nachricht den Cache wegwirft (last_active im Cache darf also bis zur TTL alt sein).
        """
        row = await self._pool.fetchrow(
            "WITH old AS (SELECT username, first_name FROM users WHERE telegram_id = $1), "
            "up AS ("
            "  INSERT INTO users (telegram_id, username, first_name) VALUES ($1, $2, $3) "
            "  ON CONFLICT (telegram_id) DO UPDATE "
            "    SET username = EXCLUDED.username, first_name = EXCLUDED.first_name, last_active = now() "
            "  RETURNING telegram_id, username, first_name, ai_enabled, last_active"
            ") "
            "SELECT up.*, "
            "  NOT EXISTS (SELECT 1 FROM old) "
            "  OR EXISTS (SELECT 1 FROM old WHERE old.username IS DISTINCT FROM up.username "
            "             OR old.first_name IS DISTINCT FROM up.first_name) AS profile_changed "
            "FROM up",
            telegram_id,
            username,
            first_name,
        )
        data: dict[str, Any] = dict(row)
        if data.pop("profile_changed"):
            await self._cache.invalidate(telegram_id)
        return User(**data)

    async def set_ai_enabled(self, user_id: int, enabled: bool) -> None:
        result = await self._pool.execute(
            "UPDATE users SET ai_enabled = $2 WHERE telegram_id = $1", user_id, enabled
        )
        if result.endswith(" 0"):
            raise UserNotFound(user_id)
        await self._cache.invalidate(user_id)

    async def record_offer(self, user_id: int, product_id: str) -> bool:
        """Vermerkt, dass dem User ein Produkt angeboten wurde.

        Ein bereits gekauftes Produkt bleibt 'purchased'. Rueckgabe: True, wenn neu angeboten.
        """
        try:
            created = await self._pool.fetchval(
                "INSERT INTO offers_and_purchases (user_id, product_id, status) VALUES ($1, $2, 'offered') "
                "ON CONFLICT (user_id, product_id) DO NOTHING RETURNING true",
                user_id,
                product_id,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise _fk_error(exc, user_id, product_id) from exc
        if created:
            await self._cache.invalidate(user_id)
        return bool(created)

    async def upsert_product(
        self,
        product_id: str,
        title: str,
        price_stars: int | None,
        description: str = "",
        file_ids: list[str] | None = None,
        is_active: bool = True,
    ) -> None:
        await self._pool.execute(
            "INSERT INTO products (product_id, title, description, price_stars, file_ids, is_active) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (product_id) DO UPDATE SET title = EXCLUDED.title, description = EXCLUDED.description, "
            "  price_stars = EXCLUDED.price_stars, file_ids = EXCLUDED.file_ids, is_active = EXCLUDED.is_active",
            product_id,
            title,
            description,
            price_stars,
            file_ids or [],
            is_active,
        )
        # Titel/Preis stehen in gecachten Kontexten; die kurze TTL reicht hier als Aktualisierung.

    async def get_product(self, product_id: str) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            "SELECT product_id, title, description, price_stars, file_ids, is_active "
            "FROM products WHERE product_id = $1",
            product_id,
        )
        return dict(row) if row else None

    async def list_active_products(self) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            "SELECT product_id, title, description, price_stars, file_ids, is_active "
            "FROM products WHERE is_active ORDER BY title"
        )
        return [dict(r) for r in rows]

    async def deactivate_products_except(self, keep_ids: list[str]) -> int:
        """Deaktiviert alle Produkte, die nicht in keep_ids stehen (z. B. aus der Konfiguration entfernt).

        Gibt die Zahl der deaktivierten Produkte zurueck. Kaeufe bleiben erhalten.
        """
        result = await self._pool.execute(
            "UPDATE products SET is_active = false WHERE is_active AND NOT (product_id = ANY($1::text[]))",
            list(keep_ids),
        )
        return int(result.rsplit(" ", 1)[-1])

    async def try_offer(self, user_id: int, product_id: str, cooldown_seconds: int) -> bool:
        """Vermerkt ein Angebot, wenn es erlaubt ist.

        Erlaubt ist es, wenn das Produkt nie angeboten wurde oder das letzte Angebot
        laenger als cooldown_seconds her ist. Gekaufte Produkte werden nie erneut angeboten.
        Rueckgabe: True, wenn das Angebot jetzt rausgehen darf.
        """
        try:
            allowed = await self._pool.fetchval(
                "INSERT INTO offers_and_purchases (user_id, product_id, status) VALUES ($1, $2, 'offered') "
                "ON CONFLICT (user_id, product_id) DO UPDATE SET updated_at = now() "
                "  WHERE offers_and_purchases.status = 'offered' "
                "  AND offers_and_purchases.updated_at < now() - make_interval(secs => $3::double precision) "
                "RETURNING true",
                user_id,
                product_id,
                float(cooldown_seconds),
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise _fk_error(exc, user_id, product_id) from exc
        if allowed:
            await self._cache.invalidate(user_id)
        return bool(allowed)

    # ------------------------------------------------------------------ Chatverlauf

    async def add_message(self, user_id: int, role: str, content: str) -> int:
        """Speichert eine Chatnachricht und gibt ihre fortlaufende id zurueck."""
        if role not in ("user", "assistant"):
            raise ValueError("role muss 'user' oder 'assistant' sein")
        try:
            return await self._pool.fetchval(
                "INSERT INTO messages (user_id, role, content) VALUES ($1, $2, $3) RETURNING id",
                user_id,
                role,
                content,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise _fk_error(exc, user_id) from exc

    async def recent_messages(self, user_id: int, limit: int = 30) -> list[ChatMessage]:
        """Die letzten `limit` Nachrichten, aelteste zuerst."""
        rows = await self._pool.fetch(
            "SELECT id, role, content, created_at FROM ("
            "  SELECT id, role, content, created_at FROM messages WHERE user_id = $1 ORDER BY id DESC LIMIT $2"
            ") t ORDER BY id",
            user_id,
            limit,
        )
        return [ChatMessage(**dict(r)) for r in rows]

    async def mark_disclosed(self, user_id: int) -> bool:
        """Vermerkt, dass der KI-Hinweis gesendet wurde. True nur beim ersten Mal."""
        done = await self._pool.fetchval(
            "UPDATE users SET disclosed_at = now() WHERE telegram_id = $1 AND disclosed_at IS NULL RETURNING true",
            user_id,
        )
        return bool(done)

    # ------------------------------------------------------------------ Zahlungen

    async def record_payment(
        self,
        payment_id: str,
        provider: str,
        user_id: int,
        product_id: str,
        amount: int | None = None,
        currency: str | None = None,
    ) -> bool:
        """Speichert eine Zahlung. False, wenn payment_id schon bekannt ist (Doppelzustellung)."""
        try:
            created = await self._pool.fetchval(
                "INSERT INTO payments (payment_id, provider, user_id, product_id, amount, currency) "
                "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (payment_id) DO NOTHING RETURNING true",
                payment_id,
                provider,
                user_id,
                product_id,
                amount,
                currency,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise _fk_error(exc, user_id, product_id) from exc
        return bool(created)

    async def get_payment(self, payment_id: str) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            "SELECT payment_id, provider, user_id, product_id, amount, currency, refunded, created_at "
            "FROM payments WHERE payment_id = $1",
            payment_id,
        )
        return dict(row) if row else None

    async def mark_refunded(self, payment_id: str) -> bool:
        """Markiert eine Zahlung als erstattet und nimmt den Kauf zurueck (Status wieder 'offered')."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "UPDATE payments SET refunded = true WHERE payment_id = $1 AND NOT refunded "
                    "RETURNING user_id, product_id",
                    payment_id,
                )
                if row is None:
                    return False
                await conn.execute(
                    "UPDATE offers_and_purchases SET status = 'offered', updated_at = now() "
                    "WHERE user_id = $1 AND product_id = $2",
                    row["user_id"],
                    row["product_id"],
                )
        await self._cache.invalidate(row["user_id"])
        return True
