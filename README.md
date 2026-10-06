# KI-Chat über Telegram

Ein Telegram-Account, über den eine KI-Persona chattet. Ein Schalter in der `.env` legt fest, wofür:

| `BOT_MODE` | Wofür | Geld |
|---|---|---|
| `chat` | KI-Chat als Produkt | Nutzer bekommen Gratis-Antworten und kaufen Guthaben über Zahlungslinks (Stripe) |
| `shop` | Produktberatung für einen Online-Shop | Chatten ist gratis, die KI empfiehlt Produkte aus einem Katalog und verlinkt in den Shop |

In beiden Modi merkt sich die KI, was Nutzer über sich erzählen, und schreibt beim ersten Kontakt einen KI-Hinweis.

```
 Telegram ◄──► telegram-gateway ──POST /inbound──► orchestrator ◄──► PostgreSQL / Redis
                    ▲    (Userbot)                      │    ▲
                    └──────────POST /send───────────────┘    └── Stripe-Webhook (nur chat)
                                                        │
                                                   Sprachmodell
```

| Ordner | Aufgabe |
|---|---|
| `telegram-gateway/` | Telegram-Userbot: Nachrichten rein, Sendeaufträge mit Tipp-Status raus, `/pause` und `/resume` |
| `orchestrator/` | Gesprächssteuerung, Antwort-Generator, Gedächtnis, Guthaben, Zahlungslinks, Produktkatalog |
| `db_layer/` | Datenbankschicht (`botdb`): Schema, Repository, Cache |
| `config/` | Persona, Pakete (chat) oder Katalog (shop), Beispiele liegen bei |

## Ablauf einer Nachricht

1. Das Gateway leitet die private Nachricht an `POST /inbound` weiter.
2. Der Orchestrator sammelt kurz hintereinander geschickte Nachrichten (`DEBOUNCE_SECONDS`) und beantwortet sie gemeinsam.
3. Beim ersten Kontakt geht zuerst der **KI-Hinweis** (`TEXT_DISCLOSURE`) raus. Er ist Pflicht, ohne ihn startet der Orchestrator nicht.
4. Ist das Tageslimit (`DAILY_REPLY_LIMIT`) erreicht, bleibt die KI still.
5. **Nur chat:** Pro Antwort wird 1 Guthaben abgezogen. Ist keins mehr da, kommt ein Zahlungslink, höchstens alle `OFFER_COOLDOWN_HOURS` Stunden.
6. Die Antwort wird aus Persona, gespeicherten Fakten und Verlauf erzeugt. Links, die das Modell selbst schreibt, werden immer entfernt.
7. **Nur shop:** Die KI nennt Produkte als `[[id]]`. Der Orchestrator setzt den Produktnamen ein und hängt den echten Link aus dem Katalog an. Unbekannte IDs werden entfernt, das Modell kann also keine Produkte oder Links erfinden.
8. Die Antwort wird in kurze Nachrichten zerlegt und mit Tipp-Status verschickt.
9. Alle `FACT_EVERY` Nachrichten extrahiert ein zweiter Modellaufruf dauerhafte Fakten über den Nutzer.

Schlägt im Chat-Modus etwas fehl (Modell, Gateway, `/pause` mitten im Senden, Shutdown), wird das Guthaben zurückgebucht.

### Befehle für Nutzer

| Befehl | Wirkung | Modus |
|---|---|---|
| `/stop` | KI antwortet nicht mehr, Nachrichten werden weiter gespeichert | beide |
| `/start` | KI-Antworten wieder an | beide |
| `/delete` | Warnt und erklärt die Bestätigung | beide |
| `/delete confirm` | Löscht Profil, Fakten, Verlauf (und Guthaben) | beide |
| `/balance` | Guthaben anzeigen | chat |

### Befehle für den Betreiber

`/pause` und `/resume` im jeweiligen Chat (vom Bot-Account aus), siehe `telegram-gateway/README.md`.
Während einer Pause schreibst du selbst, die KI bleibt still.

## Einrichtung

Voraussetzung: ein Server mit Docker und Docker Compose.

1. Passende Vorlage nach `.env` kopieren und ausfüllen: `.env.chat.example` oder `.env.shop.example`.
2. Persona anlegen: `config/persona.chat.example.md` oder `config/persona.shop.example.md` nach `config/persona.md` kopieren und anpassen.
3. **chat:** `config/products.example.json` nach `config/products.json` kopieren und Pakete anpassen.
   **shop:** `config/catalog.example.json` nach `config/catalog.json` kopieren und mit den Produkten des Shops füllen.
   Beide Dateien werden bei jedem Start neu eingelesen.
