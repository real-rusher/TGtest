FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tg_adapter ./tg_adapter
# Session-Datei und Pause-Liste liegen hier, als Volume mounten
VOLUME /app/data
EXPOSE 8080
CMD ["python", "-m", "tg_adapter"]
