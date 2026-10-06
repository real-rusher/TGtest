# Builder 1: Telegram-Adapter

Isolierter Telegram-Client (Telethon, Bot-Account, nur private Chats). Nimmt Nachrichten
entgegen, leitet sie an den Core weiter, sendet Antworten mit Tippen-Status und
Verzögerung pro Chunk und verarbeitet Telegram-Stars-Zahlungen.

## Warum Telethon mit Bot-Token

Telegram Stars (`pre_checkout_query`, `successful_payment`) gibt es nur für Bots, nicht
für normale User-Accounts. Telethon spricht direkt MTProto und hat alle Stars-Typen;
das Original-Pyrogram wird nicht mehr gepflegt. `ChatAction.TYPING` aus der Spezifikation
entspricht hier `SendMessageTypingAction`.

## Schnittstellen

**Inbound** (Adapter -> Core), `POST INBOUND_URL`:
```json
{ "user_id": 42, "text": "Hallo", "timestamp": 1791288000 }
```

**Zahlung** (Adapter -> Core), `POST PAYMENT_URL`:
```json
{ "event": "successful_payment", "user_id": 42, "currency": "XTR", "total_amount": 50,
  "payload": "pkg_basic", "charge_id": "...", "provider_charge_id": "", "timestamp": 1791288000 }
```
Jede `charge_id` wird nur einmal gemeldet (gespeichert in `data/state.json`).
`charge_id` aufheben, man braucht sie für Erstattungen.

**Outbound** (Core -> Adapter), Header `Authorization: Bearer <OUTBOUND_TOKEN>`:

| Methode | Pfad | Body | Antwort |
|---|---|---|---|
| POST | `/send` | `{"user_id", "text_chunks": [..], "delays": [..]}` | 202 sofort, Versand im Hintergrund |
| POST | `/send?wait=1` | wie oben | 200 erst nach dem letzten Chunk |
| POST | `/invoice` | `{"user_id", "title", "description", "payload", "amount"}` | Stars-Rechnung senden |
| POST | `/refund` | `{"user_id", "charge_id"}` | Stars erstatten |
| GET | `/health` | | 200 wenn verbunden |

Fehler: 400 ungültige Daten, 401 Token falsch, 404 User unbekannt, 502 Telegram-Fehler.

## Ablauf von `send_message_chunks`

Für jeden Chunk: Tippen-Status setzen, Delay abwarten (Status wird alle 4 s erneuert, weil
Telegram ihn sonst nach etwa 5 s ausblendet), dann senden. Details:

* Delays über `MAX_DELAY` (Standard 30 s) werden gekappt.
* Chunks über 4096 Zeichen werden an Zeilenumbrüchen oder Leerzeichen geteilt.
* Mehrere Aufträge für denselben User laufen nacheinander, nie durcheinander.
* `FloodWaitError` wird abgewartet und bis zu 3 mal wiederholt.

## Admin-Befehle

Nur für IDs in `ADMIN_IDS`, im privaten Chat mit dem Bot:

* `/pause <user_id>`: Nachrichten dieses Users werden nicht mehr weitergeleitet
* `/resume <user_id>`: wieder freigeben
* `/paused`: Liste anzeigen

Die Pause-Liste übersteht Neustarts. Zahlungen pausierter User werden trotzdem gemeldet.
Wenn ein Nicht-Admin `/pause` schreibt, geht das als normaler Text an den Core.

## Wichtige Einschränkung

Ein Bot kann nur Usern schreiben, die ihm vorher selbst geschrieben haben (oder `/start`
gedrückt haben). Sonst kommt 404 zurück.

## Deployment (Server oder Cloud)

```bash
cp .env.example .env        # Werte eintragen
docker build -t tg-adapter .
docker run -d --name tg-adapter --env-file .env -v tg_data:/app/data -p 8080:8080 tg-adapter
```
Ohne Docker: `pip install -r requirements.txt` und `python -m tg_adapter`.
Das Volume `data/` enthält die Telethon-Session und die Pause-Liste und darf nicht verloren gehen.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```
29 Tests mit Fake-Client, ohne echte Telegram-Verbindung.
