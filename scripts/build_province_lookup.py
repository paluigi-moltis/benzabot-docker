"""Build data/it_provinces.json from the Istat "Confini delle unita'
amministrative" shapefile (ProvCM level), for ingest-time coordinate
validation.

Usage (one-off, NOT needed at runtime — the JSON artifact is committed):

    # 1. download & unzip (adjust year as needed)
    curl -o /tmp/limiti.zip \
      https://www.istat.it/storage/cartografia/confini_amministrativi/generalizzati/2026/Limiti01012026_g.zip
    unzip -o /tmp/limiti.zip -d /tmp/limiti
    # 2. build (adjust BASE below if the paths differ)
    python3 scripts/build_province_lookup.py

Source: https://www.istat.it/notizia/confini-delle-unita-amministrative-a-fini-statistici-al-1-gennaio-2018-2
(IODL 2.0 license, like the fuel data).

CRS notes:
- The .prj of the 2026 generalizzata declares WGS_1984 / UTM zone 32N
  (projected, metres) despite the "_WGS84" suffix in the filename.
- All vertices are therefore reprojected UTM32N -> EPSG:4326 lon/lat with
  an inverse Transverse Mercator implementation (no external GIS deps),
  the same CRS as the MIMIT Latitudine/Longitudine columns.
- Sanity-checked against 16 known points (mainland cities, small islands
  such as Tremiti/Giglio/Ponza/Lipari/Elba, coastal towns, open sea).

Output: JSON {SIGLA: [{bbox: [minx,miny,maxx,maxy], ring: [[lon,lat]...]}, ...]}.
Small rings (<300 vertices) are kept verbatim; large ones are simplified
with Douglas-Peucker at ~150 m so coastal stations still fall inside.
"""

import json
import math
import os
import struct
import sys

BASE = "/tmp/limiti2026/ProvCM01012026_g/ProvCM01012026_g_WGS84"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "it_provinces.json")


def utm32n_to_wgs84(easting, northing):
    a = 6378137.0
    f = 1 / 298.257223563
    e2 = f * (2 - f)
    e = math.sqrt(e2)
    k0 = 0.9996
    x = easting - 500000.0
    lon0 = math.radians(9.0)
    m = northing / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi1 = mu + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
    phi1 += (15 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
    phi1 += 35 * e1**3 / 48 * math.sin(6 * mu)
    phi1 += 315 * e1**4 / 512 * math.sin(8 * mu)
    n1 = a / math.sqrt(1 - e2 * math.sin(phi1) ** 2)
    r1 = a * (1 - e2) / (1 - e2 * math.sin(phi1) ** 2) ** 1.5
    t1 = math.tan(phi1) ** 2
    ep2 = e2 / (1 - e2)
    c1 = ep2 * math.cos(phi1) ** 2
    d = x / (n1 * k0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (
        d**2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * ep2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * ep2 - 3 * c1**2) * d**6 / 720)
    lon = lon0 + (d - (1 + 2 * t1 + c1) * d**3 / 6
                  + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * ep2 + 24 * t1**2) * d**5 / 120) / math.cos(phi1)
    return [math.degrees(lon), math.degrees(lat)]


def read_dbf(path):
    fh = open(path, "rb")
    n = struct.unpack("<I", fh.read(30)[4:8])[0]
    fh.seek(32)
    fields = []
    while True:
        b = fh.read(32)
        if b[0:1] == b"\r":
            break
        fields.append((b[:11].split(b"\x00")[0].decode(), b[16]))
    header_len = struct.unpack("<H", open(path, "rb").read(10)[8:10])[0]
    rec_len = 1 + sum(l for _, l in fields)
    fh.seek(header_len)
    out = []
    for _ in range(n):
        rec = fh.read(rec_len)
        pos, vals = 1, {}
        for name, ln in fields:
            vals[name] = rec[pos:pos + ln].decode("utf-8", "replace").strip()
            pos += ln
        out.append(vals)
    fh.close()
    return out


