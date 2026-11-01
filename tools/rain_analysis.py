"""Does rain over the last 30-90 days relate to field mushroom finds in Skåne?

Runs on GitHub Actions (.github/workflows/analysis.yml); results go to the
"analysis" branch. Sources:
  - finds: GBIF occurrences of Agaricus campestris (includes Artportalen)
  - rain: SMHI station data, daily precipitation (parameter 5), open data CC BY 4.0
  - a small Open-Meteo check (~160 calls, reused on re-runs) comparing the map's
    model rain with station rain over 60 days

Tests:
  1. Across years: is the rain before a find higher than in the same weeks of
     other years (within +-10 years) at the nearest station? 50 % = no relation.
  2. Window length: the same for 10, 20, 30, 45, 60 and 90 days, and for days
     11-60 only (rain before the last 10 days, which the map already uses).
  3. Within the season: the find day against 20-40 days before and after.
  4. Years: finds per year (divided by all fungus records, to allow for how
     many people report) against regional rain.
Also writes the 60-day rain climatology used for the map's colour classes.

  python tools/rain_analysis.py <out-dir> [previous-openmeteo.json]
Standard library only.
"""

from __future__ import annotations

import json
import math
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

UA = "gis-tool analysis (https://github.com/isakwadso/gis-tool)"
BBOX = (55.3, 56.6, 12.4, 14.7)  # lat min, lat max, lon min, lon max
SMHI = "https://opendata-download-metobs.smhi.se/api/version/1.0/parameter/5"
GBIF = "https://api.gbif.org/v1/occurrence/search"
OM_HIST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
TAXON = 5243458  # Agaricus campestris
FUNGI = 5        # kingdom Fungi
FIRST_YEAR = 2005
WINDOWS = [10, 20, 30, 45, 60, 90]
MAIN = 60
CLIM_YEARS = (1996, 2025)
YEAR_SPAN = 10
MAX_KM = 25
MIN_OTHER_YEARS = 10
MAX_MISSING = 2


def log(*a):
    print(*a, flush=True)


def get(url: str, tries: int = 4) -> bytes | None:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == tries - 1:
                raise
            log(f"  HTTP {e.code} for {url[:120]}; retrying")
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            log(f"  {e} for {url[:120]}; retrying")
        time.sleep(5 * (i + 1))
    return None


def km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- finds (GBIF)

def gbif_finds():
    recs, offset = [], 0
    while True:
        q = {"taxonKey": TAXON, "country": "SE", "hasCoordinate": "true", "hasGeospatialIssue": "false",
             "occurrenceStatus": "PRESENT", "decimalLatitude": f"{BBOX[0]},{BBOX[1]}",
             "decimalLongitude": f"{BBOX[2]},{BBOX[3]}", "limit": 300, "offset": offset}
        d = json.loads(get(GBIF + "?" + urllib.parse.urlencode(q)))
        recs += d["results"]
        offset += 300
        if d.get("endOfRecords") or offset >= d.get("count", 0):
            break
        time.sleep(1)
    log(f"GBIF: {len(recs)} records of A. campestris in the box")

    why = defaultdict(int)
    finds, seen = [], set()
    for r in recs:
        sp = (r.get("stateProvince") or "").lower()
        if sp and "skåne" not in sp and "skane" not in sp:
            why["outside Skåne"] += 1
            continue
        y, m, dd = r.get("year"), r.get("month"), r.get("day")
        ev = r.get("eventDate") or ""
        if not (y and m and dd):
            why["no exact day"] += 1
            continue
        if "/" in ev and ev.split("/")[0][:10] != ev.split("/")[1][:10]:
            why["date range"] += 1
            continue
        if not 7 <= m <= 11:
            why["outside Jul-Nov"] += 1
            continue
        if y < FIRST_YEAR:
            why[f"before {FIRST_YEAR}"] += 1
            continue
        unc = r.get("coordinateUncertaintyInMeters")
        if unc is not None and unc > 5000:
            why["position uncertain > 5 km"] += 1
            continue
        lat, lon = r["decimalLatitude"], r["decimalLongitude"]
        key = (y, m, dd, round(lat, 2), round(lon, 2))
        if key in seen:
            why["duplicate"] += 1
            continue
        seen.add(key)
        finds.append({"date": date(y, m, dd), "lat": lat, "lon": lon})
    log(f"GBIF: kept {len(finds)} finds; dropped {dict(why)}")
    return finds, dict(why)


