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


def deduplicate_stations(stations, prices_by_station):
    """Resolve groups of stations sharing the exact same coordinate.

    A few hundred registry entries share a point (motorway service areas,
    duplicated registry rows, same street number). For each coordinate group:
    - stations without any communicated price are dropped (likely closed or
      non-reporting duplicates);
    - if the survivors have *identical* price lists, they are merged into one
      entry (closest thing to a unique real station);
    - otherwise all survivors are kept: same point but genuinely different
      operators/pumps is plausible (the user will see each with its prices).

    Returns (stations, prices) with merged ids removed from prices too.
    """
    by_coord = {}
    for s in stations:
        by_coord.setdefault((s["lat"], s["lon"]), []).append(s)

    drop_ids = set()
    for coord, group in by_coord.items():
        if len(group) < 2:
            continue
        with_prices = [s for s in group if prices_by_station.get(s["_id"])]
        if len(with_prices) < 2:
            # keep the only one with prices, or if none has prices keep the
            # group untouched (the bot already skips price-less stations)
            if len(with_prices) == 1:
                drop_ids.update(s["_id"] for s in group if s["_id"] != with_prices[0]["_id"])
            continue
        def _sig(s):
            return sorted(
                (p["carburante"], p["prezzo"], p.get("is_self", False))
                for p in prices_by_station[s["_id"]]
            )
        sigs = {}
        for s in with_prices:
            sigs.setdefault(tuple(_sig(s)), []).append(s)
        for sig, same in sigs.items():
            if len(same) > 1:
                # identical price lists at the identical point: keep the first
                keeper = sorted(same, key=lambda s: s["_id"])[0]
                drop_ids.update(s["_id"] for s in same if s["_id"] != keeper["_id"])
    if not drop_ids:
        return stations, prices_by_station
    kept = [s for s in stations if s["_id"] not in drop_ids]
    kept_prices = {k: v for k, v in prices_by_station.items() if k not in drop_ids}
    logger.info(
        "Dedup: dropped %d duplicate stations sharing identical coordinates/prices",
        len(drop_ids),
    )
    return kept, kept_prices


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
    stations, prices = deduplicate_stations(stations, prices)
    store.replace_stations(stations)
    store.replace_prices(prices)
    store.record_ingest(extraction_date, len(stations), len(prices), ok=True)
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
