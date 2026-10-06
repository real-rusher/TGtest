"""Zahlungsanbieter für externe Zahlungslinks.

Ein Anbieter erzeugt einen Link für ein Paket und übersetzt seine Webhooks in ein
einheitliches PaymentEvent. Neue Anbieter implementieren das PaymentProvider-Protokoll.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from botdb import Product


@dataclass(frozen=True, slots=True)
class CheckoutLink:
    provider: str
    ref: str   # eindeutige Referenz beim Anbieter (z. B. Stripe Checkout Session ID)
    url: str


@dataclass(frozen=True, slots=True)
class PaymentEvent:
    """Eine vom Anbieter bestätigte, erfolgreiche Zahlung."""

    provider: str
    ref: str
    amount_cents: int
    currency: str


class InvalidWebhook(Exception):
    """Signatur ungültig oder Inhalt nicht lesbar."""


class PaymentProvider(Protocol):
    name: str

    async def create_checkout(self, user_id: int, product: Product) -> CheckoutLink: ...

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> PaymentEvent | None:
        """PaymentEvent bei erfolgreicher Zahlung, None bei irrelevanten Events.

        Wirft InvalidWebhook bei falscher Signatur.
        """
        ...

    async def close(self) -> None: ...


def format_price(cents: int, currency: str) -> str:
    amount = f"{cents / 100:.2f}".replace(".", ",")
    symbols = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF"}
    symbol = symbols.get(currency.upper(), currency.upper())
    return f"{amount} {symbol}"


__all__ = [
    "CheckoutLink",
    "InvalidWebhook",
    "PaymentEvent",
    "PaymentProvider",
    "format_price",
]
