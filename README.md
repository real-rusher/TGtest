# Telegram-KI-Bot

Ein fertiger Telegram-Bot mit KI, Gedächtnis und optionalem Verkauf digitaler Produkte.
Läuft mit Docker auf jedem eigenen oder gemieteten Linux-Server.

- **Antwortet wie im Messenger:** kurze Nachrichten, „tippt…“-Anzeige, wartet ab, ob noch mehr kommt
- **Merkt sich Fakten** über jede Person (Wohnort, Hobbys, …) und nutzt sie im Gespräch
- **Beliebiges Sprachmodell:** Claude, OpenAI, OpenRouter, Groq, Mistral oder ein eigenes Modell (Ollama)
- **Verkauf optional:** Telegram Stars, Stripe oder ein beliebiger Bezahllink. Die Auslieferung nach der Zahlung läuft automatisch.
- **Übernahme durch einen Menschen:** KI pro Person pausieren und von Hand weiterschreiben

---

## Wo trage ich was ein?

| Was | Wo |
|---|---|
| API-Keys, Telegram-Zugang, Passwörter | **`.env`** (die einzige Datei mit Geheimnissen) |
| Persönlichkeit, Schreibstil, KI-Hinweis, Zahlungsart, Produkte | **`config/bot.yaml`** |
| Dateien, die nach einem Kauf verschickt werden | **`config/content/`** |

Fertige Vorlagen für `config/bot.yaml`: `examples/roleplay.yaml` (Rollenspiel) und `examples/sales.yaml` (Verkauf).

---

## Schnellstart

### 1. Server besorgen

Jeder Linux-Server mit mindestens **1 GB RAM** (besser 2 GB) reicht, zum Beispiel ein kleiner
Cloud-Server bei Hetzner, Netcup, IONOS, DigitalOcean oder Contabo für wenige Euro im Monat.
Ubuntu 22.04 oder 24.04 ist eine gute Wahl.

Docker installieren (einmalig, als root):

```bash
curl -fsSL https://get.docker.com | sh
```

### 2. Projekt auf den Server kopieren

```bash
# vom eigenen Rechner aus, z. B.:
scp -r telegram-ki-bot root@SERVER-IP:/opt/
ssh root@SERVER-IP
cd /opt/telegram-ki-bot
```

### 3. Zugangsdaten besorgen

