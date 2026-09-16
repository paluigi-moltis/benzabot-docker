"""Download and parse MIMIT open-data fuel files (prezzi + anagrafica impianti)."""

import csv
import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.mimit.gov.it/images/exportCSV"
PRICES_FILE = "prezzo_alle_8.csv"
STATIONS_FILE = "anagrafica_impianti_attivi.csv"

# Since 2026-02-10 the separator is '|'; before it was ';'. We detect it.
BANNER_PREFIX = "Estrazione del "

# Italy-ish bounding box used to discard stations with broken coordinates
# (the open data is known to contain swapped/zero lat-lon pairs).
IT_BBOX = {"lat_min": 35.0, "lat_max": 47.5, "lon_min": 6.0, "lon_max": 19.0}

# Coordinates that several unrelated stations share in the MIMIT data: these
# are lazy-geocoding placeholders (famous landmarks), not real positions.
# Note: this only affects ~400 out of ~24k stations — coordinates are
# otherwise good quality, as they come from the station managers themselves.
PLACEHOLDER_COORDS = [
    (45.4642035, 9.189982),  # Milan Duomo
    (41.8904, 12.5126),  # Rome Colosseum
    (41.89041, 12.5126),
    (41.8947, 12.49348),  # Rome centre
]


def _parse_float(value):
    """Parse a float that may use ',' as decimal separator. None if invalid."""
    if value is None:
        return None
    value = value.strip().replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


def _split_row(line, sep):
    return next(csv.reader([line], delimiter=sep))


def _detect_separator(sample_lines):
    for sep in ("|", ";"):
        for line in sample_lines:
            if sep in line:
                return sep
    raise ValueError("Cannot detect CSV separator in MIMIT file")


def download_file(name, timeout=60):
    """Download one MIMIT CSV and return the decoded text."""
    url = f"{BASE_URL}/{name}"
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "benzabot/1.0"})
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def parse_extraction_date(text):
    """Return the 'Estrazione del YYYY-MM-DD' banner date as a datetime, or None."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(BANNER_PREFIX):
            raw = line[len(BANNER_PREFIX):].strip()
            for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
                try:
                    return datetime.strptime(raw, fmt)
                except ValueError:
                    continue
    return None


def _looks_like_geocoding_placeholder(lat, lon):
    """Detect shared placeholder coords from lazy geocoding (e.g. Milan Duomo
    45.4642035,9.189982 or Rome Colosseum 41.8904,12.5126)."""
    for plat, plon in PLACEHOLDER_COORDS:
        if abs(lat - plat) < 1e-5 and abs(lon - plon) < 1e-5:
            return True
    return False


def parse_stations(text):
    """Parse anagrafica CSV -> list of station dicts (valid coordinates only)."""
    lines = text.splitlines()
    lines = [ln for ln in lines if not ln.strip().startswith(BANNER_PREFIX)]
    if not lines:
        return [], None
    sep = _detect_separator(lines[:20])
    reader = csv.DictReader(lines, delimiter=sep)
    stations = []
    for row in reader:
        try:
            station_id = int(row["idImpianto"])
        except (TypeError, ValueError):
            continue
        lat = _parse_float(row.get("Latitudine"))
        lon = _parse_float(row.get("Longitudine"))
        if lat is None or lon is None:
            continue
        if not (IT_BBOX["lat_min"] <= lat <= IT_BBOX["lat_max"]):
            continue
        if not (IT_BBOX["lon_min"] <= lon <= IT_BBOX["lon_max"]):
            continue
        if _looks_like_geocoding_placeholder(lat, lon):
            continue
        stations.append(
            {
                "_id": station_id,
                "gestore": (row.get("Gestore") or "").strip(),
                "bandiera": (row.get("Bandiera") or "").strip(),
                "tipo_impianto": (row.get("Tipo Impianto") or "").strip(),
                "nome_impianto": (row.get("Nome Impianto") or "").strip(),
                "indirizzo": (row.get("Indirizzo") or "").strip(),
                "comune": (row.get("Comune") or "").strip(),
                "provincia": (row.get("Provincia") or "").strip(),
                "lat": lat,
                "lon": lon,
                "location": {
                    "type": "Point",
                    # GeoJSON order is [longitude, latitude]
                    "coordinates": [lon, lat],
                },
            }
        )
    return stations, sep


def parse_prices(text):
    """Parse prezzi CSV -> dict idImpianto -> list of price dicts."""
    lines = text.splitlines()
    extraction_date = parse_extraction_date(text)
    lines = [ln for ln in lines if not ln.strip().startswith(BANNER_PREFIX)]
    if not lines:
        return {}, extraction_date
    sep = _detect_separator(lines[:20])
    reader = csv.DictReader(lines, delimiter=sep)
    by_station = {}
    for row in reader:
        try:
            station_id = int(row["idImpianto"])
            price = _parse_float(row.get("prezzo"))
        except (TypeError, ValueError):
            continue
        if price is None or price <= 0:
            continue
        dt_comu = (row.get("dtComu") or "").strip()
        parsed_dt = None
        if dt_comu:
            for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
                try:
                    parsed_dt = datetime.strptime(dt_comu, fmt)
                    break
                except ValueError:
                    continue
        by_station.setdefault(station_id, []).append(
        {
            "carburante": (row.get("descCarburante") or "").strip(),
            "prezzo": price,
            "is_self": (row.get("isSelf") or "").strip() in ("1", "true", "TRUE"),
            "dt_comu": parsed_dt,
        }
    )
    return by_station, extraction_date


def fetch_dataset():
    """Download and parse both files.

    Returns (stations, prices_by_station, extraction_date).
    """
    stations_text = download_file(STATIONS_FILE)
    prices_text = download_file(PRICES_FILE)
    stations, _ = parse_stations(stations_text)
    prices, extraction_date = parse_prices(prices_text)
    logger.info(
        "Parsed %d stations (valid coords) and %d stations with prices, estrazione %s",
        len(stations),
        len(prices),
        extraction_date,
    )
    return stations, prices, extraction_date
