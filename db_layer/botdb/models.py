"""Datenobjekte, die das Repository zurueckgibt.

Alle Objekte lassen sich ueber to_dict()/from_dict() verlustfrei als JSON
serialisieren, damit der Redis-Cache sie speichern kann.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


def _dt(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


@dataclass(frozen=True, slots=True)
class User:
    telegram_id: int
    username: str | None
    first_name: str | None
    ai_enabled: bool
    last_active: datetime

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "User":
        return cls(
            telegram_id=int(d["telegram_id"]),
            username=d["username"],
            first_name=d["first_name"],
            ai_enabled=bool(d["ai_enabled"]),
            last_active=_dt(d["last_active"]),
        )


@dataclass(frozen=True, slots=True)
class Memory:
    id: UUID
    fact: str
    category: str
    created_at: datetime

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Memory":
        return cls(
            id=UUID(str(d["id"])),
            fact=d["fact"],
            category=d["category"],
            created_at=_dt(d["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class PurchaseEntry:
    """Ein Eintrag aus offers_and_purchases, angereichert mit Produktdaten."""

    id: UUID
    product_id: str
    title: str
    price_stars: int | None  # None: Produkt wird nicht per Stars verkauft
    status: str  # 'offered' | 'purchased'
    updated_at: datetime

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PurchaseEntry":
        return cls(
            id=UUID(str(d["id"])),
            product_id=d["product_id"],
            title=d["title"],
            price_stars=None if d["price_stars"] is None else int(d["price_stars"]),
            status=d["status"],
            updated_at=_dt(d["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class UserContext:
    """Ergebnis von get_user_context: alles, was die KI ueber einen User wissen muss."""

    user: User
    memories: list[Memory] = field(default_factory=list)
    purchases: list[PurchaseEntry] = field(default_factory=list)  # status = purchased
    open_offers: list[PurchaseEntry] = field(default_factory=list)  # status = offered

    @property
    def purchased_product_ids(self) -> set[str]:
        return {p.product_id for p in self.purchases}

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UserContext":
        return cls(
            user=User.from_dict(d["user"]),
            memories=[Memory.from_dict(m) for m in d["memories"]],
            purchases=[PurchaseEntry.from_dict(p) for p in d["purchases"]],
            open_offers=[PurchaseEntry.from_dict(p) for p in d["open_offers"]],
        )


@dataclass(frozen=True, slots=True)
class PurchaseResult:
    entry: PurchaseEntry
    newly_purchased: bool  # False, wenn das Produkt schon vorher gekauft war


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """Eine Nachricht aus dem gespeicherten Chatverlauf."""

    id: int
    role: str  # 'user' | 'assistant'
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryResult:
    memory: Memory
    created: bool  # False, wenn der Fakt schon existierte (Duplikat)
