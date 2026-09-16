"""MongoDB persistence: ingest collections and request logging."""

import logging
from datetime import datetime, timezone

from pymongo import ASCENDING, MongoClient

logger = logging.getLogger(__name__)

PRICES_COLLECTION = "prezzi"
STATIONS_COLLECTION = "stazioni"
STATUS_COLLECTION = "stato_ingest"
LOGS_COLLECTION = "richieste"


class Store:
    """Thin wrapper over the Atlas database used by the bot."""

    def __init__(self, uri, db_name, client=None):
        self._client = client or MongoClient(uri)
        self.db = self._client[db_name]

    # -- ingest ------------------------------------------------------------

    def ensure_indexes(self):
        self.db[STATIONS_COLLECTION].create_index([("location", "2dsphere")])
        self.db[STATIONS_COLLECTION].create_index([("_id", ASCENDING)])
        self.db[PRICES_COLLECTION].create_index([("id_impianto", ASCENDING)])

    def _atomic_replace(self, coll_name, docs):
        """Atomically replace a collection: write to a temp one, then rename.

        `rename` with dropTarget=True is atomic server-side and works both on
        Atlas replica sets and standalone instances (no transaction needed).
        Note: rename swaps in the temp collection's own (empty) index set, so
        indexes must be re-created afterwards.
        """
        tmp = self.db[coll_name + "_nuova"]
        tmp.delete_many({})
        if docs:
            tmp.insert_many(docs, ordered=False)
        tmp.rename(coll_name, dropTarget=True)
        self.ensure_indexes()

    def replace_stations(self, stations):
        self._atomic_replace(STATIONS_COLLECTION, stations)

    def replace_prices(self, prices_by_station):
        """prices_by_station: dict idImpianto -> list of price dicts."""
        docs = [
            {
                "id_impianto": station_id,
                "prezzi": prices,
            }
            for station_id, prices in prices_by_station.items()
        ]
        self._atomic_replace(PRICES_COLLECTION, docs)

    def record_ingest(self, extraction_date, n_stations, n_price_stations, ok, error=None):
        self.db[STATUS_COLLECTION].replace_one(
            {"_id": "last_ingest"},
            {
                "_id": "last_ingest",
                "extraction_date": extraction_date,
                "completed_at": datetime.now(timezone.utc),
                "n_stazioni": n_stations,
                "n_stazioni_con_prezzi": n_price_stations,
                "ok": ok,
                "error": error,
            },
            upsert=True,
        )

    def last_ingest(self):
        return self.db[STATUS_COLLECTION].find_one({"_id": "last_ingest"})

    # -- queries -----------------------------------------------------------

    def nearest_stations(self, lon, lat, limit=3):
        """Return up to `limit` stations nearest to (lat, lon), by distance.

        Each result: station dict + 'distance_m' + 'prezzi' list (possibly
        empty if the station hasn't communicated prices today).
        """
        pipeline = [
            {
                "$geoNear": {
                    "near": {"type": "Point", "coordinates": [lon, lat]},
                    "distanceField": "distance_m",
                    "spherical": True,
                    "query": {"location": {"$geoWithin": {"$centerSphere": [[lon, lat], 50_000 / 6_371_000]}}},
                }
            },
            {"$limit": limit},
            {
                "$lookup": {
                    "from": PRICES_COLLECTION,
                    "localField": "_id",
                    "foreignField": "id_impianto",
                    "as": "prezzi_doc",
                }
            },
            {"$addFields": {"prezzi": {"$ifNull": [{"$arrayElemAt": ["$prezzi_doc.prezzi", 0]}, []]}}},
            {"$project": {"prezzi_doc": 0}},
        ]
        stations = list(self.db[STATIONS_COLLECTION].aggregate(pipeline))
        return stations

    # -- logging -----------------------------------------------------------

    def log_request(self, telegram_user, location, n_results):
        self.db[LOGS_COLLECTION].insert_one(
            {
                "telegram_id": telegram_user.id,
                "username": telegram_user.username,
                "first_name": telegram_user.first_name,
                "requested_at": datetime.now(timezone.utc),
                "location": {"type": "Point", "coordinates": [location.longitude, location.latitude]},
                "n_risultati": n_results,
            }
        )
