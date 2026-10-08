"""Build the list of place names drawn on top of the map.

Asks OpenStreetMap's Overpass API for cities, towns and villages inside the
region (by its ISO 3166-2 code in config.json) and writes

  site/data/places.json   {"source": ..., "places": [[name, rank, lat, lon, population], ...]}

rank: 0 = city, 1 = town, 2 = village. Sorted by rank, then population.
Data © OpenStreetMap contributors, ODbL.

Run:  python scripts/build_places.py                 (downloads)
      python scripts/build_places.py --from-file x.json   (saved Overpass JSON, for tests)
Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
USER_AGENT = "gis-tool place labels (https://github.com/isakwadso/gis-tool)"
RANK = {"city": 0, "town": 1, "village": 2}


def query(iso_code: str) -> str:
    return f"""[out:json][timeout:90];
area["ISO3166-2"="{iso_code}"][admin_level=4]->.a;
node(area.a)["place"~"^(city|town|village)$"]["name"];
out tags qt;"""


def download(iso_code: str) -> dict:
    body = urllib.parse.urlencode({"data": query(iso_code)}).encode()
    last = None
    for url in ENDPOINTS:
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=180) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
                print(f"  {url} failed ({exc}); retrying", flush=True)
                time.sleep(30 * (attempt + 1))
    raise RuntimeError(f"Overpass request failed: {last}")


def population(tags: dict) -> int:
    digits = re.sub(r"[^\d]", "", str(tags.get("population", "")))
    return int(digits) if digits else 0


def build(data: dict, iso_code: str) -> dict:
    places = []
    seen = set()
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name, kind = tags.get("name"), tags.get("place")
        if not name or kind not in RANK or "lat" not in el:
            continue
        key = (name, round(el["lat"], 3), round(el["lon"], 3))
        if key in seen:
            continue
        seen.add(key)
        places.append([name, RANK[kind], round(el["lat"], 5), round(el["lon"], 5), population(tags)])
    places.sort(key=lambda p: (p[1], -p[4], p[0]))
    return {
        "source": {"name": "OpenStreetMap contributors (ODbL), via Overpass API", "area": iso_code},
        "places": places,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-file", type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "site" / "data" / "places.json")
    args = ap.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    iso_code = cfg["region"]["iso3166_2"]
    data = (json.loads(args.from_file.read_text(encoding="utf-8")) if args.from_file
            else download(iso_code))
    out = build(data, iso_code)
    if len(out["places"]) < 5:
        print(f"Only {len(out['places'])} places returned; keeping the existing file.", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    counts = [sum(1 for p in out["places"] if p[1] == r) for r in range(3)]
    print(f"Wrote {len(out['places'])} places (cities {counts[0]}, towns {counts[1]}, villages {counts[2]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
