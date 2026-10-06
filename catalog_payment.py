"""
Builder 5: Catalog & Payment Workflow Logic

Business-Logik fuer Angebots-Validierung, Angebots-Historie und Auslieferung
digitaler Inhalte (Telegram file_ids).

Annahmen:
  * Datenbank: SQLite (per init_db() konfigurierbar).
  * Zahlung: Telegram Payments. Der successful_payment-Handler ruft
    record_payment() auf, fulfill_order() prueft danach gegen diese Tabelle.
  * "Kuerzlich angeboten" = innerhalb von OFFER_COOLDOWN_SECONDS.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
OFFER_COOLDOWN_SECONDS = 7 * 24 * 3600  # 7 Tage
_DB_PATH = "shop.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id  TEXT PRIMARY KEY,
    title       TEXT    NOT NULL,
    price       INTEGER NOT NULL,              -- kleinste Einheit (Cent)
    currency    TEXT    NOT NULL DEFAULT 'EUR',
    file_ids    TEXT    NOT NULL DEFAULT '[]', -- JSON: ["id", ...] oder [{"type":"photo","file_id":"..."}]
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS offers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    product_id  TEXT    NOT NULL REFERENCES products(product_id),
    status      TEXT    NOT NULL DEFAULT 'offered',   -- offered | purchased
    offered_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offers_lookup
    ON offers(user_id, product_id, offered_at);

CREATE TABLE IF NOT EXISTS payments (
    charge_id           TEXT PRIMARY KEY,         -- telegram_payment_charge_id
    provider_charge_id  TEXT,
    user_id             INTEGER NOT NULL,
    product_id          TEXT    NOT NULL REFERENCES products(product_id),
    amount              INTEGER NOT NULL,
    currency            TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'paid',  -- paid | fulfilled | refunded
    paid_at             INTEGER NOT NULL,
    fulfilled_at        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_payments_lookup
    ON payments(user_id, product_id, status);
"""

_OWNED = ("paid", "fulfilled")  # Status, die als "gekauft" gelten


# ---------------------------------------------------------------------------
# Fehler
# ---------------------------------------------------------------------------
class ShopError(Exception):
    """Basisklasse fuer alle Fehler dieses Moduls."""


class ProductNotFound(ShopError):
    pass


class PaymentNotVerified(ShopError):
    pass


class PaymentMismatch(ShopError):
    pass


class FulfillmentError(ShopError):
    pass


# ---------------------------------------------------------------------------
# DB-Helfer
# ---------------------------------------------------------------------------
def init_db(path: str = "shop.db") -> None:
    """Setzt den DB-Pfad und legt das Schema an (idempotent)."""
    global _DB_PATH
    _DB_PATH = path
    con = sqlite3.connect(_DB_PATH)
    try:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


@contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    """Schreib-Transaktion mit sofortiger Sperre (verhindert Race Conditions
    bei parallelen Updates desselben Users)."""
    con = sqlite3.connect(_DB_PATH, isolation_level=None, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def _now() -> int:
    return int(time.time())


def _normalize_file_ids(raw: str) -> list[dict]:
    """Akzeptiert ["id", ...] oder [{"type": ..., "file_id": ...}, ...]."""
    items = json.loads(raw or "[]")
    out = []
    for item in items:
        if isinstance(item, str):
            out.append({"type": "document", "file_id": item})
        elif isinstance(item, dict) and item.get("file_id"):
            out.append({"type": item.get("type", "document"), "file_id": item["file_id"]})
    return out


# ---------------------------------------------------------------------------
# 1) Angebots-Validierung
# ---------------------------------------------------------------------------
def validate_offer_eligibility(user_id: int, product_id: str) -> bool:
    """
    1. Bereits gekauft            -> False
    2. Kuerzlich angeboten        -> False
    3. Sonst: Angebot als 'offered' registrieren -> True
    Inaktive oder unbekannte Produkte -> False.
    """
    now = _now()
    with _tx() as con:
        if con.execute(
            "SELECT 1 FROM products WHERE product_id = ? AND active = 1",
            (product_id,),
        ).fetchone() is None:
            return False

        # 1. bereits gekauft?
        if con.execute(
            "SELECT 1 FROM payments WHERE user_id = ? AND product_id = ? "
            "AND status IN (?, ?) LIMIT 1",
            (user_id, product_id, *_OWNED),
        ).fetchone():
            return False

        # 2. kuerzlich angeboten?
        if con.execute(
            "SELECT 1 FROM offers WHERE user_id = ? AND product_id = ? "
            "AND offered_at >= ? LIMIT 1",
            (user_id, product_id, now - OFFER_COOLDOWN_SECONDS),
        ).fetchone():
            return False

        # 3. zulaessig -> registrieren
        con.execute(
            "INSERT INTO offers (user_id, product_id, status, offered_at) "
            "VALUES (?, ?, 'offered', ?)",
            (user_id, product_id, now),
        )
    return True


# ---------------------------------------------------------------------------
# Zahlungs-Event (vom successful_payment-Handler aufzurufen)
# ---------------------------------------------------------------------------
def record_payment(
    user_id: int,
    product_id: str,
    charge_id: str,
    amount: int,
    currency: str,
    provider_charge_id: str | None = None,
) -> bool:
    """
    Speichert ein bestaetigtes Zahlungs-Event, nachdem Betrag und Waehrung
    gegen den Katalog geprueft wurden. Idempotent: ein bereits bekanntes
    charge_id liefert False statt einen Doppel-Eintrag anzulegen.
    """
    with _tx() as con:
        product = con.execute(
            "SELECT price, currency FROM products WHERE product_id = ?",
            (product_id,),
        ).fetchone()
        if product is None:
            raise ProductNotFound(product_id)
        if amount != product["price"] or currency.upper() != product["currency"].upper():
            raise PaymentMismatch(
                f"erwartet {product['price']} {product['currency']}, "
                f"erhalten {amount} {currency}"
            )

        cur = con.execute(
            "INSERT OR IGNORE INTO payments "
            "(charge_id, provider_charge_id, user_id, product_id, amount, currency, status, paid_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'paid', ?)",
            (charge_id, provider_charge_id, user_id, product_id, amount,
             currency.upper(), _now()),
        )
        return cur.rowcount == 1


# ---------------------------------------------------------------------------
# 2) Auslieferung
# ---------------------------------------------------------------------------
def fulfill_order(user_id: int, product_id: str) -> dict:
    """
    1. Verifiziert, dass eine gueltige Zahlung (paid/fulfilled) existiert.
    2. Laedt die Telegram file_ids aus `products`.
    3. Gibt den Auslieferungs-Payload fuer den Dispatcher zurueck.

    Erneuter Aufruf nach erfolgreicher Auslieferung ist erlaubt (Redelivery,
    z.B. /meinekaeufe) und wird im Payload markiert.
    Wirft PaymentNotVerified, ProductNotFound oder FulfillmentError.
    """
    now = _now()
    with _tx() as con:
        payment = con.execute(
            "SELECT * FROM payments WHERE user_id = ? AND product_id = ? "
            "AND status IN (?, ?) ORDER BY paid_at DESC LIMIT 1",
            (user_id, product_id, *_OWNED),
        ).fetchone()
        if payment is None:
            raise PaymentNotVerified(f"keine Zahlung fuer user={user_id}, product={product_id}")

        product = con.execute(
            "SELECT title, file_ids FROM products WHERE product_id = ?",
            (product_id,),
        ).fetchone()
        if product is None:
            raise ProductNotFound(product_id)

        files = _normalize_file_ids(product["file_ids"])
        if not files:
            raise FulfillmentError(f"Produkt {product_id} hat keine file_ids")

        redelivery = payment["status"] == "fulfilled"
        if not redelivery:
            con.execute(
                "UPDATE payments SET status = 'fulfilled', fulfilled_at = ? WHERE charge_id = ?",
                (now, payment["charge_id"]),
            )
            con.execute(
                "UPDATE offers SET status = 'purchased' "
                "WHERE user_id = ? AND product_id = ? AND status = 'offered'",
                (user_id, product_id),
            )

    return {
        "chat_id": user_id,
        "order_id": payment["charge_id"],
        "product_id": product_id,
        "title": product["title"],
        "redelivery": redelivery,
        "messages": [
            {"method": f"send_{f['type']}", "file_id": f["file_id"]}
            for f in files
        ],
    }
