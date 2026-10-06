FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY gateway ./gateway
COPY main.py login.py ./

RUN useradd --create-home gateway && mkdir -p /app/data && chown gateway /app/data
USER gateway
VOLUME ["/app/data"]

ENV HTTP_HOST=0.0.0.0
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"

CMD ["python", "main.py"]
