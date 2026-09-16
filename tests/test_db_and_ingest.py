import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, ".")

from benzabot import ingest  # noqa: E402
from benzabot.db import Store  # noqa: E402


class FakeCollection:
    def __init__(self, name):
        self.name = name
        self.docs = []

    def delete_many(self, q):
        self.docs = []

    def insert_many(self, docs, ordered=True):
        self.docs.extend(docs)
        return SimpleNamespace(inserted_ids=[None] * len(docs))

    def rename(self, new_name, dropTarget=False):
        return SimpleNamespace(ok=1)

    def find(self, q):
        return iter(self.docs)

    def find_one(self, q):
        return self.docs[0] if self.docs else None

    def replace_one(self, q, doc, upsert=False):
        self.docs = [doc]

    def insert_one(self, doc):
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=None)

    def create_index(self, *a, **kw):
        pass

    def aggregate(self, pipeline):
        return []


class FakeDb:
    def __init__(self):
        self.colls = {}

    def __getitem__(self, name):
        return self.colls.setdefault(name, FakeCollection(name))


class FakeClient:
    """Minimal MongoClient stand-in: client[db][coll] -> FakeCollection."""

    def __init__(self):
        self.dbs = {}

    def __getitem__(self, db_name):
        return self.dbs.setdefault(db_name, FakeDb())


@pytest.fixture
def store():
    s = Store(None, "test", client=FakeClient())
    return s


def _db(store):
    return store.db


def test_replace_stations_atomic(store, monkeypatch):
    fake_db = FakeDb()
    monkeypatch.setattr(store, "db", fake_db)
    stations = [{"_id": 1, "bandiera": "X"}]
    store.replace_stations(stations)
    assert fake_db["stazioni_nuova"].docs == stations


def test_replace_prices_groups_by_station(store, monkeypatch):
    fake_db = FakeDb()
    monkeypatch.setattr(store, "db", fake_db)
    store.replace_prices({1: [{"carburante": "Benzina", "prezzo": 1.9}], 2: []})
    docs = fake_db["prezzi_nuova"].docs
    assert {d["id_impianto"] for d in docs} == {1, 2}


def test_log_request(store, monkeypatch):
    fake_db = FakeDb()
    monkeypatch.setattr(store, "db", fake_db)
    user = SimpleNamespace(id=42, username="luigi", first_name="Luigi")
    loc = SimpleNamespace(latitude=45.0, longitude=9.0)
    store.log_request(user, loc, 3)
    doc = fake_db["richieste"].docs[0]
    assert doc["telegram_id"] == 42
    assert doc["location"]["coordinates"] == [9.0, 45.0]
    assert doc["n_risultati"] == 3


def test_ingest_success_records_status(store, monkeypatch):
    fake_db = FakeDb()
    monkeypatch.setattr(store, "db", fake_db)
    stations = [{"_id": 1, "lat": 45.0, "lon": 9.0}]
    prices = {1: [{"carburante": "Benzina", "prezzo": 1.9}]}
    with patch("benzabot.mimit.fetch_dataset", return_value=(stations, prices, None)):
        assert ingest.run_ingest(store) is True
    status = fake_db["stato_ingest"].docs[0]
    assert status["ok"] is True
    assert status["n_stazioni"] == 1


def test_ingest_retries_then_succeeds(store, monkeypatch):
    fake_db = FakeDb()
    monkeypatch.setattr(store, "db", fake_db)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return [{"_id": 1, "lat": 45.0, "lon": 9.0}], {1: []}, None

    monkeypatch.setattr(time, "sleep", lambda s: None)
    with patch("benzabot.mimit.fetch_dataset", side_effect=flaky):
        assert ingest.run_ingest(store, max_attempts=5) is True
    assert calls["n"] == 3


def test_ingest_failure_keeps_previous_data_and_flags_status(store, monkeypatch):
    fake_db = FakeDb()
    monkeypatch.setattr(store, "db", fake_db)
    # seed "previous" data
    fake_db["stazioni_nuova"].docs = [{"_id": 999}]
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with patch("benzabot.mimit.fetch_dataset", side_effect=RuntimeError("down")):
        assert ingest.run_ingest(store, max_attempts=2) is False
    status = fake_db["stato_ingest"].docs[0]
    assert status["ok"] is False
    assert "down" in status["error"]
    # previous data untouched (no rename happened)
    assert fake_db["stazioni_nuova"].docs == [{"_id": 999}]


def test_parse_hhmm():
    assert ingest.parse_hhmm("08:30") == (8, 30)


# --- dedup tests -------------------------------------------------------

def _st(i, lat, lon):
    return {"_id": i, "lat": lat, "lon": lon, "bandiera": "X"}


def test_dedup_drops_priceless_duplicates():
    stations = [_st(1, 45.0, 9.0), _st(2, 45.0, 9.0), _st(3, 46.0, 9.5)]
    prices = {1: [{"carburante": "Benzina", "prezzo": 1.9}]}
    kept, kept_prices = ingest.deduplicate_stations(stations, prices)
    assert [s["_id"] for s in kept] == [1, 3]
    assert set(kept_prices) == {1}


def test_dedup_merges_identical_prices():
    p = [{"carburante": "Benzina", "prezzo": 1.9}, {"carburante": "Gasolio", "prezzo": 1.8}]
    stations = [_st(1, 45.0, 9.0), _st(2, 45.0, 9.0), _st(3, 46.0, 9.5)]
    prices = {1: p, 2: p, 3: [{"carburante": "GPL", "prezzo": 0.8}]}
    kept, kept_prices = ingest.deduplicate_stations(stations, prices)
    assert sorted(s["_id"] for s in kept) == [1, 3]  # 2 merged into 1
    assert set(kept_prices) == {1, 3}


def test_dedup_keeps_different_prices_same_coords():
    p1 = [{"carburante": "Benzina", "prezzo": 1.9}]
    p2 = [{"carburante": "Benzina", "prezzo": 1.7}]
    stations = [_st(1, 45.0, 9.0), _st(2, 45.0, 9.0)]
    prices = {1: p1, 2: p2}
    kept, kept_prices = ingest.deduplicate_stations(stations, prices)
    assert sorted(s["_id"] for s in kept) == [1, 2]
    assert set(kept_prices) == {1, 2}


def test_dedup_ignores_unique_coords():
    stations = [_st(1, 45.0, 9.0), _st(2, 46.0, 9.5)]
    prices = {2: [{"carburante": "Benzina", "prezzo": 1.9}]}
    kept, kept_prices = ingest.deduplicate_stations(stations, prices)
    assert len(kept) == 2
    assert set(kept_prices) == {2}
