import sys
import csv
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

import pytest

sys.path.insert(0, ".")

from benzabot import mimit  # noqa: E402

STATIONS_CSV = """Estrazione del 2026-09-14
idImpianto|Gestore|Bandiera|Tipo Impianto|Nome Impianto|Indirizzo|Comune|Provincia|Latitudine|Longitudine
59183|ENIMOOV S.P.A.|Agip Eni|Stradale|19829 AGRIGENTO|SS.189 KM. 64+649|AGRIGENTO|AG|37.333935|13.595533
60001|Q8|Q8|Stradale|Q8 MILANO|VIA TEST 1|MILANO|MI|45.4642|9.1900
60002|X|X|Stradale|ROTTA|VIA ROTTA 2|ROMA|RM|13.595533|37.333935
60003|Y|Y|Stradale|ZERO|VIA ZERO 3|ROMA|RM|0|0
60004|Z|Z|Stradale|FUORI|VIA FUORI 4|ROMA|RM|55.0|9.0
60005|W|W|Stradale|DUOMO|VIA DUOMO 5|MILANO|MI|45.4642035|9.189982
"""

PRICES_CSV = """Estrazione del 2026-09-14
idImpianto|descCarburante|prezzo|isSelf|dtComu
59183|Benzina|1.899|1|11/09/2026 21:00:09
59183|Benzina|2.099|0|11/09/2026 21:00:09
59183|Gasolio|1.799|1|10/09/2026 08:15:00
60001|GPL|0.799|0|11/09/2026 07:30:00
60001|Metano|0|0|11/09/2026 07:30:00
BADROW|Benzina|1.5|1|11/09/2026 07:30:00
60001|Gasolio|,abc|1|11/09/2026 07:30:00
"""

LEGACY_CSV = (
    "Estrazione del 2026-01-05\n"
    "idImpianto;descCarburante;prezzo;isSelf;dtComu\n"
    "59183;Benzina;1.799;1;04/01/2026 20:00:00\n"
)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (STATIONS_CSV if "anagrafica" in self.path else PRICES_CSV).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def fake_server():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    import benzabot.mimit as mm

    old_base = mm.BASE_URL
    mm.BASE_URL = f"http://127.0.0.1:{server.server_port}"
    yield mm
    mm.BASE_URL = old_base
    server.shutdown()


class StubLookup:
    """Province lookup stub: valid if a point falls inside its bbox entry."""

    SYNONYMS = {}

    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    def find(self, lon, lat):
        for sigla, (x0, y0, x1, y1) in self.mapping.items():
            if x0 <= lon <= x1 and y0 <= lat <= y1:
                return [sigla]
        return []

    def matches(self, declared, lon, lat):
        if not declared:
            return bool(self.find(lon, lat))
        if declared in self.find(lon, lat):
            return True
        return any(c in self.find(lon, lat) for c in self.SYNONYMS.get(declared, ()))


def test_parse_stations_filters_invalid_coords():
    mimit.set_province_lookup(None)
    # disable province validation for the pure-parsing test
    stations, sep = mimit.parse_stations(STATIONS_CSV, validate_provinces=False)
    assert sep == "|"
    ids = [s["_id"] for s in stations]
    # 60002 swapped lat/lon, 60003 zero coords, 60004 out of bbox,
    # 60005 geocoding placeholder (Milan Duomo) -> all filtered
    assert ids == [59183, 60001]
    s = stations[0]
    assert s["location"]["coordinates"] == [13.595533, 37.333935]
    assert s["bandiera"] == "Agip Eni"
    assert s["comune"] == "AGRIGENTO"


def test_parse_prices():
    prices, extraction = mimit.parse_prices(PRICES_CSV)
    assert extraction == datetime(2026, 9, 14)
    assert set(prices.keys()) == {59183, 60001}  # BADROW dropped, zero price dropped
    benz = prices[59183]
    assert len(benz) == 3
    self_benz = [p for p in benz if p["is_self"]]
    assert self_benz[0]["prezzo"] == 1.899
    assert self_benz[0]["dt_comu"] == datetime(2026, 9, 11, 21, 0, 9)
    # unparseable price row dropped
    assert all(p["carburante"] != "Gasolio" or p["prezzo"] > 0 for p in prices[60001])


def test_legacy_semicolon_separator():
    prices, extraction = mimit.parse_prices(LEGACY_CSV)
    assert extraction == datetime(2026, 1, 5)
    assert prices[59183][0]["prezzo"] == 1.799


def test_fetch_dataset_against_http(fake_server):
    mimit.set_province_lookup(None)
    stations, prices, extraction = fake_server.fetch_dataset()
    assert extraction == datetime(2026, 9, 14)
    assert len(stations) == 2
    assert 59183 in prices


def test_parse_stations_province_validation():
    # AG (Agrigento) bbox contains 13.59/37.33; MI bbox contains 9.19/45.46;
    # station 60001 declares MI but sits in AG's bbox -> dropped.
    stub = StubLookup({"AG": (13.0, 36.5, 14.5, 38.0), "MI": (8.5, 45.0, 9.8, 46.0)})
    mimit.set_province_lookup(stub)
    try:
        stations, _ = mimit.parse_stations(STATIONS_CSV)
        ids = [s["_id"] for s in stations]
        # 60001 (MI declared, coords in MI bbox) kept, 59183 (AG) kept
        assert ids == [59183, 60001]
    finally:
        mimit.set_province_lookup(None)


def test_parse_stations_in_sea_dropped():
    # declare the province lookup that knows nothing -> both points "in the sea"
    class SeaLookup:
        SYNONYMS = {}

        def find(self, lon, lat):
            return []

        def matches(self, declared, lon, lat):
            return False
    mimit.set_province_lookup(SeaLookup())
    try:
        stations, _ = mimit.parse_stations(STATIONS_CSV)
        assert stations == []
    finally:
        mimit.set_province_lookup(None)
