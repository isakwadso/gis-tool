"""Fetch weather for every pasture grid cell and compute daily suitability scores.

Reads  site/data/cells.json (from build_pasture.py) and config.json
Writes site/data/scores.json (published with the site, not committed)

Weather: Open-Meteo forecast API (past days + forecast, daily values, local days
in Europe/Stockholm). Data licence CC BY 4.0, https://open-meteo.com/.
Free tier limits: 600 calls/min, 5,000/h, 10,000/day; every location counts as
at least one call, so locations are sent in batches with a pause in between.

Run: python scripts/build_scores.py
Uses only the Python standard library.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model import history_days, score_day  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.open-meteo.com/v1/forecast"
DAILY_VARS = ["precipitation_sum", "temperature_2m_mean", "temperature_2m_min", "et0_fao_evapotranspiration"]
USER_AGENT = "gis-tool mushroom map (https://github.com/isakwadso/gis-tool)"
MAX_FAILED_SHARE = 0.10
CALLS_PER_MINUTE = 500  # Open-Meteo free tier allows 600; keep a margin

Fetcher = Callable[[list[tuple[float, float]], dict], list[dict]]


def fetch_open_meteo(points: list[tuple[float, float]], params: dict) -> list[dict]:
    """One request for many locations. Returns one dict per location, same order."""
    q = {
        "latitude": ",".join(f"{lat:.4f}" for lat, _ in points),
        "longitude": ",".join(f"{lon:.4f}" for _, lon in points),
        "daily": ",".join(DAILY_VARS),
        "timezone": params["timezone"],
        "past_days": params["past_days"],
        "forecast_days": params["forecast_days"],
    }
    url = API + "?" + urllib.parse.urlencode(q)
    waits = [65, 120, 240, 300]
    for attempt in range(len(waits) + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, list) else [data]
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            retryable = exc.code == 429 or exc.code >= 500
            if not retryable or attempt == len(waits):
                raise RuntimeError(f"Open-Meteo HTTP {exc.code}: {body}") from exc
            print(f"  Open-Meteo HTTP {exc.code} ({body}); waiting {waits[attempt]}s", flush=True)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == len(waits):
                raise RuntimeError(f"Open-Meteo request failed: {exc}") from exc
            print(f"  Open-Meteo request failed ({exc}); waiting {waits[attempt]}s", flush=True)
        time.sleep(waits[attempt])
    raise AssertionError("unreachable")


def target_dates(today: date, days_ahead: int) -> list[date]:
    return [today + timedelta(days=k) for k in range(days_ahead + 1)]


def call_weight(locations: int, days: int, variables: int) -> float:
    """Open-Meteo counts a location as more than one call beyond 14 days or 10 variables."""
    return locations * max(1.0, days / 14) * max(1.0, variables / 10)


def build(cfg: dict, cells_meta: dict | None, now: datetime, fetch: Fetcher,
          pause_s: float | None = None) -> dict:
    model = cfg["model"]
    fc = cfg["forecast"]
    tz = fc["timezone"]
    today = now.date()
    dates = target_dates(today, fc["days_ahead"])
    in_season = [d.month in model["season_months"] for d in dates]
    out = {
        "generated": now.isoformat(timespec="minutes"),
        "today": today.isoformat(),
        "timezone": tz,
        "dates": [d.isoformat() for d in dates],
        "inSeason": in_season,
        "model": model,
        "source": {"name": "Open-Meteo", "url": "https://open-meteo.com/", "licence": "CC BY 4.0"},
        "cells": {},
    }

    if not cells_meta or not cells_meta.get("cells"):
        out["status"] = "no-pasture-layer"
        return out
    out["pastureVersion"] = cells_meta.get("version")
    if not any(in_season):
        out["status"] = "out-of-season"
        return out

    n = history_days(model)
    params = {"timezone": tz, "past_days": n - 1, "forecast_days": fc["days_ahead"] + 1}
    rows = cells_meta["cells"]
    size = fc["batch_size"]
    days_per_location = params["past_days"] + params["forecast_days"]
    batch_calls = call_weight(size, days_per_location, len(DAILY_VARS))
    if pause_s is None:
        # Space batches so a burst never exceeds the per-minute limit.
        pause = max(fc["pause_between_batches_s"], 60 * batch_calls / CALLS_PER_MINUTE)
    else:
        pause = pause_s
    out["calls"] = round(call_weight(len(rows), days_per_location, len(DAILY_VARS)))
    failed = 0

    for b in range(0, len(rows), size):
        batch = rows[b:b + size]
        if b:
            time.sleep(pause)
        print(f"  weather batch {b // size + 1}: {len(batch)} cells", flush=True)
        results = fetch([(r[1], r[2]) for r in batch], params)
        if len(results) != len(batch):
            raise RuntimeError(f"Expected {len(batch)} locations, got {len(results)}")
        for row, res in zip(batch, results):
            daily = res.get("daily") or {}
            by_date = {}
            for k, d in enumerate(daily.get("time", [])):
                by_date[d] = tuple(daily.get(v, [None] * (k + 1))[k] for v in DAILY_VARS)
            s_list, r_list, t_list, f_list, d_list = [], [], [], [], []
            empty = (None,) * len(DAILY_VARS)
            for d, season in zip(dates, in_season):
                window = [(d - timedelta(days=n - 1 - k)).isoformat() for k in range(n)]
                res_day = score_day([by_date.get(w, empty) for w in window], model)
                s_list.append(round(res_day.score * 100) if (season and res_day.score is not None) else None)
                r_list.append(None if res_day.rain_mm is None else round(res_day.rain_mm, 1))
                t_list.append(None if res_day.tmean_c is None else round(res_day.tmean_c, 1))
                f_list.append(res_day.frost_nights)
                d_list.append(None if res_day.drying_loss is None else round(res_day.drying_loss * 100))
            if all(v is None for v in r_list):
                failed += 1
            # Per day: score %, weighted rain mm, mean temperature °C, frost nights, drying loss %
            out["cells"][row[0]] = [s_list, r_list, t_list, f_list, d_list]

    out["failedCells"] = failed
    if failed > MAX_FAILED_SHARE * len(rows):
        raise RuntimeError(f"Weather missing for {failed} of {len(rows)} cells")
    out["status"] = "ok"
    return out


def summarize(out: dict) -> str:
    if out.get("status") != "ok":
        return f"status: {out.get('status')}"
    lines = [f"status ok, {len(out['cells'])} cells, {out.get('failedCells', 0)} without weather, "
             f"~{out.get('calls')} Open-Meteo calls"]
    for k, d in enumerate(out["dates"]):
        scores = [c[0][k] for c in out["cells"].values() if c[0][k] is not None]
        if scores:
            good = sum(1 for s in scores if s >= 50)
            lines.append(f"  {d}: max {max(scores)}%, {good} cells >= 50%")
        else:
            lines.append(f"  {d}: no scores (out of season)")
    return "\n".join(lines)


def main() -> int:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    data_dir = ROOT / "site" / "data"
    cells_path = data_dir / "cells.json"
    cells_meta = json.loads(cells_path.read_text(encoding="utf-8")) if cells_path.exists() else None
    now = datetime.now(ZoneInfo(cfg["forecast"]["timezone"]))
    out = build(cfg, cells_meta, now, fetch_open_meteo)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "scores.json").write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False),
                                          encoding="utf-8")
    print(summarize(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
