"""Produktkatalog für den Shop-Modus.

Die KI nennt Produkte nur über ihre ID in doppelten eckigen Klammern, z. B. [[jacke-01]].
Der Orchestrator ersetzt das durch den Produktnamen und hängt den echten Link an.
So kann das Modell weder Links noch Produkte erfinden.

Katalogdatei (JSON-Liste):
    [{"id": "jacke-01", "name": "Winterjacke", "url": "https://shop.example/jacke",
      "description": "...", "price": "89,90 €", "tags": ["winter"], "featured": true}]
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

PRODUCT_TOKEN = re.compile(r"\[\[\s*([A-Za-z0-9_.:-]{1,64})\s*\]\]")
# Satzzeichen am Ende gehören nicht zum Link ("schau auf x.de!")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]*[^\s<>\"')\].,!?;:]", re.IGNORECASE)
_WORD = re.compile(r"[a-zäöüß0-9]{3,}")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class CatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogItem:
    id: str
    name: str
    url: str
    description: str = ""
    price: str = ""
    tags: tuple[str, ...] = ()
    featured: bool = False

    def prompt_line(self) -> str:
        parts = [f"[[{self.id}]] {self.name}"]
        if self.price:
            parts.append(self.price)
        if self.description:
            parts.append(self.description)
        if self.tags:
            parts.append("Stichworte: " + ", ".join(self.tags))
        return "- " + " | ".join(parts)


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _with_query(url: str, query: str | None) -> str:
    if not query:
        return url
    parts = urlsplit(url)
    joined = f"{parts.query}&{query}" if parts.query else query
    return urlunsplit((parts.scheme, parts.netloc, parts.path, joined, parts.fragment))


class Catalog:
    def __init__(self, items: list[CatalogItem], *, link_query: str | None = None) -> None:
        self._items = list(items)
        self._by_id = {item.id.lower(): item for item in self._items}
        self._link_query = (link_query or "").lstrip("?&") or None
        self._index = [
            (item, _words(item.name), _words(" ".join(item.tags)), _words(item.description))
            for item in self._items
        ]

    @classmethod
    def from_file(cls, path: str, *, link_query: str | None = None) -> "Catalog":
        try:
            raw = json.loads(Path(path).read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise CatalogError(f"Katalog {path} nicht lesbar: {exc}") from None
        return cls.from_list(raw, link_query=link_query)

    @classmethod
    def from_list(cls, raw: object, *, link_query: str | None = None) -> "Catalog":
        if not isinstance(raw, list) or not raw:
            raise CatalogError("Katalog muss eine nicht leere JSON-Liste sein")
        items: list[CatalogItem] = []
        seen: set[str] = set()
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise CatalogError(f"Eintrag {i} ist kein Objekt")
            item_id = str(entry.get("id", "")).strip()
            name = str(entry.get("name", "")).strip()
            url = str(entry.get("url", "")).strip()
            if not _ID_RE.match(item_id):
                raise CatalogError(f"Eintrag {i}: id fehlt oder enthält ungültige Zeichen")
            if item_id.lower() in seen:
                raise CatalogError(f"Eintrag {i}: id {item_id!r} doppelt")
            if not name:
                raise CatalogError(f"Eintrag {i} ({item_id}): name fehlt")
            if not url.startswith(("https://", "http://")):
                raise CatalogError(f"Eintrag {i} ({item_id}): url muss mit http(s):// beginnen")
            tags = entry.get("tags", [])
            if not isinstance(tags, list):
                raise CatalogError(f"Eintrag {i} ({item_id}): tags muss eine Liste sein")
            seen.add(item_id.lower())
            items.append(
                CatalogItem(
                    id=item_id,
                    name=name,
                    url=url,
                    description=" ".join(str(entry.get("description", "")).split()),
                    price=str(entry.get("price", "")).strip(),
                    tags=tuple(str(t).strip() for t in tags if str(t).strip()),
                    featured=bool(entry.get("featured", False)),
                )
            )
        return cls(items, link_query=link_query)

    def __len__(self) -> int:
        return len(self._items)

    def get(self, item_id: str) -> CatalogItem | None:
        return self._by_id.get(item_id.lower())

    def url_for(self, item: CatalogItem) -> str:
        return _with_query(item.url, self._link_query)

    def select(self, query: str, limit: int) -> list[CatalogItem]:
        """Die für die Anfrage passendsten Produkte. Kleine Kataloge kommen komplett."""
        if len(self._items) <= limit:
            return list(self._items)
        words = _words(query)
        scored = []
        for pos, (item, name, tags, desc) in enumerate(self._index):
            score = 3 * len(words & name) + 2 * len(words & tags) + len(words & desc)
            if item.featured:
                score += 0.5
            scored.append((-score, pos, item))
        scored.sort()
        return [item for _, _, item in scored[:limit]]


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def strip_urls(text: str) -> str:
    """Entfernt Sätze mit Links aus Modelltext. Echte Links setzt nur der Orchestrator ein.

    Ganze Sätze statt nur der URL, weil sonst Reste wie "Mehr findest du auf!" übrig bleiben.
    """
    lines = []
    for line in text.splitlines():
        kept = [part for part in _SENTENCE_END.split(line) if not URL_RE.search(part)]
        lines.append(" ".join(kept).strip())
    return "\n".join(line for line in lines if line).strip()


def resolve_products(text: str, catalog: Catalog | None, *, max_links: int = 3) -> str:
    """Ersetzt [[id]] durch den Produktnamen und hängt die Links als eigene Zeilen an.

    Unbekannte IDs werden still entfernt (das Modell hat sich etwas ausgedacht).
    """
    linked: list[CatalogItem] = []

    def replace(match: re.Match) -> str:
        item = catalog.get(match.group(1)) if catalog is not None else None
        if item is None:
            log.warning("Unbekannte Produkt-ID %r in der Antwort entfernt", match.group(1))
            return ""
        if item not in linked:
            linked.append(item)
        return item.name

    body = PRODUCT_TOKEN.sub(replace, text)
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"\s+([,.!?])", r"\1", body).strip()
    if catalog is None or not linked:
        return body
    links = [f"{item.name}: {catalog.url_for(item)}" for item in linked[:max_links]]
    return "\n".join([body, *links]) if body else "\n".join(links)
