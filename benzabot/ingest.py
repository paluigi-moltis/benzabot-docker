"""Daily ingest of MIMIT data into MongoDB, scheduled with APScheduler."""

import logging
import os
import random
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from benzabot import mimit
from benzabot.db import Store

logger = logging.getLogger(__name__)

ROME_TZ = ZoneInfo("Europe/Rome")
DEFAULT_INGEST_TIME = "08:30"


def run_ingest(store: Store, max_attempts: int = 4):
    """One ingest run: download, parse, atomically replace collections."""
    started = datetime.now(timezone.utc)
    logger.info("Ingest started at %s", started.isoformat())
    stations = prices = None
    extraction_date = None
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            stations, prices, extraction_date = mimit.fetch_dataset()
            if not stations:
                raise ValueError("Empty station dataset from MIMIT")
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001 - keep previous data on failure
            last_error = exc
            logger.warning("Ingest attempt %d/%d failed: %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                backoff = 60 * 2 ** (attempt - 1) + random.uniform(0, 15)
                time.sleep(backoff)
    if last_error is not None:
        logger.error("Ingest failed after %d attempts: %s", max_attempts, last_error)
        store.record_ingest(extraction_date, 0, 0, ok=False, error=str(last_error))
        return False
    n_prices = len(prices)
    store.replace_stations(stations)
    store.replace_prices(prices)
    store.record_ingest(extraction_date, len(stations), n_prices, ok=True)
    logger.info(
        "Ingest OK: %d stations, %d stations with prices, estrazione %s (%.1fs)",
        len(stations), n_prices, extraction_date,
        (datetime.now(timezone.utc) - started).total_seconds(),
    )
    return True


def parse_hhmm(value: str):
    hour, minute = value.split(":")
    return int(hour), int(minute)


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mongo_uri = os.environ["MONGODB_URI"]
    db_name = os.getenv("MONGODB_DB", "benzabot")
    tz = ZoneInfo(os.getenv("TZ", "Europe/Rome"))
    hour, minute = parse_hhmm(os.getenv("INGEST_TIME", DEFAULT_INGEST_TIME))
    jitter = int(os.getenv("INGEST_JITTER_SECONDS", "900"))

    store = Store(mongo_uri, db_name)
    store.ensure_indexes()

    scheduler = BlockingScheduler(timezone=str(tz))
    scheduler.add_job(
        run_ingest,
        trigger="cron",
        args=[store],
        hour=hour,
        minute=minute,
        jitter=jitter,
        id="mimit_ingest",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
