FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Rome

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY benzabot/ ./benzabot/

# Two containers from the same image:
#   * `bot`    — Telegram long polling
#   * `ingest` — APScheduler daily download of MIMIT data
# (override via `command:` in docker-compose.yml)
CMD ["python", "-m", "benzabot.bot"]
