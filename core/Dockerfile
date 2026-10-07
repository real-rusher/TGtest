# Build-Kontext ist das Projektverzeichnis (siehe docker-compose.yml),
# damit die Datenbankschicht db_layer mit installiert werden kann.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY core/requirements.txt core-requirements.txt
RUN pip install -r core-requirements.txt

COPY db_layer /tmp/db_layer
RUN pip install /tmp/db_layer && rm -rf /tmp/db_layer

COPY core/botcore ./botcore
# Lesbar für den Nicht-root-Benutzer, auch wenn die Dateien mit engen Rechten ankommen
RUN chmod -R a+rX /app

RUN useradd --create-home bot
USER bot

ENV CONFIG_FILE=/app/config/bot.yaml CONTENT_DIR=/app/config/content
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["python", "-m", "botcore"]
