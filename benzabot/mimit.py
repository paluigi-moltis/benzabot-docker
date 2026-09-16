"""Download and parse MIMIT open-data fuel files (prezzi + anagrafica impianti)."""

import csv
import logging
from datetime import datetime

import requests

from benzabot.provinces import ProvinceLookup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.mimit.gov.it/images/exportCSV"
PRICES_FILE = "prezzo_alle_8.csv"
STATIONS_FILE = "anagrafica_impianti_attivi.csv"

# Since 2026-02-10 the separator is '|'; before it was ';'. We detect it.
BANNER_PREFIX = "Estrazione del "

# Italy-ish bounding box used to discard stations with broken coordinates
# (the open data is known to contain swapped/zero lat-lon pairs).
IT_BBOX = {"lat_min": 35.0, "lat_max": 47.5, "lon_min": 6.0, "lon_max": 19.0}

# Coordinates claimed by stations with mutually incompatible addresses
# (different streets, even different towns): lazy-geocoding fallbacks to
# famous landmarks. Verified in the source data, e.g. three unrelated
# stations at exactly Milan Duomo's coords, five at Rome Colosseum's.
# Only landmarks are filtered: other coordinate-sharing groups are mostly
# genuine (motorway service areas, same-address duplicates).
PLACEHOLDER_COORDS = [
    (45.4642035, 9.189982),  # Milan Duomo
    (41.8904, 12.5126),  # Rome Colosseum
    (41.89041, 12.5126),
    (41.8947, 12.49348),  # Rome centre
]

_province_lookup = None


def _get_province_lookup():
    """Lazy-load the Istat province polygons (needed only if a caller wants
    province validation; tests can inject a stub via set_province_lookup)."""
    global _province_lookup
    if _province_lookup is None:
        _province_lookup = ProvinceLookup()
    return _province_lookup


def set_province_lookup(lookup):
    global _province_lookup
    _province_lookup = lookup


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


def parse_stations(text, validate_provinces=True, deduplicate=True):
    """Parse anagrafica CSV -> list of station dicts (valid coordinates only).

    Validation steps (all ingest-time, cheap at query time):
    - bbox check (catches zero/swapped coordinates)
    - landmark placeholder coordinates (Duomo, Colosseo, ...)
    - if validate_provinces: the declared Provincia must be compatible with
      the point-in-polygon test against the Istat province boundaries
      (data/it_provinces.json, WGS84). Stations in the sea or attributed to
      an impossible province are dropped (~1.6% of the file).
    - if deduplicate: when several stations share the exact same coordinate
      (motorway service areas, duplicated registry entries), keep one station
      per (coordinate, price signature) is done later in the ingest; here we
      keep stations that at least differ by id.
    """
    lines = text.splitlines()
    lines = [ln for ln in lines if not ln.strip().startswith(BANNER_PREFIX)]
    if not lines:
        return [], None
    sep = _detect_separator(lines[:20])
    reader = csv.DictReader(lines, delimiter=sep)
    lookup = _get_province_lookup() if validate_provinces else None
    stations = []
    dropped_province = 0
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
        if lookup is not None:
            declared = (row.get("Provincia") or "").strip().upper()
            hits = lookup.find(lon, lat)
            compatible = lookup.matches(declared, lon, lat) if declared else bool(hits)
            if not compatible:
                dropped_province += 1
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
    if dropped_province:
        logger.info("Dropped %d stations whose coordinates contradict the declared province", dropped_province)
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
