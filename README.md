# KI-Chat über Telegram

Ein Telegram-Account, über den eine KI-Persona chattet. Nutzer bekommen ein Gratis-Kontingent an Antworten
und können über externe Zahlungslinks Guthaben nachkaufen. Die KI merkt sich, was Nutzer über sich erzählen.

```
 Telegram ◄──► telegram-gateway ──POST /inbound──► orchestrator ◄──► PostgreSQL / Redis
                    ▲    (Userbot)                      │    ▲
                    └──────────POST /send───────────────┘    └── Stripe-Webhook
                                                        │
                                                   Sprachmodell
```

| Ordner | Aufgabe |
|---|---|
| `telegram-gateway/` | Telegram-Userbot: Nachrichten rein, Sendeaufträge mit Tipp-Status raus, `/pause` und `/resume` |
| `orchestrator/` | Gesprächssteuerung, Antwort-Generator, Gedächtnis, Guthaben, Zahlungslinks |
| `db_layer/` | Datenbankschicht (`botdb`): Schema, Repository, Cache |
| `config/` | Persona und Paketliste (Beispiele liegen bei) |

## Ablauf einer Nachricht

1. Das Gateway leitet die private Nachricht an `POST /inbound` weiter.
2. Der Orchestrator sammelt kurz hintereinander geschickte Nachrichten (`DEBOUNCE_SECONDS`) und beantwortet sie gemeinsam.
3. Beim ersten Kontakt geht zuerst der **KI-Hinweis** raus. Er lässt sich umformulieren, aber nicht abschalten.
4. Pro Antwort wird 1 Guthaben abgezogen. Ist keins mehr da, kommt ein **Zahlungslink**, höchstens alle `OFFER_COOLDOWN_HOURS` Stunden.
5. Die Antwort wird aus Persona, gespeicherten Fakten und Verlauf erzeugt, in kurze Nachrichten zerlegt und mit Tipp-Status verschickt.
6. Alle `FACT_EVERY` Nachrichten extrahiert ein zweiter Modellaufruf dauerhafte Fakten über den Nutzer.
7. Nach einer Zahlung bestätigt der Orchestrator den Eingang und beantwortet die zuletzt offene Nachricht.

Schlägt etwas fehl (Modell, Gateway, `/pause` mitten im Senden, Shutdown), wird das Guthaben zurückgebucht.

### Befehle für Nutzer

| Befehl | Wirkung |
|---|---|
| `/stop` | KI antwortet nicht mehr, Nachrichten werden weiter gespeichert |
| `/start` | KI-Antworten wieder an |
| `/balance` | Guthaben anzeigen |
| `/delete` | Warnt und erklärt die Bestätigung |
| `/delete confirm` | Löscht Profil, Fakten, Verlauf und Guthaben |

### Befehle für den Betreiber

`/pause` und `/resume` im jeweiligen Chat (vom eigenen Account aus), siehe `telegram-gateway/README.md`.
Während einer Pause schreibst du selbst, die KI bleibt still.

## Einrichtung

Voraussetzung: ein Server mit Docker und Docker Compose.

1. `.env.example` nach `.env` kopieren und ausfüllen.
2. `config/persona.example.md` nach `config/persona.md` kopieren und die Persona beschreiben.
3. `config/products.example.json` nach `config/products.json` kopieren und Pakete anpassen. Die Datei wird bei jedem Start übernommen.
4. Telegram einmalig einloggen (fragt Telefonnummer, Code und ggf. 2FA-Passwort):
   ```
   docker compose run --rm gateway python login.py
   ```
5. Starten:
   ```
   docker compose up -d --build
   docker compose logs -f orchestrator gateway
   ```

### Zahlungen

**Stripe:** Im Stripe-Dashboard einen Webhook auf `https://<deine-domain>/payments/stripe/webhook` anlegen, mit den Events
`checkout.session.completed` und `checkout.session.async_payment_succeeded`. Das Signing Secret kommt in `STRIPE_WEBHOOK_SECRET`.
Der Orchestrator lauscht nur auf `127.0.0.1:8090`. Davor gehört ein Reverse Proxy mit HTTPS (z. B. Caddy), der nur `/payments/` nach außen freigibt.

**Test ohne Geld:** `PAYMENT_PROVIDER=dummy` und `PUBLIC_BASE_URL` setzen. Der Zahlungslink führt dann auf eine Testseite
mit einem Knopf, der die Zahlung sofort verbucht. Niemals produktiv verwenden.

### Sprachmodell

Funktioniert mit jedem OpenAI-kompatiblen Endpunkt (`LLM_BASE_URL`, `LLM_MODEL`). Die Fakten-Extraktion braucht
Structured Outputs (`json_schema`), dafür kann mit `FACT_MODEL` ein anderes Modell gewählt werden.

## Tests

```
pip install -r telegram-gateway/requirements.txt
pip install -e "db_layer[test]" -e "orchestrator[test]"
python -m unittest discover -s telegram-gateway/tests
TEST_PG_DSN=postgresql://... TEST_REDIS_URL=redis://.../15 pytest db_layer
TEST_PG_DSN=postgresql://... pytest orchestrator
```

Die Tests leeren das Schema `public` der Testdatenbank. Ohne `TEST_PG_DSN` laufen nur die Tests ohne Datenbank.
GitHub Actions führt alles bei jedem Push aus (`.github/workflows/tests.yml`).

## Vor dem Livegang offen

- **Altersprüfung**: noch nicht umgesetzt.
- **Auslieferung digitaler Inhalte**: noch nicht umgesetzt, Pakete schreiben bisher nur Guthaben gut.
- **Rechtliches**: Datenschutzerklärung (Verlauf und Fakten werden gespeichert), Impressum, AGB, Widerrufsbelehrung für digitale Inhalte.
  Der Zahlungsanbieter verlangt einen verifizierten Kontoinhaber.

## Bekannte Grenzen

- Was du während `/pause` von Hand schreibst, landet nicht im Verlauf. Die KI kennt diese Nachrichten nach `/resume` nicht.
- Nach `/delete confirm` gilt ein Nutzer beim nächsten Kontakt als neu und bekommt erneut das Gratis-Kontingent.
- Telegram geht gegen automatisierte Nutzer-Accounts vor, besonders bei unaufgeforderten Nachrichten. Das System antwortet nur, es schreibt niemanden von sich aus an.
