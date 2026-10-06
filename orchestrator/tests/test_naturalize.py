from orchestrator.naturalize import MAX_DELAY, calc_delay, naturalize_response


def test_grundfall():
    out = naturalize_response("Hahaha okay. und was hast du danach gemacht?\nWar echt krass... Erzähl!")
    assert out["chunks"] == ["Hahaha okay", "und was hast du danach gemacht?", "War echt krass...", "Erzähl!"]
    assert out["delays"] == [calc_delay(c) for c in out["chunks"]]
    assert out["delays"][0] == round(1.0 + len("Hahaha okay") * 0.05, 2)


def test_leer():
    for value in ("", "   \n  ", None, 42):
        assert naturalize_response(value) == {"chunks": [], "delays": []}


def test_abkuerzungen_ordinalzahlen_initialen():
    out = naturalize_response("Ich mag z.B. Pizza. Am 3. Mai komm ich. Peter M. Müller war da.")
    assert out["chunks"] == ["Ich mag z.B. Pizza", "Am 3. Mai komm ich", "Peter M. Müller war da"]


def test_abkuerzung_am_ende_bleibt():
    assert naturalize_response("Brot, Käse usw.")["chunks"] == ["Brot, Käse usw."]


def test_dezimalzahlen_und_links():
    out = naturalize_response("Kostet 3.50 Euro. Schau hier: https://example.com/a.b_c?x=1.")
    assert out["chunks"] == ["Kostet 3.50 Euro", "Schau hier: https://example.com/a.b_c?x=1"]


def test_delay_gedeckelt():
    long = "wort " * 300
    out = naturalize_response(long)
    assert all(d <= MAX_DELAY for d in out["delays"])
    assert max(out["delays"]) == MAX_DELAY


def test_lange_chunks_werden_geteilt():
    text = " ".join(["abcdefghi"] * 300)  # ~3000 Zeichen ohne Satzende
    out = naturalize_response(text, max_chunk_chars=1000)
    assert len(out["chunks"]) == 3
    assert all(len(c) <= 1000 for c in out["chunks"])
    assert " ".join(out["chunks"]) == text


def test_markdown_wird_entfernt():
    out = naturalize_response("## Titel\n- **eins**\n• zwei\n*lacht* ok")
    assert out["chunks"] == ["Titel", "eins", "zwei", "*lacht* ok"]
