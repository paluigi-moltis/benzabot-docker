import sys
from datetime import datetime

import pytest

sys.path.insert(0, ".")

from benzabot import formatting  # noqa: E402

STATION = {
    "_id": 59183,
    "gestore": "ENIMOOV S.P.A.",
    "bandiera": "Agip Eni",
    "indirizzo": "SS.189 KM. 64+649",
    "comune": "AGRIGENTO",
    "provincia": "AG",
    "lat": 37.333935,
    "lon": 13.595533,
    "distance_m": 1234,
    "prezzi": [
        {"carburante": "Gasolio", "prezzo": 1.799, "is_self": True, "dt_comu": datetime(2026, 9, 11, 8, 15)},
        {"carburante": "Benzina", "prezzo": 1.899, "is_self": True, "dt_comu": datetime(2026, 9, 11, 21, 0, 9)},
        {"carburante": "Benzina", "prezzo": 2.099, "is_self": False, "dt_comu": datetime(2026, 9, 11, 21, 0, 9)},
    ],
}


def test_station_message_contains_all_prices():
    msg = formatting.format_station_message(STATION)
    assert "<b>Agip Eni</b>" in msg
    assert "1.2 km da te" in msg
    assert "Benzina</b> (self): 1.899 €/L" in msg
    assert "Benzina</b> (servito): 2.099 €/L" in msg
    assert "Gasolio</b> (self): 1.799 €/L" in msg
    # dtComu shown = max across fuels
    assert "Prezzi comunicati il 11 settembre 2026, 21:00" in msg
    # benzina (self) before servito
    assert msg.index("self): 1.899") < msg.index("servito): 2.099")


def test_station_without_prices():
    st = dict(STATION, prezzi=[])
    msg = formatting.format_station_message(st)
    assert "Nessun prezzo disponibile" in msg


def test_navigation_keyboard_urls():
    kb = formatting.navigation_keyboard(STATION)
    urls = [btn.url for row in kb.keyboard for btn in row]
    assert any("google.com/maps/dir" in u and "destination=37.333935,13.595533" in u for u in urls)
    assert any("maps.apple.com" in u for u in urls)
    assert any("openstreetmap.org" in u for u in urls)


def test_info_message_with_status():
    status = {
        "extraction_date": datetime(2026, 9, 14),
        "n_stazioni": 21000,
        "n_stazioni_con_prezzi": 20500,
        "completed_at": datetime(2026, 9, 15, 6, 31),
        "ok": True,
    }
    msg = formatting.format_info_message(status, "https://github.com/paluigi/benzabot-docker")
    assert "/info" in msg and "/github" in msg
    assert "14/09/2026" in msg
    assert "✅" in msg
    assert "21000" in msg


def test_info_message_failed_ingest():
    msg = formatting.format_info_message({"ok": False}, "https://x")
    assert "⚠️" in msg