def gbif_year_counts(taxon):
    q = {"taxonKey": taxon, "country": "SE", "hasCoordinate": "true", "occurrenceStatus": "PRESENT",
         "decimalLatitude": f"{BBOX[0]},{BBOX[1]}", "decimalLongitude": f"{BBOX[2]},{BBOX[3]}",
         "month": "7,11", "year": f"{FIRST_YEAR},2026", "facet": "year", "facetLimit": 100, "limit": 0}
    d = json.loads(get(GBIF + "?" + urllib.parse.urlencode(q)))
    out = {}
    for f in d.get("facets", []):
        if f["field"] == "YEAR":
            out = {int(c["name"]): c["count"] for c in f["counts"]}
    return out


# ---------------------------------------------------------------- rain (SMHI)

class Series:
    """Daily values with prefix sums for fast window totals."""

    def __init__(self, values: dict[date, float]):
        self.start = min(values)
        n = (max(values) - self.start).days + 1
        self.sum = [0.0] * (n + 1)
        self.cnt = [0] * (n + 1)
        for i in range(n):
            v = values.get(self.start + timedelta(days=i))
            self.sum[i + 1] = self.sum[i] + (v if v is not None else 0.0)
            self.cnt[i + 1] = self.cnt[i] + (1 if v is not None else 0)
        self.n = n

    def total(self, end: date, days: int, skip_last: int = 0) -> float | None:
        """Sum of the `days` days ending on `end` (inclusive), leaving out the last
        `skip_last` of them. Up to MAX_MISSING missing days are filled pro rata."""
        hi = (end - self.start).days + 1 - skip_last
        lo = (end - self.start).days + 1 - days
        if lo < 0 or hi > self.n or hi <= lo:
            return None
        have = self.cnt[hi] - self.cnt[lo]
        want = hi - lo
        if want - have > MAX_MISSING:
            return None
        return (self.sum[hi] - self.sum[lo]) * want / have


def smhi_stations():
    d = json.loads(get(SMHI + ".json"))
    out = []
    for s in d["station"]:
        lat, lon = s["latitude"], s["longitude"]
        if not (BBOX[0] - 0.15 <= lat <= BBOX[1] + 0.15 and BBOX[2] - 0.25 <= lon <= BBOX[3] + 0.25):
            continue
        to_y = datetime.fromtimestamp(s["to"] / 1000, timezone.utc).year if s.get("to") else 2100
        from_y = datetime.fromtimestamp(s["from"] / 1000, timezone.utc).year if s.get("from") else 1900
        if to_y < 1996 or from_y > 2016:
            continue
        out.append({"id": s["id"], "name": s["name"], "lat": lat, "lon": lon, "from": from_y, "to": to_y})
    return out


def parse_smhi_csv(raw: bytes) -> dict[date, float]:
    lines = raw.decode("utf-8-sig", "replace").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("Från Datum"):
            cols = line.split(";")
            break
    else:
        return {}
    try:
        di, vi = cols.index("Representativt dygn"), cols.index("Nederbördsmängd")
    except ValueError:
        return {}
    out = {}
    for line in lines[i + 1:]:
        p = line.split(";")
        if len(p) <= vi:
            continue
        try:
            out[date.fromisoformat(p[di].strip())] = float(p[vi])
        except ValueError:
            continue
    return out


def smhi_series(sid):
    vals = {}
    for period in ("corrected-archive", "latest-months"):
        raw = get(f"{SMHI}/station/{sid}/period/{period}/data.csv")
        if raw:
            vals.update(parse_smhi_csv(raw))
        time.sleep(0.3)
    return vals


# ---------------------------------------------------------------- statistics

def rank(x, others):
    below = sum(1 for o in others if o < x)
    ties = sum(1 for o in others if o == x)
    return (below + 0.5 * ties) / len(others)


def mean(v):
    return sum(v) / len(v) if v else float("nan")


