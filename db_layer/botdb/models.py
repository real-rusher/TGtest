"""Datenobjekte, die das Repository zurueckgibt.

UserContext laesst sich ueber to_dict()/from_dict() verlustfrei als JSON
serialisieren, damit der Redis-Cache ihn speichern kann.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


def _dt(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


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
    credits: int
    disclosed_at: datetime | None
    created_at: datetime
    last_active: datetime

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "User":
        return cls(
            telegram_id=int(d["telegram_id"]),
            username=d["username"],
            first_name=d["first_name"],
            ai_enabled=bool(d["ai_enabled"]),
            credits=int(d["credits"]),
            disclosed_at=_dt(d["disclosed_at"]),
            created_at=_dt(d["created_at"]),
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
    """Eine bezahlte Zahlung, angereichert mit dem Produkttitel."""

    payment_id: UUID
    product_id: str
    title: str
    amount_cents: int
    currency: str
    credits: int
    paid_at: datetime

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PurchaseEntry":
        return cls(
            payment_id=UUID(str(d["payment_id"])),
            product_id=d["product_id"],
            title=d["title"],
            amount_cents=int(d["amount_cents"]),
            currency=d["currency"],
            credits=int(d["credits"]),
            paid_at=_dt(d["paid_at"]),
        )


@dataclass(frozen=True, slots=True)
class UserContext:
    """Ergebnis von get_user_context: alles, was die KI ueber einen User wissen muss."""

    user: User
    memories: list[Memory] = field(default_factory=list)
    purchases: list[PurchaseEntry] = field(default_factory=list)
    last_offer_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UserContext":
        return cls(
            user=User.from_dict(d["user"]),
            memories=[Memory.from_dict(m) for m in d["memories"]],
            purchases=[PurchaseEntry.from_dict(p) for p in d["purchases"]],
            last_offer_at=_dt(d.get("last_offer_at")),
        )


@dataclass(frozen=True, slots=True)
class ChatMessage:
    id: int
    role: str  # 'user' | 'assistant'
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Product:
    product_id: str
    title: str
    description: str
    price_cents: int
    currency: str
    credits: int
    is_active: bool


@dataclass(frozen=True, slots=True)
class Payment:
    id: UUID
    provider: str
    provider_ref: str
    user_id: int | None
    product_id: str
    amount_cents: int
    currency: str
    credits: int
    status: str  # 'pending' | 'paid'
    created_at: datetime
    paid_at: datetime | None


@dataclass(frozen=True, slots=True)
class PaymentResult:
    payment: Payment
    newly_paid: bool  # False, wenn die Zahlung schon vorher verbucht war
    new_balance: int | None  # Guthaben danach, None wenn der User inzwischen geloescht ist


@dataclass(frozen=True, slots=True)
class MemoryResult:
    memory: Memory
    created: bool  # False, wenn der Fakt schon existierte (Duplikat)
