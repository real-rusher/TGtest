"""Stripe Checkout über die REST-API (ohne Stripe-SDK).

Ablauf:
1. create_checkout legt eine Checkout Session an und liefert deren URL als Zahlungslink.
2. Stripe ruft nach der Zahlung den Webhook auf (checkout.session.completed oder
   checkout.session.async_payment_succeeded). parse_webhook prüft die Signatur und
   gibt ein PaymentEvent zurück.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Mapping

import aiohttp

from botdb import Product

from . import CheckoutLink, InvalidWebhook, PaymentEvent

log = logging.getLogger(__name__)

API_BASE = "https://api.stripe.com"
SIGNATURE_TOLERANCE = 300  # Sekunden, wie in Stripes eigenen Bibliotheken
PAID_EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded"}


class StripeError(RuntimeError):
    pass


def verify_signature(
    body: bytes, header: str | None, secret: str, *, now: float | None = None, tolerance: int = SIGNATURE_TOLERANCE
) -> None:
    """Prüft den Stripe-Signature-Header. Wirft InvalidWebhook bei Fehler."""
    if not header:
        raise InvalidWebhook("Stripe-Signature fehlt")
    timestamp = None
    signatures: list[str] = []
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        raise InvalidWebhook("Stripe-Signature unvollständig")
    try:
        ts = int(timestamp)
    except ValueError:
        raise InvalidWebhook("Zeitstempel ungültig") from None
    if abs((now if now is not None else time.time()) - ts) > tolerance:
        raise InvalidWebhook("Zeitstempel außerhalb der Toleranz")

    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise InvalidWebhook("Signatur stimmt nicht")


def sign(body: bytes, secret: str, timestamp: int | None = None) -> str:
    """Erzeugt einen Stripe-Signature-Header (für Tests und lokale Simulation)."""
    ts = timestamp if timestamp is not None else int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


class StripeProvider:
    name = "stripe"

    def __init__(
        self,
        secret_key: str,
        webhook_secret: str,
        success_url: str,
        cancel_url: str | None = None,
        *,
        api_base: str = API_BASE,
    ) -> None:
        self._secret_key = secret_key
        self._webhook_secret = webhook_secret
        self._success_url = success_url
        self._cancel_url = cancel_url
        self._api_base = api_base.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._secret_key}"},
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def create_checkout(self, user_id: int, product: Product) -> CheckoutLink:
        form = {
            "mode": "payment",
            "success_url": self._success_url,
            "client_reference_id": str(user_id),
            "metadata[user_id]": str(user_id),
            "metadata[product_id]": product.product_id,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": product.currency.lower(),
            "line_items[0][price_data][unit_amount]": str(product.price_cents),
            "line_items[0][price_data][product_data][name]": product.title,
        }
        if product.description:
            form["line_items[0][price_data][product_data][description]"] = product.description
        if self._cancel_url:
            form["cancel_url"] = self._cancel_url

        try:
            async with self._http().post(f"{self._api_base}/v1/checkout/sessions", data=form) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise StripeError(f"Stripe nicht erreichbar: {exc!r}") from exc
        if resp.status >= 300 or not isinstance(data, dict) or "url" not in data:
            message = data.get("error", {}).get("message") if isinstance(data, dict) else data
            raise StripeError(f"Stripe HTTP {resp.status}: {message}")
        return CheckoutLink(provider=self.name, ref=data["id"], url=data["url"])

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> PaymentEvent | None:
        verify_signature(body, headers.get("Stripe-Signature"), self._webhook_secret)
        try:
            event = json.loads(body)
            event_type = event["type"]
            obj = event["data"]["object"]
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidWebhook(f"Event nicht lesbar: {exc}") from exc

        if event_type not in PAID_EVENTS:
            return None
        # Bei verzögerten Zahlarten (z. B. SEPA) kommt completed mit "unpaid",
        # das Geld später mit async_payment_succeeded.
        if obj.get("payment_status") != "paid":
            log.info("Checkout %s abgeschlossen, aber noch nicht bezahlt", obj.get("id"))
            return None
        try:
            return PaymentEvent(
                provider=self.name,
                ref=obj["id"],
                amount_cents=int(obj["amount_total"]),
                currency=str(obj["currency"]).upper(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidWebhook(f"Checkout Session unvollständig: {exc}") from exc
