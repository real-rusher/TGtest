FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY gateway ./gateway
COPY main.py login.py ./
# Lesbar für den Nicht-root-Benutzer, auch wenn die Dateien mit engen Rechten ankommen
RUN chmod -R a+rX /app

RUN useradd --create-home --uid 1000 gateway && mkdir -p /app/data /app/content && chown gateway /app/data
USER gateway
VOLUME ["/app/data"]

ENV HTTP_HOST=0.0.0.0 CONTENT_DIR=/app/content
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"

CMD ["python", "main.py"]
