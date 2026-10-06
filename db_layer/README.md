# botdb: Datenbankschicht

PostgreSQL-Schema, Redis-Cache und ein kapselndes Repository. Asynchron (asyncpg und redis.asyncio).
Wird vom Orchestrator benutzt, funktioniert aber auch eigenständig.

## Aufbau

```
botdb/
  schema.sql      Tabellen, Enum, Indizes (idempotent, wird beim Start angewendet)
  repository.py   BotRepository: das einzige Modul mit SQL
  cache.py        Redis-Cache für get_user_context (optional, fällt weich aus)
  models.py       Rückgabeobjekte (Dataclasses)
  errors.py       UserNotFound, ProductNotFound, InvalidFact, InvalidMessage,
                  PaymentNotFound, PaymentMismatch
tests/
  test_repository.py   Integrationstests gegen echtes Postgres und Redis
```

## Tabellen

| Tabelle | Inhalt |
|---|---|
| `users` | Profil, `ai_enabled` (aus nach `/stop`), `credits` (verbleibende KI-Antworten), `disclosed_at` (KI-Hinweis verschickt) |
| `memories` | Dauerhafte Fakten über den User, ohne exakte Duplikate |
| `messages` | Chatverlauf (`user` / `assistant`) |
| `products` | Pakete: Preis in Cent, Währung, gutgeschriebene `credits` |
| `offers` | Wann wem welches Paket angeboten wurde (für den Cooldown) |
| `payments` | Zahlungen über externe Links: `pending` bis zum Webhook, dann `paid` |

Löschen eines Users (`delete_user`) entfernt Profil, Fakten, Verlauf und Angebote per `ON DELETE CASCADE`.
Zahlungen bleiben für die Buchhaltung erhalten, ihre `user_id` wird auf `NULL` gesetzt.

## API

```python
from botdb import BotRepository

repo = await BotRepository.connect("postgresql://...", "redis://...")  # Redis optional

user, created = await repo.upsert_user(uid, username, first_name, initial_credits=20)
ctx = await repo.get_user_context(uid)          # Profil, Fakten, Käufe, letztes Angebot
await repo.add_message(uid, "user", "hallo")
history = await repo.get_recent_messages(uid, 30)
await repo.record_memory(uid, "fährt Motorrad")

balance = await repo.consume_credit(uid)        # None, wenn nichts mehr da ist
await repo.add_credits(uid, 1)

product = await repo.get_product("small")
await repo.create_payment("stripe", session_id, uid, product)
result = await repo.complete_payment("stripe", session_id, 299, "EUR")  # idempotent
```

| Funktion | Verhalten |
|---|---|
| `upsert_user` | Legt an oder aktualisiert Namen und `last_active`. `initial_credits` nur beim Anlegen. |
| `get_user_context` | Ein konsistenter Snapshot, gecacht in Redis. `None` bei unbekanntem User. |
| `consume_credit` | Atomar, nie unter 0, auch bei parallelen Aufrufen. |
| `complete_payment` | Prüft Betrag und Währung gegen den angelegten Auftrag, bucht Guthaben genau einmal gut. |
| `mark_disclosed` | `True` nur beim ersten Aufruf. |
| `delete_user` | Löscht alles außer anonymisierten Zahlungen. |

## Designentscheidungen

* **Guthaben statt Einzelkäufen**: Ein Paket kann beliebig oft gekauft werden, jede Zahlung schreibt `credits` gut.
* **Zahlungen sind idempotent**: `UNIQUE(provider, provider_ref)` plus `SELECT ... FOR UPDATE`. Doppelt zugestellte Webhooks verbuchen nichts doppelt.
* **TIMESTAMPTZ** überall, Zeiten sind zeitzonensicher.
* **Cache fällt weich aus**: Ist Redis nicht erreichbar, wird nur gewarnt und direkt aus Postgres gelesen. Jede schreibende Funktion leert den Cache des betroffenen Users.
* Schema wird beim Start unter einem Advisory Lock angewendet, mehrere Instanzen können gleichzeitig hochfahren.

## Tests

```
pip install -e ".[test]"
TEST_PG_DSN=postgresql://... TEST_REDIS_URL=redis://.../15 pytest
```

Achtung: Die Tests leeren das Schema `public` der angegebenen Datenbank. Nur eine eigene Testdatenbank verwenden.