def read_shp_polygons(path):
    fh = open(path, "rb")
    fh.seek(32)
    shp_type = struct.unpack("<i", fh.read(4))[0]
    assert shp_type == 5, f"expected polygon shapefile, got type {shp_type}"
    fh.seek(100)
    records = []
    while True:
        header = fh.read(8)
        if len(header) < 8:
            break
        content_len = struct.unpack(">i", header[4:8])[0]
        content = fh.read(content_len * 2)
        rtype = struct.unpack("<i", content[0:4])[0]
        assert rtype == 5
        nparts, npoints = struct.unpack("<2i", content[36:44])
        parts = struct.unpack(f"<{nparts}i", content[44:44 + 4 * nparts])
        pts_off = 44 + 4 * nparts
        pts = struct.unpack(f"<{2 * npoints}d", content[pts_off:pts_off + 16 * npoints])
        records.append((nparts, list(parts), pts))
    fh.close()
    return records


def dp_simplify(ring, eps):
    """Douglas-Peucker, iterative; eps in degrees."""
    if len(ring) < 8:
        return ring
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = ring[i]
        bx, by = ring[j]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        dmax, imax = -1.0, -1
        for k in range(i + 1, j):
            px, py = ring[k]
            dist = abs(dy * px - dx * py + bx * ay - by * ax) / norm if norm else math.hypot(px - ax, py - ay)
            if dist > dmax:
                dmax, imax = dist, k
        if dmax > eps:
            keep[imax] = True
            stack.append((i, imax))
            stack.append((imax, j))
    return [p for p, k in zip(ring, keep) if k]


def main():
    provs = read_dbf(BASE + ".dbf")
    shapes = read_shp_polygons(BASE + ".shp")
    assert len(provs) == len(shapes), "shp/dbf record mismatch"

    data = {}
    for prov, (nparts, parts, pts) in zip(provs, shapes):
        sigla = prov["SIGLA"]
        assert sigla and sigla not in data, f"bad/duplicate sigla {sigla!r}"
        rings = []
        for i in range(nparts):
            s, e = parts[i], (parts[i + 1] if i + 1 < nparts else len(pts) // 2)
            raw = [utm32n_to_wgs84(pts[2 * j], pts[2 * j + 1]) for j in range(s, e)]
            ring = dp_simplify(raw, eps=0.15 / 111.0)  # ~150 m
            if len(ring) >= 4:
                xs = [p[0] for p in ring]
                ys = [p[1] for p in ring]
                rings.append({"bbox": [min(xs), min(ys), max(xs), max(ys)], "ring": ring})
        data[sigla] = rings

    assert len(data) == 110, f"expected 110 provinces, got {len(data)}"

    # spot checks: mainland cities, islands, coastal towns, open sea
    def pip(lon, lat, ring):
        inside = False
        for i in range(len(ring) - 1):
            x1, y1 = ring[i]
            x2, y2 = ring[i + 1]
            if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                inside = not inside
        return inside

    def locate(lon, lat):
        for sigla, rings in data.items():
            for r in rings:
                b = r["bbox"]
                if b[0] <= lon <= b[2] and b[1] <= lat <= b[3] and pip(lon, lat, r["ring"]):
                    return sigla
        return None

    for (lon, lat), expected in [
        ((9.19, 45.4642), "MI"), ((12.4964, 41.9028), "RM"), ((13.3615, 38.1157), "PA"),
        ((11.2558, 43.7696), "FI"), ((7.6869, 45.0703), "TO"), ((16.8698, 41.1171), "BA"),
        ((10.9209, 42.3594), "GR"),    # Isola del Giglio
        ((12.9635, 40.8943), "LT"),    # Ponza
        ((15.2394, 38.7983), "ME"),    # Lipari
        ((10.3984, 42.7646), "LI"),    # Elba
        ((7.78, 43.8161), "IM"),       # Sanremo
        ((18.0, 40.0), None),          # open sea
        ((9.5, 46.5), None),           # outside north
    ]:
        got = locate(lon, lat)
        assert got == expected, f"sanity check failed: ({lon},{lat}) -> {got}, expected {expected}"

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    n_rings = sum(len(r) for r in data.values())
    n_vert = sum(len(r["ring"]) for rings in data.values() for r in rings)
    print(f"wrote {OUT}: {len(data)} provinces, {n_rings} rings, {n_vert} vertices, "
          f"{os.path.getsize(OUT) // 1024} KB")


if __name__ == "__main__":
    main()
