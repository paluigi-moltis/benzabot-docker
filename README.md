# benzabot-docker

Dockerized Telegram bot that tells you which gas stations are closest to you and what prices they charge, using the official [MIMIT open data](https://www.mimit.gov.it/it/open-data/elenco-dataset/carburanti-prezzi-praticati-e-anagrafica-degli-impianti) ("Carburanti — Prezzi praticati e anagrafica degli impianti", IODL 2.0 license).

All messages to users are in Italian.

## What it does

- **Daily ingest** (~08:30 Europe/Rome, with random jitter, configurable via APScheduler): downloads the `anagrafica_impianti_attivi.csv` and `prezzo_alle_8.csv` files from MIMIT, validates and parses them, and atomically replaces two MongoDB collections:
  - `stazioni` — station registry (name, brand, address, GeoJSON location with a 2dsphere geospatial index)
  - `prezzi` — fuel prices per station, including the `dtComu` communication timestamp
  - On download or parse failure it retries with exponential backoff and **keeps the previous day's data**, flagging the status in the `stato_ingest` collection (visible via the bot's `/info` command).
- **Telegram bot** (long polling):
  - Send your GPS location → get the **3 nearest stations** as separate messages, ordered by distance, each with the full price list (self/servito), the address, the distance, and the "prezzi comunicati il …" date (from `dtComu`).
  - Each message has inline buttons to open the route in **Google Maps**, **Apple Maps**, or **OpenStreetMap**.
  - `/info` — how the bot works + current data status (last extraction date, station counts)
  - `/github` — link to this repository
  - Any non-location message (text, photo, …) gets a polite hint to send the GPS position instead.
- **Request logging**: every location query is stored in the `richieste` collection (Telegram user id/username, timestamp, coordinates, number of results).

## Setup

### 1. Telegram bot token

Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.

### 2. MongoDB Atlas

Create a free [Atlas](https://www.mongodb.com/atlas) cluster, a database user, and allow network access from your host. Collections and indexes are created automatically on first run.

### 3. Environment

```bash
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN and MONGODB_URI are required
```

### 4. Run

```bash
docker compose up -d --build
```

This starts two containers from the same image:

- `bot` — the Telegram bot (long polling)
- `ingest` — the APScheduler cron that refreshes MongoDB once a day

## Configuration (env vars)

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | BotFather token (bot container) |
| `MONGODB_URI` | yes | — | Atlas connection string |
| `MONGODB_DB` | no | `benzabot` | Database name |
| `GITHUB_REPO_URL` | no | repo URL | Link returned by `/github` |
| `LOG_LEVEL` | no | `INFO` | Python log level |
| `INGEST_TIME` | no | `08:30` | Daily ingest time (Europe/Rome) |
| `INGEST_JITTER_SECONDS` | no | `900` | Random jitter window for the ingest |
| `TZ` | no | `Europe/Rome` | Container timezone |

## Data quality notes

- Coordinates come from the station managers and are generally good, but the source contains a small number of broken entries (swapped or zero lat/lon, and ~400/24k stations sharing lazy-geocoding placeholder coordinates such as Milan Duomo or Rome Colosseum). These are filtered out at ingest time.
- Since 2026-02-10 MIMIT uses `|` as CSV field separator (previously `;`) — the parser auto-detects it.
- Published data reflects prices "in vigore alle 8 del giorno precedente"; each price row carries its own `dtComu` timestamp.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest
```

Tests cover CSV parsing (both separators, malformed rows, coordinate validation), message formatting, ingest retry/failure behaviour, and MongoDB persistence (via in-memory fakes).

## License

See [LICENSE](LICENSE).
