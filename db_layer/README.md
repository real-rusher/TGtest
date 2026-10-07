# botdb: Datenbankschicht (Builder 2)

> **Teil des Gesamtpakets.** Erweitert um Chatverlauf (`messages`), Zahlungen (`payments`),
> Angebots-Sperrzeit (`try_offer`) und KI-Hinweis (`mark_disclosed`). Anleitung: `README.md` im Hauptordner.

PostgreSQL Schema, Redis Cache und ein kapselndes Repository fuer den Telegram Bot.
Asynchron (asyncpg + redis.asyncio), passt also direkt zu aiogram oder python-telegram-bot.

## Aufbau

```
botdb/
  schema.sql      Tabellen, Enum, Indizes (idempotent, wird beim Start angewendet)
  repository.py   BotRepository: das einzige Modul mit SQL
  cache.py        Redis Cache fuer get_user_context
  models.py       Rueckgabeobjekte (Dataclasses, JSON-serialisierbar)
  errors.py       UserNotFound, ProductNotFound, InvalidFact
tests/
  test_repository.py   28 Integrationstests gegen echtes Postgres und Redis
```

## Einbindung

```python
from botdb import BotRepository

repo = await BotRepository.connect(
    dsn="postgresql://user:pass@host:5432/botdb",
    redis_url="redis://host:6379/0",   # optional, None = ohne Cache
)

# bei jeder eingehenden Nachricht
await repo.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)

ctx = await repo.get_user_context(user_id)       # UserContext oder None
await repo.record_memory(user_id, "Mag Warhammer", category="hobby")
await repo.record_purchase(user_id, "guide")     # im successful_payment Handler

await repo.close()   # beim Shutdown
```

## Kern-API laut Spezifikation

| Funktion | Verhalten |
|---|---|
| `get_user_context(user_id, memory_limit=50)` | Profil, neueste Memories, `purchases` (gekauft) und `open_offers` (angeboten). Alles aus einem konsistenten Snapshot. Gecacht in Redis. `None` bei unbekanntem User. |
| `record_purchase(user_id, product_id)` | Setzt Status auf `purchased`. Fehlt der Angebotseintrag, wird er direkt als Kauf angelegt. Idempotent: doppelt zugestellte Zahlungs-Updates zaehlen nur einmal (`newly_purchased` ist dann `False`). |
| `record_memory(user_id, fact_text, category="general")` | Normalisiert Leerzeichen, verhindert exakte Duplikate (gross/klein egal). `created` zeigt, ob neu angelegt. |

## Zusaetzliche Hilfsfunktionen

Ohne diese laesst sich die Kern-API nicht sinnvoll nutzen:
`upsert_user`, `set_ai_enabled`, `record_offer`, `upsert_product`, `get_product`, `list_active_products`.

## Designentscheidungen

* **Ein Datensatz pro User und Produkt** (`UNIQUE(user_id, product_id)`), der von `offered` zu `purchased` wandert. Ein spaeteres `record_offer` stuft einen Kauf nie zurueck.
* **TIMESTAMPTZ statt TIMESTAMP**: Zeiten werden zeitzonensicher gespeichert. Das ist die einzige bewusste Abweichung vom Schema in der Spezifikation.
* **Cache ist optional und faellt weich aus**: Ist Redis nicht erreichbar, wird nur eine Warnung geloggt und direkt aus Postgres gelesen. Jede schreibende Funktion leert den Cache des betroffenen Users.
* `upsert_user` leert den Cache nur bei neuem User oder geaendertem Namen. `last_active` im gecachten Kontext kann daher bis zur TTL (Standard 300 s) veraltet sein.
* Loeschen eines Users entfernt per `ON DELETE CASCADE` auch Memories und Kaeufe. Produkte mit Kaeufen koennen nicht geloescht werden, nur per `is_active = false` deaktiviert.
* Schema wird beim Start unter einem Advisory Lock angewendet, mehrere Bot-Instanzen koennen also gleichzeitig hochfahren.

## Tests

```
pip install -e ".[test]"
TEST_PG_DSN=postgresql://... TEST_REDIS_URL=redis://.../15 pytest
```

Achtung: Die Tests leeren das Schema `public` der angegebenen Datenbank. Nur eine eigene Testdatenbank verwenden.
