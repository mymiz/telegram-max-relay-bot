# Сборка из корня репозитория: docker build -f deploy/Dockerfile .
FROM python:3.11-slim

WORKDIR /app

COPY deploy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py storage.py logo.png ./

ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/data

RUN mkdir -p /data

CMD ["python", "bot.py"]
