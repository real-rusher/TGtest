FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY tg_adapter ./tg_adapter
# Lesbar für den Nicht-root-Benutzer, auch wenn die Dateien mit engen Rechten ankommen
RUN chmod -R a+rX /app

# Gleiche uid wie telegram-userbot, damit beide dasselbe Daten-Volume nutzen können
RUN useradd --create-home --uid 1000 bot && mkdir -p /app/data /app/content && chown bot /app/data
USER bot
# Session-Datei und Pause-Liste liegen hier, als Volume mounten
VOLUME ["/app/data"]

ENV CONTENT_DIR=/app/content
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"

CMD ["python", "-m", "tg_adapter"]