| Was | Woher |
|---|---|
| **Bot-Token** | In Telegram [@BotFather](https://t.me/BotFather) öffnen, `/newbot` senden |
| **TG_API_ID** und **TG_API_HASH** | [my.telegram.org](https://my.telegram.org) → „API development tools“ |
| **API-Key des Sprachmodells** | z. B. [console.anthropic.com](https://console.anthropic.com) oder [platform.openai.com](https://platform.openai.com) |
| **Deine Telegram-ID** (für Admin-Befehle) | In Telegram [@userinfobot](https://t.me/userinfobot) anschreiben |

### 4. Einrichten

```bash
./setup.sh          # legt .env an und erzeugt die internen Passwörter
nano .env           # LLM_API_KEY, TG_API_ID, TG_API_HASH, TG_BOT_TOKEN, ADMIN_IDS eintragen
nano config/bot.yaml   # Persona und Verhalten anpassen
```

### 5. Starten

```bash
docker compose up -d --build
docker compose logs -f        # mit Strg+C verlassen, der Bot läuft weiter
```

Jetzt den Bot in Telegram öffnen und „Start“ drücken. Fertig.

---

## Sprachmodell wählen (`.env`)

| Anbieter | Einstellungen |
|---|---|
| **Claude (Anthropic)** | `LLM_PROVIDER=anthropic`, `LLM_API_KEY=sk-ant-...`, `LLM_MODEL` leer (= `claude-opus-5-5`) oder z. B. `claude-sonnet-5-5` / `claude-haiku-4-5` |
| **OpenAI** | `LLM_PROVIDER=openai`, `LLM_API_KEY=sk-...`, `LLM_MODEL=<Modellname>` |
| **OpenRouter** (viele Modelle, ein Key) | `LLM_PROVIDER=openai`, `LLM_BASE_URL=https://openrouter.ai/api/v1`, `LLM_MODEL=<Modell laut OpenRouter>` |
| **Groq, Mistral, Together, …** | `LLM_PROVIDER=openai`, `LLM_BASE_URL=<OpenAI-kompatible URL des Anbieters>` |
| **Eigenes Modell mit Ollama** | `LLM_PROVIDER=openai`, `LLM_BASE_URL=http://host.docker.internal:11434/v1`, `LLM_MODEL=<z. B. llama3.1>`, Key leer |

Tipp: Mit `FACTS_MODEL` kann für das Gedächtnis ein günstigeres Modell desselben Anbieters genutzt werden.

---

## Telegram: Bot-Account oder normaler Account

**`TELEGRAM_MODE=bot` (empfohlen):** Ein Bot von @BotFather. Läuft sofort, unterstützt Telegram Stars.
Personen müssen den Bot zuerst selbst anschreiben, das ist eine Telegram-Regel.

**`TELEGRAM_MODE=userbot`:** Der Bot schreibt über einen normalen Telegram-Account (Telefonnummer).
Telegram Stars gehen damit nicht. Einmaliger Login vor dem ersten Start:

```bash
docker compose run --rm telegram python login.py    # fragt Telefonnummer, Code und ggf. 2FA-Passwort
docker compose up -d --build
```

Hinweis: Telegram geht gegen automatisierte normale Accounts vor, wenn sie Leute unaufgefordert
anschreiben. Der Bot antwortet nur und schreibt niemanden von sich aus an. Ein eigener
Account nur für den Bot ist empfehlenswert.

---

## Persona und Verhalten (`config/bot.yaml`)

Alle Optionen sind in der Datei selbst erklärt. Die wichtigsten:

- **`persona`:** Wer ist der Bot, wie schreibt er, was macht er nicht? In ganzen Sätzen beschreiben.
- **`start_message`:** Begrüßung bei „Start“.
- **`style`:** Wartezeit auf weitere Nachrichten, Tippgeschwindigkeit.
- **`disclosure`:** KI-Hinweis, siehe unten.
- **`memory`:** Gedächtnis an/aus.

Nach Änderungen: `docker compose restart core`

### KI-Hinweis

- `disclosure.mode: first_message`: Bei der ersten Antwort an eine Person wird der Text aus
  `disclosure.text` einmalig mitgeschickt.
- `disclosure.mode: off`: Kein automatischer Hinweis, z. B. weil er schon in der
  Bot-Beschreibung bei @BotFather steht.

Unabhängig davon gilt immer: Fragt jemand ernsthaft nach, ob er mit einer KI schreibt, streitet
der Bot das nicht ab. Er bleibt dabei in seiner Rolle.

---

## Verkaufen

In `config/bot.yaml` unter `payments.method` die Zahlungsart wählen und unter `products` die Produkte
eintragen. Die KI bietet passende Produkte im Gespräch von selbst an, dasselbe Produkt aber
höchstens alle `offer_cooldown_hours` Stunden. Nach der Zahlung werden die Inhalte aus `deliver`
automatisch geschickt: Dateien aus `config/content/` und/oder Text.

### Telegram Stars (`method: stars`)

Am einfachsten, ohne eigene Webseite. Nur mit `TELEGRAM_MODE=bot`.
Pro Produkt `price_stars` eintragen. Fertig.

### Stripe (`method: stripe`)

1. In Stripe pro Produkt einen **Payment Link** anlegen und bei `stripe_link` eintragen.
2. Öffentlichen Zugang einrichten, siehe „HTTPS“ unten.
3. In Stripe unter Entwickler → Webhooks einen Endpunkt anlegen:
   - URL: `https://DEINE-DOMAIN/webhooks/stripe`
   - Ereignisse: `checkout.session.completed` und `checkout.session.async_payment_succeeded`
4. Das „Signing Secret“ (`whsec_...`) in `.env` als `STRIPE_WEBHOOK_SECRET` eintragen.

Der Bot hängt an jeden Link automatisch eine Kennung an, über die Stripe die Zahlung der
richtigen Person zuordnet.

### Beliebiger Bezahllink (`method: link`)

Für PayPal, Digistore24, Ko-fi, Gumroad, eigene Shops usw. Pro Produkt `link` eintragen.
Im Link können `{user_id}`, `{product_id}` und `{ref}` stehen; der Bot setzt die Werte ein.

Freischalten nach der Zahlung, auf eine von zwei Arten:

**Automatisch per Webhook**, z. B. über Zapier, Make oder den Anbieter selbst:

```
POST https://DEINE-DOMAIN/webhooks/payment
Authorization: Bearer <PAYMENT_WEBHOOK_TOKEN aus .env>
Content-Type: application/json

{"ref": "u123456789-rezeptbuch", "payment_id": "eindeutige-zahlungs-id"}
```

`ref` ist genau die Kennung, die der Bot über `{ref}` in den Link eingesetzt hat. Alternativ
gehen `user_id` und `product_id` als einzelne Felder. `payment_id` verhindert, dass eine doppelt
gemeldete Zahlung zweimal ausgeliefert wird.

**Von Hand** per Admin-Befehl, siehe nächster Abschnitt.

---

## Bedienung im Betrieb

### Im Telegram-Chat (nur Admins)

| Modus | Befehl | Wirkung |
|---|---|---|
| bot | `/pause 123456789` im Chat mit dem Bot | Nachrichten dieser Person gehen nicht mehr an die KI |
| bot | `/resume 123456789` | wieder freigeben |
| bot | `/paused` | Liste anzeigen |
| userbot | `/pause` im Chat mit der Person | wie oben, der Befehl wird sofort gelöscht |
| userbot | `/resume` im Chat mit der Person | wieder freigeben |

Im Userbot-Modus kann man nach `/pause` einfach selbst im Chat weiterschreiben.

### Admin-Befehle auf dem Server

Auf dem Server direkt (ohne HTTPS) mit `http://127.0.0.1:8000`, von außen mit `https://DEINE-DOMAIN`.
Das Passwort ist `ADMIN_TOKEN` aus der `.env`.

```bash
T=$(grep ^ADMIN_TOKEN= .env | cut -d= -f2)

# Kauf manuell freischalten und ausliefern
curl -X POST http://127.0.0.1:8000/admin/grant -H "Authorization: Bearer $T" \
     -H "Content-Type: application/json" -d '{"user_id": 123456789, "product_id": "rezeptbuch"}'

# Produkt erneut schicken
curl -X POST http://127.0.0.1:8000/admin/deliver -H "Authorization: Bearer $T" \
     -H "Content-Type: application/json" -d '{"user_id": 123456789, "product_id": "rezeptbuch"}'

# KI für eine Person aus- bzw. einschalten (Gespräch wird weiter gespeichert)
curl -X POST http://127.0.0.1:8000/admin/ai -H "Authorization: Bearer $T" \
     -H "Content-Type: application/json" -d '{"user_id": 123456789, "enabled": false}'

# Von Hand eine Nachricht schicken (auch im Bot-Modus)
curl -X POST http://127.0.0.1:8000/admin/send -H "Authorization: Bearer $T" \
     -H "Content-Type: application/json" -d '{"user_id": 123456789, "text": "Hallo, hier schreibt jetzt ein Mensch."}'

# Zahlung als erstattet markieren (Stars werden automatisch zurückgebucht,
# bei Stripe/Link das Geld beim Anbieter selbst zurückbuchen)
curl -X POST http://127.0.0.1:8000/admin/refund -H "Authorization: Bearer $T" \
     -H "Content-Type: application/json" -d '{"payment_id": "..."}'
```

Die `user_id` einer Person steht in den Logs (`docker compose logs core`).

---

## HTTPS (nur für Stripe, Webhooks oder Admin von außen)

Für Telegram Stars und den reinen Chat-Betrieb ist kein öffentlicher Zugang nötig.

1. Eine Domain oder Subdomain per DNS (A-Eintrag) auf die Server-IP zeigen lassen.
2. In `.env`: `DOMAIN=bot.deine-domain.de` und `COMPOSE_PROFILES=https`
3. Ports 80 und 443 in der Firewall des Servers freigeben.
4. `docker compose up -d`

Das Zertifikat kommt automatisch von Let's Encrypt. Von außen erreichbar sind nur
`/webhooks/*`, `/admin/*` und `/health`.

---

## Wartung

```bash
docker compose ps                    # läuft alles?
docker compose logs -f core          # Logs der KI
docker compose restart core          # nach Änderungen an config/bot.yaml
docker compose up -d --build         # nach einem Update des Projekts
```

**Backup der Datenbank** (Gespräche, Gedächtnis, Käufe):

```bash
docker compose exec -T postgres pg_dump -U bot bot > backup-$(date +%F).sql
```

**Wichtig:** Das Docker-Volume `telegram_data` enthält den Telegram-Login. Nicht löschen,
also kein `docker compose down -v`. Im Userbot-Modus ist es ein vollwertiger Zugang zum
Telegram-Account und darf nie weitergegeben werden.

### Fehlersuche

| Problem | Lösung |
|---|---|
| `Konfigurationsfehler: ...` in den Logs | Die Meldung sagt genau, was in `.env` oder `config/bot.yaml` fehlt |
| Bot antwortet nicht | `docker compose logs core telegram` prüfen; API-Key und Guthaben beim KI-Anbieter prüfen |
| `Session ist nicht eingeloggt` (userbot) | `docker compose run --rm telegram python login.py` |
| Stripe-Zahlung kommt nicht an | In Stripe die Webhook-Zustellungen ansehen; `STRIPE_WEBHOOK_SECRET` und Domain prüfen |
| Datei wird nicht ausgeliefert | Liegt sie in `config/content/` und steht der Name genau so in `bot.yaml`? |

---

## Für Entwickler

```
telegram-bot/       Telegram über Bot-Account (Telethon), inkl. Stars
telegram-userbot/   Telegram über normalen Account (Telethon)
core/               Zentrale: Orchestrator, Prompt, Sprachmodelle, Zahlungen, HTTP-API
db_layer/           Datenbankschicht (PostgreSQL + Redis)
config/             bot.yaml und Inhalte zum Ausliefern
deploy/Caddyfile    HTTPS-Proxy
```

Ablauf: Telegram → `telegram` → `POST core:8000/inbound` → Datenbank + Sprachmodell →
`POST telegram:8080/send` (Chunks mit Tipp-Verzögerung) → Telegram.

Tests (Python 3.12):

```bash
pip install -r core/requirements.txt -r core/requirements-dev.txt ./db_layer telethon
cd core && pytest                       # Datenbank-Tests zusätzlich mit TEST_PG_DSN=postgresql://...
cd ../db_layer && TEST_PG_DSN=postgresql://... pytest
cd ../telegram-bot && pytest
cd ../telegram-userbot && python -m unittest discover -s tests
```
