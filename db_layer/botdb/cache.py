"""Redis-Caching-Layer fuer den User-Kontext.

Grundsatz: Redis ist nur ein Beschleuniger. Faellt Redis aus, laeuft alles
weiter direkt gegen PostgreSQL; Fehler werden geloggt, aber nie geworfen.
"""

from __future__ import annotations

import json
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from .models import UserContext

log = logging.getLogger(__name__)

# Bei Aenderungen am Format von UserContext die Version erhoehen,
# dann werden alte Cache-Eintraege automatisch ignoriert.
_KEY_VERSION = "v1"


class ContextCache:
    def __init__(self, redis: Redis | None, ttl_seconds: int = 300, prefix: str = "botdb"):
        self._redis = redis
        self._ttl = ttl_seconds
        self._prefix = prefix

    def _key(self, user_id: int) -> str:
        return f"{self._prefix}:ctx:{_KEY_VERSION}:{user_id}"

    async def get(self, user_id: int) -> UserContext | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(self._key(user_id))
        except RedisError as exc:
            log.warning("Redis GET fehlgeschlagen, nutze DB: %s", exc)
            return None
        if raw is None:
            return None
        try:
            return UserContext.from_dict(json.loads(raw))
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("Kaputter Cache-Eintrag fuer %s wird verworfen: %s", user_id, exc)
            await self.invalidate(user_id)
            return None

    async def set(self, ctx: UserContext) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(
                self._key(ctx.user.telegram_id),
                json.dumps(ctx.to_dict(), ensure_ascii=False),
                ex=self._ttl,
            )
        except RedisError as exc:
            log.warning("Redis SET fehlgeschlagen: %s", exc)

    async def invalidate(self, user_id: int) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._key(user_id))
        except RedisError as exc:
            # Schlimmster Fall: veralteter Kontext bis TTL abgelaufen ist.
            log.warning("Redis DELETE fehlgeschlagen fuer %s: %s", user_id, exc)
