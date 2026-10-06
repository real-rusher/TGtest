# Build-Kontext ist das Repo-Root (siehe docker-compose.yml), weil botdb mit installiert wird.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /build

COPY db_layer ./db_layer
COPY orchestrator ./orchestrator
RUN pip install ./db_layer ./orchestrator && rm -rf /build

RUN useradd --create-home app
USER app
WORKDIR /srv

EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/health').status==200 else 1)"

CMD ["python", "-m", "orchestrator"]