4. Telegram einmalig einloggen (fragt Telefonnummer, Code und ggf. 2FA-Passwort):
   ```
   docker compose run --rm gateway python login.py
   ```
5. Starten:
   ```
   docker compose up -d --build
   docker compose logs -f orchestrator gateway
   ```

Änderungen an `.env`, Persona, Paketen oder Katalog übernimmst du mit `docker compose restart orchestrator`.

### Katalog (shop)

```json
[
  {
    "id": "jacke-nordlicht",
    "name": "Winterjacke Nordlicht",
    "url": "https://shop.example/produkte/winterjacke-nordlicht",
    "price": "189,00 €",
    "description": "Daunenjacke bis -20 °C",
    "tags": ["winter", "jacke"],
    "featured": true
  }
]
```

Pflicht sind `id`, `name` und `url`. Bei bis zu `CATALOG_PROMPT_LIMIT` Produkten sieht die KI den ganzen Katalog,
bei mehr nur die zur Anfrage passendsten (nach Name, Stichworten und Beschreibung). `featured` bevorzugt ein Produkt,
wenn nichts eindeutig passt. `LINK_QUERY` wird an jeden Link gehängt, z. B. für UTM-Parameter.

### Zahlungen (chat)

**Stripe:** Im Stripe-Dashboard einen Webhook auf `https://<deine-domain>/payments/stripe/webhook` anlegen, mit den Events
`checkout.session.completed` und `checkout.session.async_payment_succeeded`. Das Signing Secret kommt in `STRIPE_WEBHOOK_SECRET`.
Das Geld landet auf dem Stripe-Konto des `STRIPE_SECRET_KEY`.
Der Orchestrator lauscht nur auf `127.0.0.1`. Davor gehört ein Reverse Proxy mit HTTPS (z. B. Caddy), der nur `/payments/` nach außen freigibt.

**Test ohne Geld:** `PAYMENT_PROVIDER=dummy` und `PUBLIC_BASE_URL` setzen. Der Zahlungslink führt dann auf eine Testseite
mit einem Knopf, der die Zahlung sofort verbucht. Niemals produktiv verwenden.

### Mehrere Klienten auf einem Server

Jeder Klient bekommt einen eigenen Ordner mit eigenem Clone, eigener `.env`, eigenem Telegram-Account und eigener `config/`.
In jeder `.env` einen eigenen `COMPOSE_PROJECT_NAME` und `ORCHESTRATOR_PORT` setzen. Dann hat jeder Klient eigene
Container und eine eigene Datenbank, nichts wird geteilt.

### Sprachmodell

Funktioniert mit jedem OpenAI-kompatiblen Endpunkt (`LLM_BASE_URL`, `LLM_MODEL`). Die Fakten-Extraktion braucht
Structured Outputs (`json_schema`), dafür kann mit `FACT_MODEL` ein anderes Modell gewählt werden.

### Texte

Alle Systemtexte stehen als `TEXT_...` in der `.env`. **Pflicht** sind `TEXT_DISCLOSURE` (KI-Hinweis, beide Modi)
und `TEXT_PAYWALL` (Zahlungslink, nur chat). Fehlen sie, startet der Orchestrator nicht. Die Vorlagen enthalten Beispieltexte.
Alle anderen Texte sind optional, leer heißt: Standardtext des Modus aus `orchestrator/orchestrator/config.py`. Feste Regeln für jede Antwort (kurz schreiben, kein Markdown,
ehrlich sagen, dass hier eine KI schreibt) stehen in `orchestrator/orchestrator/llm.py`.

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
- **Rechtliches**: Datenschutzerklärung (Verlauf und Fakten werden gespeichert), Impressum, AGB, Widerrufsbelehrung.
  Im Shop-Modus muss erkennbar sein, dass ein Assistent des Shops schreibt (die Vorlage für `TEXT_DISCLOSURE` nennt `{shop_name}`).
  Der Zahlungsanbieter verlangt einen verifizierten Kontoinhaber.

## Bekannte Grenzen

- Was du während `/pause` von Hand schreibst, landet nicht im Verlauf. Die KI kennt diese Nachrichten nach `/resume` nicht.
- Nach `/delete confirm` gilt ein Nutzer beim nächsten Kontakt als neu und bekommt erneut das Gratis-Kontingent.
- Der Katalog kommt aus einer Datei. Eine direkte Anbindung an Shopify oder WooCommerce gibt es noch nicht.
- Telegram geht gegen automatisierte Nutzer-Accounts vor, besonders bei unaufgeforderten Nachrichten. Das System antwortet nur, es schreibt niemanden von sich aus an.
