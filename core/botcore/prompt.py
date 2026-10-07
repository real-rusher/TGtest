"""Systemprompt zusammenbauen und Angebots-Markierungen aus Antworten lesen."""

from __future__ import annotations

import re
from datetime import date

from botdb import UserContext

from .config import BotConfig

# Das Modell markiert ein Angebot mit [[OFFER:produkt_id]] am Ende seiner Antwort.
OFFER_TAG_RE = re.compile(r"\[\[\s*OFFER\s*:\s*([A-Za-z0-9_]+)\s*\]\]", re.IGNORECASE)
# Reste anderer Doppelklammer-Markierungen, die nie beim User ankommen sollen.
STRAY_TAG_RE = re.compile(r"\[\[[^\]]{0,80}\]\]")

STYLE_RULES = """## Schreibstil
- Du schreibst in einem Messenger. Kurze Nachrichten, natürlicher Ton, kein Markdown (keine Sternchen, Rauten, Listen).
- Trenne einzelne Gedanken durch Zeilenumbrüche, jede Zeile wird als eigene Nachricht verschickt.
- Halte Antworten knapp. Lieber zwei, drei kurze Nachrichten als ein langer Absatz."""

HONESTY_RULE = """## Feste Regel
Wenn dich jemand ernsthaft und direkt fragt, ob er mit einer KI oder einem Bot schreibt, streite das nicht ab. \
Du darfst dabei in deiner Rolle und deinem Tonfall bleiben, aber behaupte nicht, ein echter Mensch zu sein."""


def _user_section(ctx: UserContext | None) -> str:
    if ctx is None:
        return ""
    lines = ["## Was du über dein Gegenüber weißt"]
    name = ctx.user.first_name or ctx.user.username
    if name:
        lines.append(f"- Name: {name}")
    for memory in reversed(ctx.memories):  # älteste zuerst, liest sich natürlicher
        lines.append(f"- {memory.fact}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _price(product) -> str:
    if product.price_label:
        return product.price_label
    if product.price_stars:
        return f"{product.price_stars} Stars"
    return ""


def _sales_section(cfg: BotConfig, ctx: UserContext | None) -> str:
    if not cfg.sells:
        return ""
    owned = {p.product_id for p in ctx.purchases} if ctx else set()
    offered = {p.product_id for p in ctx.open_offers} if ctx else set()
    lines = [
        "## Produkte",
        "Du kannst passende Produkte anbieten. Biete nur an, wenn es natürlich zum Gespräch passt, "
        "nie aufdringlich und höchstens ein Produkt pro Antwort. Erfinde keine Produkte, Preise oder Links.",
        "Für ein Angebot schreibst du ganz am Ende deiner Antwort in einer eigenen Zeile: [[OFFER:<id>]]",
        "Die Rechnung bzw. den Bezahllink schickt das System danach automatisch. Schreib selbst keinen Link.",
        "",
        "Verfügbare Produkte:",
    ]
    for p in cfg.products:
        if p.id in owned:
            continue
        details = " | ".join(x for x in (p.title, _price(p), p.description) if x)
        note = " (wurde kürzlich angeboten, nicht drängeln)" if p.id in offered else ""
        lines.append(f"- {p.id}: {details}{note}")
    if owned:
        titles = [p.title for p in cfg.products if p.id in owned]
        lines.append("")
        lines.append("Bereits gekauft (nicht erneut anbieten): " + ", ".join(titles))
    return "\n".join(lines)


def build_system_prompt(cfg: BotConfig, ctx: UserContext | None, today: date | None = None) -> str:
    today = today or date.today()
    parts = [
        cfg.persona.strip(),
        STYLE_RULES,
        _user_section(ctx),
        _sales_section(cfg, ctx),
        HONESTY_RULE,
        f"Heutiges Datum: {today.strftime('%d.%m.%Y')}",
    ]
    return "\n\n".join(p for p in parts if p)


def split_offer(text: str) -> tuple[str, str | None]:
    """Entfernt Angebots-Markierungen aus der Antwort.

    Gibt den bereinigten Text und die erste markierte Produkt-ID (oder None) zurück.
    """
    match = OFFER_TAG_RE.search(text)
    product_id = match.group(1) if match else None
    cleaned = STRAY_TAG_RE.sub("", OFFER_TAG_RE.sub("", text))
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, product_id
