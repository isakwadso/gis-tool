"""Build the static pasture layer for the map.

Downloads Jordbruksverket's farm blocks (jordbruksblock) of the chosen land
type(s) for one region from their public WFS, simplifies the outlines, and
writes small chunk files the phone can load on demand:

  site/data/cells.json             weather grid cells that contain pasture
  site/data/pasture/<ci>_<cj>.json polygons, grouped by chunk of grid cells

Run:  python scripts/build_pasture.py            (downloads from the WFS)
      python scripts/build_pasture.py --from-file sample.json   (offline test)

Needs: shapely, pyproj
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.ops import transform
from shapely.validation import make_valid

ROOT = Path(__file__).resolve().parent.parent
WFS = "https://epub.sjv.se/inspire/inspire/wfs"
LAYER = "inspire:arslager_block"
PAGE_SIZE = 5000
QUANT = 100_000  # coordinate quantisation: 1e-5 degrees (~1 m)
USER_AGENT = "gis-tool pasture build (https://github.com/isakwadso/gis-tool)"

TO_WGS84 = Transformer.from_crs(3006, 4326, always_xy=True)


def http_json(params: dict, retries: int = 4) -> dict:
    url = WFS + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == retries:
                raise RuntimeError(f"WFS request failed: {exc}\n{url}") from exc
            wait = 20 * (attempt + 1)
            print(f"  request failed ({exc}); retrying in {wait}s", flush=True)
            time.sleep(wait)
    raise AssertionError("unreachable")


def base_params() -> dict:
    return {
        "SERVICE": "WFS",
        "REQUEST": "GetFeature",
        "VERSION": "2.0.0",
        "TYPENAMES": LAYER,
        "outputFormat": "application/json",
    }


def latest_year() -> int:
    p = base_params() | {"COUNT": 1, "propertyName": "arslager", "sortBy": "arslager DESC"}
    return int(http_json(p)["features"][0]["properties"]["arslager"])


def cql_filter(year: int, prefix: str, types: list[str]) -> str:
    quoted = ",".join("'" + t.replace("'", "''") + "'" for t in types)
    return f"arslager={year} AND region_kod LIKE '{prefix}%' AND agoslag IN ({quoted})"


def download(year: int, prefix: str, types: list[str]) -> list[dict]:
    cql = cql_filter(year, prefix, types)
    print(f"Filter: {cql}", flush=True)
    features: list[dict] = []
    expected = None
    start = 0
    while True:
        p = base_params() | {
            "COUNT": PAGE_SIZE,
            "STARTINDEX": start,
            "sortBy": "blockid",
            "CQL_FILTER": cql,
        }
        page = http_json(p)
        expected = page.get("numberMatched", expected)
        batch = page.get("features", [])
        features.extend(batch)
        print(f"  got {len(features)} / {expected}", flush=True)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
        time.sleep(2)  # be gentle with the public server
    if expected is not None and len(features) != int(expected):
        raise RuntimeError(f"Downloaded {len(features)} features but server reported {expected}")
    return features


def polygons_of(geom) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if hasattr(geom, "geoms"):  # GeometryCollection from make_valid
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(polygons_of(g))
        return out
    return []


def check_axis_order(features: list[dict]) -> None:
    """GeoJSON from this WFS is easting, northing in SWEREF 99 TM. Fail loudly if not."""
    for f in features[:50]:
        g = f.get("geometry") or {}
        ring = (g.get("coordinates") or [[None]])[0]
        if g.get("type") == "MultiPolygon":
            ring = g["coordinates"][0][0]
        x, y = ring[0][:2]
        if not (200_000 <= x <= 1_000_000 and 6_000_000 <= y <= 7_800_000):
            raise RuntimeError(f"Unexpected coordinates {x},{y}: not SWEREF 99 TM easting/northing")


def encode_ring(coords) -> list[int] | None:
    pts: list[tuple[int, int]] = []
    for lon, lat in coords:
        q = (round(lon * QUANT), round(lat * QUANT))
        if not pts or q != pts[-1]:
            pts.append(q)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()  # rings are implicitly closed
    if len(pts) < 3:
        return None
    flat = [pts[0][0], pts[0][1]]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        flat += [x1 - x0, y1 - y0]
    return flat


def cell_of(lat: float, lon: float, grid: dict) -> tuple[int, int]:
    i = math.floor((lat - grid["origin_lat"]) / grid["dlat"])
    j = math.floor((lon - grid["origin_lon"]) / grid["dlon"])
    return i, j


def build(features: list[dict], cfg: dict, year: int, out_dir: Path) -> dict:
    grid = cfg["grid"]
    cpc = grid["cells_per_chunk"]
    tol = cfg["pasture"]["simplify_m"]
    check_axis_order(features)

    chunks: dict[str, list] = {}
    cells: dict[str, dict] = {}
    skipped = 0

    for f in features:
        props = f.get("properties", {})
        if not f.get("geometry"):
            skipped += 1
            continue
        g = shape(f["geometry"])
        if not g.is_valid:
            g = make_valid(g)
        g = g.simplify(tol, preserve_topology=True)
        polys = [p for p in polygons_of(g) if p.area >= 10]  # drop slivers < 10 m²
        if not polys:
            skipped += 1
            continue
        geom = MultiPolygon(polys) if len(polys) > 1 else polys[0]
        area_ha = float(props.get("areal") or geom.area / 10_000)

        rp = geom.representative_point()
        lon, lat = TO_WGS84.transform(rp.x, rp.y)
        i, j = cell_of(lat, lon, grid)
        cid = f"{i}_{j}"
        chunk_id = f"{i // cpc}_{j // cpc}"

        enc_polys = []
        for p in polys:
            rings = []
            for ring in [p.exterior, *p.interiors]:
                xs, ys = TO_WGS84.transform(*ring.xy)
                enc = encode_ring(zip(xs, ys))
                if enc:
                    rings.append(enc)
            if rings:
                enc_polys.append(rings)
        if not enc_polys:
            skipped += 1
            continue

        chunks.setdefault(chunk_id, []).append(
            [str(props.get("blockid", "")), cid, round(area_ha * 100), enc_polys]
        )
        c = cells.setdefault(cid, {"i": i, "j": j, "ha": 0.0, "n": 0, "wlat": 0.0, "wlon": 0.0})
        c["ha"] += area_ha
        c["n"] += 1
        c["wlat"] += lat * area_ha
        c["wlon"] += lon * area_ha

    # Write chunks deterministically so unchanged data gives byte-identical files.
    pasture_dir = out_dir / "pasture"
    pasture_dir.mkdir(parents=True)
    digest = hashlib.sha256()
    chunk_index = {}
    for chunk_id in sorted(chunks):
        feats = sorted(chunks[chunk_id], key=lambda r: r[0])
        body = json.dumps({"f": feats}, separators=(",", ":"))
        (pasture_dir / f"{chunk_id}.json").write_text(body, encoding="utf-8")
        digest.update(chunk_id.encode() + body.encode())
        ci, cj = map(int, chunk_id.split("_"))
        s = grid["origin_lat"] + ci * cpc * grid["dlat"]
        w = grid["origin_lon"] + cj * cpc * grid["dlon"]
        chunk_index[chunk_id] = [round(s, 5), round(w, 5),
                                 round(s + cpc * grid["dlat"], 5), round(w + cpc * grid["dlon"], 5)]

    cell_rows = []
    for cid in sorted(cells, key=lambda k: (cells[k]["i"], cells[k]["j"])):
        c = cells[cid]
        cell_rows.append([cid, round(c["wlat"] / c["ha"], 4), round(c["wlon"] / c["ha"], 4),
                          round(c["ha"], 1), c["n"]])

    meta = {
        "version": digest.hexdigest()[:12],
        "source": {
            "name": "Jordbruksverket, jordbruksblock (årslager)",
            "url": "https://jordbruksverket.se/e-tjanster-databaser-och-appar/e-tjanster-och-databaser-stod/kartor-och-gis",
            "year": year,
            "types": cfg["pasture"]["types"],
            "region": cfg["region"]["name"],
        },
        "n_blocks": sum(len(v) for v in chunks.values()),
        "skipped": skipped,
        "grid": {k: grid[k] for k in ("origin_lat", "origin_lon", "dlat", "dlon", "cells_per_chunk")},
        "q": QUANT,
        "cells": cell_rows,
        "chunks": chunk_index,
    }
    (out_dir / "cells.json").write_text(json.dumps(meta, separators=(",", ":"), ensure_ascii=False),
                                        encoding="utf-8")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-file", type=Path, help="use a saved WFS GeoJSON response instead of downloading")
    ap.add_argument("--out", type=Path, default=ROOT / "site" / "data")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    prefix = cfg["region"]["region_kod_prefix"]
    types = cfg["pasture"]["types"]

    if args.from_file:
        fc = json.loads(args.from_file.read_text(encoding="utf-8"))
        features = fc["features"]
        year = int(features[0]["properties"].get("arslager", 0)) if features else 0
    else:
        year_cfg = cfg["pasture"]["source_year"]
        year = latest_year() if year_cfg == "latest" else int(year_cfg)
        print(f"Using block data from year {year}", flush=True)
        features = download(year, prefix, types)

    if not features:
        print("No features returned; leaving existing data untouched.", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp_out = Path(tmp) / "data"
        tmp_out.mkdir()
        meta = build(features, cfg, year, tmp_out)
        # Replace the old layer only once the new one is complete.
        args.out.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(args.out / "pasture", ignore_errors=True)
        shutil.copytree(tmp_out / "pasture", args.out / "pasture")
        shutil.copy2(tmp_out / "cells.json", args.out / "cells.json")

    size = sum(p.stat().st_size for p in (args.out / "pasture").glob("*.json"))
    print(f"Wrote {meta['n_blocks']} blocks ({meta['skipped']} skipped) in {len(meta['chunks'])} chunks, "
          f"{len(meta['cells'])} weather cells, {size / 1e6:.1f} MB, version {meta['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
