"""Hilfsfunktionen für die Zahlungsarten.

  * stars:  Rechnung über Telegram, Bestätigung kommt vom Telegram-Modul (/payment)
  * stripe: Stripe Payment Link mit client_reference_id, Bestätigung per Stripe-Webhook
  * link:   beliebiger Bezahllink, Bestätigung per eigenem Webhook oder manuell (Admin)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import Product

STRIPE_TOLERANCE_SECONDS = 300


class SignatureError(ValueError):
    pass


def reference_id(user_id: int, product_id: str) -> str:
    """Kennung, die beim Bezahlen mitläuft und User + Produkt zuordnet."""
    return f"u{user_id}-{product_id}"


def parse_reference_id(ref: str | None) -> tuple[int, str] | None:
    if not ref or not ref.startswith("u") or "-" not in ref:
        return None
    user_part, product_id = ref[1:].split("-", 1)
    if not user_part.isdigit() or not product_id:
        return None
    return int(user_part), product_id


def _add_query(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit(parts._replace(query=urlencode(query)))


def payment_link(method: str, product: Product, user_id: int) -> str | None:
    """Personalisierter Bezahllink für stripe und link, sonst None."""
    if method == "stripe" and product.stripe_link:
        return _add_query(product.stripe_link, {"client_reference_id": reference_id(user_id, product.id)})
    if method == "link" and product.link:
        return product.link.replace("{user_id}", str(user_id)).replace("{product_id}", product.id).replace(
            "{ref}", reference_id(user_id, product.id)
        )
    return None


def verify_stripe_signature(payload: bytes, header: str | None, secret: str, now: float | None = None) -> dict:
    """Prüft die Stripe-Signatur (Header Stripe-Signature) und gibt das Event zurück."""
    if not header:
        raise SignatureError("Stripe-Signature fehlt")
    timestamp = None
    signatures: list[str] = []
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    if not timestamp or not timestamp.isdigit() or not signatures:
        raise SignatureError("Stripe-Signature unvollständig")
    now = time.time() if now is None else now
    if abs(now - int(timestamp)) > STRIPE_TOLERANCE_SECONDS:
        raise SignatureError("Stripe-Signature zu alt")
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise SignatureError("Stripe-Signature ungültig")
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise SignatureError("Stripe-Event ist kein JSON") from exc
