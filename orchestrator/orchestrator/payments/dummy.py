"""Test-Zahlungsanbieter ohne echtes Geld.

Der Link führt auf eine Seite des Orchestrators mit einem Knopf "Testzahlung abschließen".
Nur für Entwicklung und Tests, niemals produktiv einsetzen.
"""

from __future__ import annotations

import secrets
from typing import Mapping

from botdb import Product

from . import CheckoutLink, PaymentEvent


class DummyProvider:
    name = "dummy"

    def __init__(self, public_base_url: str) -> None:
        self._base = public_base_url.rstrip("/")

    async def create_checkout(self, user_id: int, product: Product) -> CheckoutLink:
        ref = f"dummy_{secrets.token_urlsafe(12)}"
        return CheckoutLink(provider=self.name, ref=ref, url=f"{self._base}/payments/dummy/{ref}")

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> PaymentEvent | None:
        return None  # Zahlungen kommen über die Testseite, nicht über Webhooks

    async def close(self) -> None:
        return None
