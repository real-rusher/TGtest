#!/usr/bin/env sh
# Einrichtung: legt die .env an und füllt die internen Passwörter automatisch.
# Mehrfaches Ausführen ist unbedenklich, vorhandene Werte bleiben erhalten.
set -eu
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo ".env aus .env.example angelegt."
fi

random() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

fill() {
  # Setzt KEY=<zufall>, wenn KEY in der .env leer ist.
  if grep -q "^$1=$" .env; then
    value=$(random)
    sed "s|^$1=$|$1=$value|" .env > .env.tmp && mv .env.tmp .env
    echo "$1 erzeugt."
  fi
}

fill INTERNAL_TOKEN
fill ADMIN_TOKEN
fill POSTGRES_PASSWORD
fill PAYMENT_WEBHOOK_TOKEN
chmod 600 .env

missing=""
for key in LLM_API_KEY TG_API_ID TG_API_HASH; do
  grep -q "^$key=$" .env && missing="$missing $key"
done
if grep -q "^TELEGRAM_MODE=bot" .env && grep -q "^TG_BOT_TOKEN=$" .env; then
  missing="$missing TG_BOT_TOKEN"
fi

echo
if [ -n "$missing" ]; then
  echo "Noch in .env eintragen:$missing"
  echo "Danach: docker compose up -d --build"
else
  echo "Fertig. Starten mit: docker compose up -d --build"
fi
