# Telegram I/O Gateway (Modul 1)

> **Teil des Gesamtpakets.** Dieses Modul wird über die `docker-compose.yml` im Hauptordner
> gestartet und dort konfiguriert (eine gemeinsame `.env`). Anleitung: siehe `README.md` im Hauptordner.
> Neu gegenüber der Einzelversion: `POST /send_file` (Datei aus `CONTENT_DIR` senden).

Userbot auf Basis von Telethon. Wandelt private Textnachrichten in Events um, führt Sendeaufträge mit Tipp-Status und Verzögerung aus und kennt `/pause` und `/resume`. Keine KI, keine Datenbank, keine Geschäftslogik.

## Schnittstellen

### Eingang: eingehende Privatnachricht

Jede private Textnachricht (inklusive Links) geht als `POST` an `INBOUND_WEBHOOK_URL`:

```json
{
  "user_id": 123456789,
  "username": "max",
  "first_name": "Max",
  "message_text": "hey was machst du",
  "timestamp": 1791302400
}
```

Ist `INBOUND_WEBHOOK_TOKEN` gesetzt, kommt ein `Authorization: Bearer <token>` Header mit. Bei Fehlern wird bis zu 4 Mal mit Backoff wiederholt, Events pro User bleiben in Reihenfolge.

Nicht weitergeleitet werden: pausierte Chats, Bots, gelöschte Accounts, der Telegram-Service (777000, Login-Codes), Medien ohne Text.

### Ausgang: Sendeauftrag

```
POST /send
Authorization: Bearer <API_TOKEN>

{"user_id": 123456789,
 "chunks": ["hahaha okay", "was hast du danach gemacht?"],
 "delays": [1.5, 2.5]}
```

| Antwort | Bedeutung |
|---|---|
| `202 queued` | angenommen, läuft im Hintergrund |
| `200 sent` | nur mit `?wait=1`: alles zugestellt, `sent` = Anzahl Chunks |
| `400` | ungültiger Auftrag (Längen ungleich, Delay negativ oder > `MAX_DELAY`, Chunk leer oder > 4096 Zeichen) |
| `404` | nur mit `?wait=1`: User dem Account unbekannt |
| `409` | Chat pausiert, oder mit `?wait=1` durch `/pause` abgebrochen |

Ablauf pro Paar: Chat als gelesen markieren (einmal am Anfang), Status "tippt..." setzen, exakt `delay` Sekunden warten (Status wird alle 4 s erneuert, damit er bei langen Delays nicht ausläuft), Chunk senden. Text geht ohne Markdown-Parsing raus, Links bleiben also unverändert. Mehrere Aufträge für denselben User laufen nacheinander, nie verschachtelt.

Außerdem: `GET /paused` (Token nötig) und `GET /health` (ohne Token).

### Admin-Steuerung

Schreib im Chat mit der Person `/pause` oder `/resume` von deinem Account aus.

- Der Befehl wird sofort für beide Seiten gelöscht, die Person sieht ihn nicht.
- `/pause` trägt den Chat in die Sperrliste ein, eingehende Nachrichten lösen keine Events mehr aus, und laufende Sendeaufträge für diesen Chat werden abgebrochen (Tipp-Status wird beendet). Neue Aufträge bekommen `409`, solange `PAUSE_BLOCKS_OUTBOUND=true`.
- `/resume` entfernt den Chat wieder.
- Die Sperrliste liegt zusätzlich in `data/paused.json` und überlebt Neustarts.

## Einrichtung auf dem Server

Über das Gesamtpaket im Hauptordner (siehe dortige `README.md`), mit `TELEGRAM_MODE=userbot` in der `.env`:

```
docker compose run --rm telegram python login.py   # einmalig: Telefonnummer, Code, ggf. 2FA
docker compose up -d --build
```

Der Service verbindet sich bei Netzproblemen selbst neu. Bricht die Verbindung endgültig ab, beendet er sich mit Code 1 und Docker startet ihn neu.

## Wichtig

- Die Session-Datei im Volume `telegram_data` ist ein vollwertiger Login in deinen Telegram-Account. Nie weitergeben.
- Der HTTP-Port ist nur im internen Docker-Netz erreichbar und wird nicht nach außen freigegeben.
- Telegram geht gegen automatisierte Nutzer-Accounts vor, besonders wenn Leute unaufgefordert angeschrieben werden. Das Gateway antwortet nur, schreibt aber niemanden von sich aus an, das sollte das System dahinter genauso halten.

## Tests

```
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Die Tests laufen mit einem simulierten Telegram-Client und prüfen Reihenfolge, Delays, Tipp-Status, Pause mitten im Senden, Filter und die HTTP-Schnittstelle.