def cluster_ci(values_by_year: dict[int, list[float]], reps=2000, seed=7):
    """95 % interval for the mean, resampling whole years (finds in one year share weather)."""
    rnd = random.Random(seed)
    years = list(values_by_year)
    boots = []
    for _ in range(reps):
        pick = [rnd.choice(years) for _ in years]
        vals = [v for y in pick for v in values_by_year[y]]
        boots.append(mean(vals))
    boots.sort()
    return boots[int(0.025 * reps)], boots[int(0.975 * reps)]


def spearman(x, y):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r
    rx, ry = ranks(x), ranks(y)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else float("nan")


def perm_p(x, y, reps=5000, seed=3):
    rnd = random.Random(seed)
    r0 = abs(spearman(x, y))
    yy = list(y)
    hits = 0
    for _ in range(reps):
        rnd.shuffle(yy)
        if abs(spearman(x, yy)) >= r0:
            hits += 1
    return (hits + 1) / (reps + 1)


def pct(v, q):
    v = sorted(v)
    k = (len(v) - 1) * q
    f = math.floor(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


# ---------------------------------------------------------------- Open-Meteo check

def open_meteo_check(stations, series, prev_path):
    if prev_path and Path(prev_path).exists():
        log("Open-Meteo check: reusing previous result (no new calls)")
        return json.loads(Path(prev_path).read_text())
    picks = []
    for st in sorted(stations, key=lambda s: -s["n2325"]):
        if st["n2325"] < 500:
            continue
        if all(km(st["lat"], st["lon"], p["lat"], p["lon"]) > 35 for p in picks):
            picks.append(st)
        if len(picks) == 4:
            break
    out = {"stations": [], "calls": 0}
    for st in picks:
        om = {}
        for y in (2023, 2024, 2025):
            q = {"latitude": st["lat"], "longitude": st["lon"], "daily": "precipitation_sum",
                 "timezone": "Europe/Stockholm", "start_date": f"{y}-06-01", "end_date": f"{y}-11-30"}
            d = json.loads(get(OM_HIST + "?" + urllib.parse.urlencode(q)))
            out["calls"] += round(183 / 14, 1)
            for t, v in zip(d["daily"]["time"], d["daily"]["precipitation_sum"]):
                if v is not None:
                    om[date.fromisoformat(t)] = v
            time.sleep(2)
        s_om, s_st = Series(om), series[st["id"]]
        pairs = []
        for y in (2023, 2024, 2025):
            dd = date(y, 8, 1)
            while dd <= date(y, 11, 30):
                a, b = s_om.total(dd, MAIN), s_st.total(dd, MAIN)
                if a is not None and b is not None:
                    pairs.append((a, b))
                dd += timedelta(days=1)
        if not pairs:
            continue
        ratio = sum(a for a, _ in pairs) / sum(b for _, b in pairs)
        xs, ys = [a for a, _ in pairs], [b for _, b in pairs]
        mx, my = mean(xs), mean(ys)
        r = sum((a - mx) * (b - my) for a, b in pairs) / math.sqrt(
            sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
        out["stations"].append({"id": st["id"], "name": st["name"], "days": len(pairs),
                                "open_meteo_over_station": round(ratio, 3), "pearson_r": round(r, 3)})
        log(f"  {st['name']}: Open-Meteo/station 60-day rain = {ratio:.2f}, r = {r:.2f} ({len(pairs)} days)")
    return out


# ---------------------------------------------------------------- main

def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
    prev_om = sys.argv[2] if len(sys.argv) > 2 else None
    out_dir.mkdir(parents=True, exist_ok=True)
    res = {"generated": datetime.now(timezone.utc).isoformat(timespec="minutes")}

    finds, dropped = gbif_finds()
    res["finds"] = {"kept": len(finds), "dropped": dropped}

    stations = smhi_stations()
    log(f"SMHI: {len(stations)} precipitation stations in and around Skåne")
    series = {}
    for k, st in enumerate(stations):
        vals = smhi_series(st["id"])
        if len(vals) < 365:
            continue
        series[st["id"]] = Series(vals)
        st["n2325"] = sum(1 for d in vals if 2023 <= d.year <= 2025 and 6 <= d.month <= 11)
        if k % 10 == 0:
            log(f"  {k + 1}/{len(stations)} stations downloaded")
    stations = [s for s in stations if s["id"] in series]
    log(f"SMHI: {len(stations)} stations with data")

    # ---- climatology of 60-day totals (for the colour classes)
    clim_all, clim_month, clim_year = [], defaultdict(list), []
    used = 0
    for st in stations:
        s = series[st["id"]]
        vals_st, yrs = [], 0
        for y in range(CLIM_YEARS[0], CLIM_YEARS[1] + 1):
            yr_vals = []
            dd = date(y, 7, 1)
            while dd <= date(y, 11, 30):
                v = s.total(dd, MAIN)
                if v is not None:
                    yr_vals.append((dd.month, v))
                dd += timedelta(days=1)
            if len(yr_vals) >= 140:
                yrs += 1
                vals_st += yr_vals
            ys = [s.total(date(y, m, 15), MAIN) for m in range(1, 13)]
            if all(v is not None for v in ys):
                clim_year += ys
        if yrs >= 20:
            used += 1
            for m, v in vals_st:
                clim_all.append(v)
                clim_month[m].append(v)
    qs = [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95]
    res["climatology_60d"] = {
        "stations": used, "years": CLIM_YEARS, "window_end": "1 Jul - 30 Nov",
        "percentiles_mm": {str(q): round(pct(clim_all, q), 1) for q in qs},
        "by_month_end": {str(m): {str(q): round(pct(v, q), 1) for q in (0.1, 0.25, 0.5, 0.75, 0.9)}
                         for m, v in sorted(clim_month.items())},
        "whole_year_mid_month": {str(q): round(pct(clim_year, q), 1) for q in qs} if clim_year else None,
    }
    log("Climatology 60 days (Jul-Nov ends):", res["climatology_60d"]["percentiles_mm"])

    # ---- Open-Meteo vs station
    try:
        res["open_meteo_check"] = open_meteo_check(stations, series, prev_om)
        (out_dir / "openmeteo.json").write_text(json.dumps(res["open_meteo_check"], indent=1))
    except Exception as e:  # noqa: BLE001
        log(f"Open-Meteo check failed: {e}")
        res["open_meteo_check"] = {"error": str(e)}

    # ---- per find: nearest station with the data needed
    longest = max(WINDOWS)
    rows = []
    no_station = 0
    for f in finds:
        d0 = f["date"] - timedelta(days=1)  # rain up to the day before the find
        best = None
        for st in sorted(stations, key=lambda s: km(f["lat"], f["lon"], s["lat"], s["lon"])):
            dist = km(f["lat"], f["lon"], st["lat"], st["lon"])
            if dist > MAX_KM:
                break
            s = series[st["id"]]
            if s.total(d0, longest) is None:
                continue
            others_y = []
            for y in range(f["date"].year - YEAR_SPAN, f["date"].year + YEAR_SPAN + 1):
                if y == f["date"].year:
                    continue
                e = date(y, f["date"].month, f["date"].day) - timedelta(days=1)
                if s.total(e, longest) is not None:
                    others_y.append(e)
            if len(others_y) >= MIN_OTHER_YEARS:
                best = (st, dist, s, others_y)
                break
        if not best:
            no_station += 1
            continue
        st, dist, s, others_y = best
        row = {"date": f["date"].isoformat(), "lat": round(f["lat"], 1), "lon": round(f["lon"], 1),
               "station": st["name"], "km": round(dist, 1), "years_compared": len(others_y)}
        for w in WINDOWS:
            x = s.total(d0, w)
            oth = [s.total(e, w) for e in others_y]
            row[f"r{w}"] = rank(x, oth)
            row[f"mm{w}"] = round(x, 1)
            row[f"normal{w}"] = round(statistics.median(oth), 1)
        x = s.total(d0, 60, skip_last=10)
        oth = [s.total(e, 60, skip_last=10) for e in others_y]
        row["r11_60"] = rank(x, oth)
        # within the season: the find day against 20-40 days before and after, same year
        ctrl = [s.total(d0 + timedelta(days=o), MAIN) for o in list(range(-40, -19)) + list(range(20, 41))]
        ctrl = [c for c in ctrl if c is not None]
        row["r60_within"] = rank(s.total(d0, MAIN), ctrl) if len(ctrl) >= 30 else None
        rows.append(row)
    log(f"Finds with a station within {MAX_KM} km: {len(rows)} (no station: {no_station})")
    res["finds"]["analysed"] = len(rows)
    res["finds"]["no_station"] = no_station
    res["finds"]["median_km"] = round(statistics.median(r["km"] for r in rows), 1)
    per_year = defaultdict(int)
    for r in rows:
        per_year[int(r["date"][:4])] += 1
    res["finds"]["per_year"] = dict(sorted(per_year.items()))

    def summarise(key):
        by_year = defaultdict(list)
        for r in rows:
            if r.get(key) is not None:
                by_year[int(r["date"][:4])].append(r[key])
        vals = [v for vs in by_year.values() for v in vs]
        lo, hi = cluster_ci(by_year)
        return {"n": len(vals), "mean_rank": round(mean(vals), 3), "ci95": [round(lo, 3), round(hi, 3)]}

    res["across_years"] = {f"{w}d": summarise(f"r{w}") for w in WINDOWS}
    res["across_years"]["days 11-60 only"] = summarise("r11_60")
    res["within_season_60d"] = summarise("r60_within")
    for k, v in res["across_years"].items():
        log(f"  across years, {k}: mean rank {v['mean_rank']:.3f} (95% CI {v['ci95'][0]:.3f}-{v['ci95'][1]:.3f}), n={v['n']}")
    log(f"  within season, 60d: {res['within_season_60d']}")

    # quintiles and thirds of the 60-day rank
    r60 = [r["r60"] for r in rows]
    def shares(parts):
        counts = [0] * parts
        for v in r60:
            counts[min(int(v * parts), parts - 1)] += 1
        return [round(c / len(r60), 3) for c in counts]
    res["quintiles_60d"] = shares(5)
    res["thirds_60d"] = shares(3)
    ratio = [r["mm60"] / r["normal60"] for r in rows if r["normal60"] > 0]
    res["mm60_vs_normal"] = {"median_ratio": round(statistics.median(ratio), 3),
                             "p25": round(pct(ratio, 0.25), 3), "p75": round(pct(ratio, 0.75), 3)}
    mm = [r["mm60"] for r in rows]
    res["mm60_at_finds"] = {str(q): round(pct(mm, q), 1) for q in (0.05, 0.1, 0.25, 0.5, 0.75, 0.9)}

    # ---- years: finds per 10,000 fungus records against regional rain
    camp = gbif_year_counts(TAXON)
    fungi = gbif_year_counts(FUNGI)
    doys = sorted(date.fromisoformat(r["date"]).timetuple().tm_yday for r in rows)
    mid = doys[len(doys) // 2]
    yr_rows = []
    for y in range(FIRST_YEAR, 2026):
        end = date(y, 1, 1) + timedelta(days=mid - 1)
        vals = [series[st["id"]].total(end, MAIN) for st in stations]
        vals = [v for v in vals if v is not None]
        if len(vals) < 5 or not fungi.get(y):
            continue
        yr_rows.append({"year": y, "campestris": camp.get(y, 0), "fungi": fungi[y],
                        "per_10000": round(10000 * camp.get(y, 0) / fungi[y], 2),
                        "rain60_mm": round(mean(vals), 1), "stations": len(vals)})
    if len(yr_rows) >= 8:
        x = [r["rain60_mm"] for r in yr_rows]
        y = [r["per_10000"] for r in yr_rows]
        res["years"] = {"window_end": (date(2025, 1, 1) + timedelta(days=mid - 1)).strftime("%d %b"),
                        "spearman": round(spearman(x, y), 3), "p": round(perm_p(x, y), 4), "rows": yr_rows}
        log(f"Years: Spearman {res['years']['spearman']} (p {res['years']['p']}) over {len(yr_rows)} years, "
            f"60 days ending {res['years']['window_end']}")

    (out_dir / "results.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    with open(out_dir / "finds.tsv", "w", encoding="utf-8") as fh:
        keys = list(rows[0].keys())
        fh.write("\t".join(keys) + "\n")
        for r in rows:
            fh.write("\t".join("" if r[k] is None else (f"{r[k]:.3f}" if isinstance(r[k], float) and k.startswith("r")
                                                        else str(r[k])) for k in keys) + "\n")
    log("done")


if __name__ == "__main__":
    main()
