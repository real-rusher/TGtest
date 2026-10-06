import json

import pytest

from orchestrator.catalog import Catalog, CatalogError, resolve_products, strip_urls

ITEMS = [
    {"id": "jacke-1", "name": "Winterjacke Nordlicht", "url": "https://shop.test/jacke",
     "description": "Warm   und\nwasserdicht", "price": "89,90 €", "tags": ["winter", "jacke"], "featured": True},
    {"id": "muetze-2", "name": "Strickmütze", "url": "https://shop.test/muetze?farbe=rot", "tags": ["winter"]},
    {"id": "shorts-3", "name": "Laufshorts", "url": "https://shop.test/shorts", "tags": ["sommer", "laufen"]},
    {"id": "socken-4", "name": "Wandersocken", "url": "https://shop.test/socken", "description": "Für lange Touren"},
]


def catalog(**kw):
    return Catalog.from_list(ITEMS, **kw)


def test_laden_aus_datei(tmp_path):
    path = tmp_path / "katalog.json"
    path.write_text(json.dumps(ITEMS), "utf-8")
    c = Catalog.from_file(str(path))
    assert len(c) == 4
    item = c.get("JACKE-1")
    assert item.name == "Winterjacke Nordlicht" and item.description == "Warm und wasserdicht"
    assert item.tags == ("winter", "jacke") and item.featured


@pytest.mark.parametrize(
    "raw, fehler",
    [
        ([], "nicht leere"),
        ({"id": "x"}, "nicht leere"),
        (["x"], "kein Objekt"),
        ([{"id": "a b", "name": "n", "url": "https://x"}], "id"),
        ([{"id": "a", "name": "", "url": "https://x"}], "name"),
        ([{"id": "a", "name": "n", "url": "ftp://x"}], "url"),
        ([{"id": "a", "name": "n", "url": "javascript:alert(1)"}], "url"),
        ([{"id": "a", "name": "n", "url": "https://x"}, {"id": "A", "name": "m", "url": "https://y"}], "doppelt"),
        ([{"id": "a", "name": "n", "url": "https://x", "tags": "winter"}], "tags"),
    ],
)
def test_ungueltige_kataloge(raw, fehler):
    with pytest.raises(CatalogError, match=fehler):
        Catalog.from_list(raw)


def test_datei_fehlt():
    with pytest.raises(CatalogError):
        Catalog.from_file("/gibt/es/nicht.json")


def test_link_parameter():
    c = catalog(link_query="?utm_source=telegram&utm_medium=chat")
    assert c.url_for(c.get("jacke-1")) == "https://shop.test/jacke?utm_source=telegram&utm_medium=chat"
    assert c.url_for(c.get("muetze-2")) == "https://shop.test/muetze?farbe=rot&utm_source=telegram&utm_medium=chat"
    assert catalog().url_for(catalog().get("jacke-1")) == "https://shop.test/jacke"


def test_auswahl():
    c = catalog()
    assert len(c.select("egal", 10)) == 4  # klein genug: alles
    assert [i.id for i in c.select("ich gehe laufen im sommer", 1)] == ["shorts-3"]
    assert [i.id for i in c.select("lange touren wandern", 1)] == ["socken-4"]
    # Nichts passt: hervorgehobene Produkte zuerst, dann Katalogreihenfolge
    assert [i.id for i in c.select("xyz", 2)] == ["jacke-1", "muetze-2"]


def test_produkte_aufloesen():
    c = catalog(link_query="ref=tg")
    out = resolve_products("Schau dir mal [[jacke-1]] an, dazu passt [[ muetze-2 ]].", c)
    assert out == (
        "Schau dir mal Winterjacke Nordlicht an, dazu passt Strickmütze.\n"
        "Winterjacke Nordlicht: https://shop.test/jacke?ref=tg\n"
        "Strickmütze: https://shop.test/muetze?farbe=rot&ref=tg"
    )


def test_unbekannte_ids_und_doppelte():
    c = catalog()
    out = resolve_products("Nimm [[fantasie-99]] oder [[jacke-1]] , [[jacke-1]] ist top", c)
    assert out == (
        "Nimm oder Winterjacke Nordlicht, Winterjacke Nordlicht ist top\n"
        "Winterjacke Nordlicht: https://shop.test/jacke"
    )


def test_maximal_links():
    out = resolve_products("[[jacke-1]] [[muetze-2]] [[shorts-3]] [[socken-4]]", catalog(), max_links=2)
    assert out.count("https://") == 2


def test_nur_produkt_ohne_text():
    assert resolve_products("[[shorts-3]]", catalog()) == "Laufshorts\nLaufshorts: https://shop.test/shorts"


def test_ohne_katalog_werden_platzhalter_entfernt():
    assert resolve_products("hey [[x]] du", None) == "hey du"


def test_links_aus_modelltext_entfernen():
    text = "Gute Wahl! Guck auf https://evil.test/a?b=1 nach. Sonst frag mich.\nwww.fake.test/x\nTschüss"
    assert strip_urls(text) == "Gute Wahl! Sonst frag mich.\nTschüss"
    assert strip_urls("keine links hier, nur 3.5 Punkte") == "keine links hier, nur 3.5 Punkte"
    assert strip_urls("https://a.test") == ""
