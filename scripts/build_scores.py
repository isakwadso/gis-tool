"""Fetch weather for every pasture grid cell, compute daily suitability scores and
the rain total over the last 60 days.

Reads  site/data/cells.json (from build_pasture.py), config.json and the previous
       rain history (site/data/rain_history.json on the live site)
Writes site/data/scores.json and site/data/rain_history.json (published with the
       site, not committed)

Weather: Open-Meteo forecast API (past days + forecast, daily values, local days
in Europe/Stockholm). Data licence CC BY 4.0, https://open-meteo.com/.
Free tier limits: 600 calls/min, 5,000/h, 10,000/day; every location counts as
at least one call, so locations are sent in batches with a pause in between.

Rain history: every run gets the last 9 days anyway, so their rain is kept in a
small file published with the site and read back by the next run. Only days the
history lacks (the first run, a new grid square, or an outage longer than 9 days)
are fetched again, by asking for more past days for those squares.

Run: python scripts/build_scores.py   (PAGES_URL=<site url> to read the live history)
Uses only the Python standard library.
"""

from __future__ import annotations

import http.client
import json
import os
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
MAX_PAST_DAYS = 92      # Open-Meteo forecast API limit
HISTORY_FILE = "rain_history.json"

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
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
            if attempt == len(waits):
                raise RuntimeError(f"Open-Meteo request failed: {exc}") from exc
            print(f"  Open-Meteo request failed ({exc}); waiting {waits[attempt]}s", flush=True)
        time.sleep(waits[attempt])
    raise AssertionError("unreachable")


def target_dates(today: date, days_ahead: int) -> list[date]:
    return [today + timedelta(days=k) for k in range(days_ahead + 1)]


def uncertainty_factor(model: dict, days_ahead: int) -> float:
    """Optional discount for days further ahead: keep_per_day ** days_ahead.
    keep_per_day = 1.0 (the default) means forecast days are not lowered."""
    keep = float(model.get("forecast_uncertainty", {}).get("keep_per_day", 1.0))
    return keep ** max(0, days_ahead)


def call_weight(locations: int, days: int, variables: int) -> float:
    """Open-Meteo counts a location as more than one call beyond 14 days or 10 variables."""
    return locations * max(1.0, days / 14) * max(1.0, variables / 10)


# ---------------------------------------------------------------- rain history

def history_from_json(data: dict | None) -> dict[str, dict[str, float]]:
    """{cell id: {ISO date: rain mm}} from a published history file; {} if unusable."""
    if not isinstance(data, dict) or data.get("version") != 1:
        return {}
    try:
        start = date.fromisoformat(data["start"])
        return {cid: {(start + timedelta(days=i)).isoformat(): float(v) for i, v in enumerate(vals) if v is not None}
                for cid, vals in data["cells"].items()}
    except (KeyError, TypeError, ValueError, AttributeError):
        return {}


def history_to_json(hist: dict[str, dict[str, float]], today: date, keep_days: int,
                    now: datetime, tz: str) -> dict:
    """The last `keep_days` complete days (up to yesterday), one list per cell."""
    start = today - timedelta(days=keep_days)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(keep_days)]
    return {
        "version": 1,
        "about": "Daily rain (mm) per weather grid square, local days. Read back by the next "
                 "run so only new days are fetched. Open-Meteo.com, CC BY 4.0.",
        "variable": "precipitation_sum", "unit": "mm", "timezone": tz,
        "updated": now.isoformat(timespec="minutes"),
        "start": start.isoformat(),
        "cells": {cid: [hist[cid].get(d) for d in dates] for cid in sorted(hist)},
    }


def load_history(url: str | None, local: Path) -> tuple[dict | None, str]:
    """The previous history: from the live site when `url` is given, else a local file.
    (None, ...) when there is none yet. Raises if the live site cannot be read, so a
    network hiccup never replaces a good history with an empty one."""
    if not url:
        if local.exists():
            return json.loads(local.read_text(encoding="utf-8")), f"local file {local}"
        return None, "none (no PAGES_URL, no local file)"
    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"{url}?t={int(time.time())}", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
            try:
                return json.loads(raw), f"live site ({len(raw) // 1024} kB)"
            except json.JSONDecodeError:
                return None, "live site file unreadable; starting again"
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None, "none on the live site yet"
            last = exc
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
            last = exc
        time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Could not read the rain history from {url}: {last}")


def should_skip(event: str, cfg: dict, today: date, history: dict | None) -> bool:
    """Outside the season the map only needs fresh rain once a day: a scheduled run
    skips when no day on the slider is in season and the history is from today."""
    if event != "schedule" or not history:
        return False
    season = cfg["model"]["season_months"]
    if any(d.month in season for d in target_dates(today, cfg["forecast"]["days_ahead"])):
        return False
    return str(history.get("updated", ""))[:10] == today.isoformat()


# ---------------------------------------------------------------- the job

def build(cfg: dict, cells_meta: dict | None, now: datetime, fetch: Fetcher,
          pause_s: float | None = None, history: dict[str, dict[str, float]] | None = None,
          ) -> tuple[dict, dict | None]:
    """Returns (scores.json content, new rain history file content or None)."""
    model = cfg["model"]
    fc = cfg["forecast"]
    rt = cfg.get("rain_total")
    tz = fc["timezone"]
    today = now.date()
    dates = target_dates(today, fc["days_ahead"])
    in_season = [d.month in model["season_months"] for d in dates]
    out = {
        "generated": now.isoformat(timespec="minutes"),
        "today": today.isoformat(),
        "timezone": tz,
        "dates": [d.isoformat() for d in dates],
        # Multiplier per day for forecast uncertainty: keep_per_day ** days ahead (1.0 = no decline)
        "uncertainty": [round(uncertainty_factor(model, k), 4) for k in range(len(dates))],
        "inSeason": in_season,
        "model": model,
        "source": {"name": "Open-Meteo", "url": "https://open-meteo.com/", "licence": "CC BY 4.0"},
        "cells": {},
    }
    if rt:
        out["rainTotal"] = {k: rt.get(k) for k in ("days", "classes_mm", "classes_note", "normal_mm", "normal_source")}

    if not cells_meta or not cells_meta.get("cells"):
        out["status"] = "no-pasture-layer"
        return out, None
    out["pastureVersion"] = cells_meta.get("version")
    if not any(in_season) and not rt:
        out["status"] = "out-of-season"
        return out, None

    hist = {cid: dict(v) for cid, v in (history or {}).items()}
    n = history_days(model)
    fdays = fc["days_ahead"] + 1
    normal_past = n - 1
    rows = cells_meta["cells"]

    # Squares whose history lacks a day that today's rain total needs (older than the
    # days every run fetches anyway) get more past days in the same request.
    groups = [(rows, normal_past)]
    backfill_info = None
    if rt:
        first_needed = today - timedelta(days=rt["days"] - 1)
        gap = [(first_needed + timedelta(days=i)).isoformat()
               for i in range((today - timedelta(days=normal_past) - first_needed).days)]
        oldest_missing = {}
        for row in rows:
            missing = [d for d in gap if hist.get(row[0], {}).get(d) is None]
            if missing:
                oldest_missing[row[0]] = missing[0]
        if oldest_missing:
            regular = [r for r in rows if r[0] not in oldest_missing]
            backfill = [r for r in rows if r[0] in oldest_missing]
            past = (today - date.fromisoformat(min(oldest_missing.values()))).days
            # Safety cap on the extra calls (the first run needs ~2,300 for 467 squares)
            budget = rt.get("max_backfill_calls", 3000)
            allowed_days = int(14 * budget / len(backfill)) + normal_past + fdays
            capped = min(past, allowed_days - fdays, MAX_PAST_DAYS)
            if capped < past:
                print(f"  back-fill capped at {capped} past days (wanted {past})", flush=True)
            past = max(capped, normal_past)
            groups = [(regular, normal_past), (backfill, past)]
            extra = call_weight(len(backfill), past + fdays, len(DAILY_VARS)) - \
                call_weight(len(backfill), normal_past + fdays, len(DAILY_VARS))
            backfill_info = {"cells": len(backfill), "past_days": past, "extra_calls": round(extra)}
            out["backfill"] = backfill_info

    out["calls"] = round(sum(call_weight(len(g), p + fdays, len(DAILY_VARS)) for g, p in groups if g))
    failed = 0
    size = fc["batch_size"]
    first_batch = True

    for group, past_days in groups:
        if not group:
            continue
        params = {"timezone": tz, "past_days": past_days, "forecast_days": fdays}
        batch_calls = call_weight(min(size, len(group)), past_days + fdays, len(DAILY_VARS))
        # Space batches so a burst never exceeds the per-minute limit.
        pause = pause_s if pause_s is not None else max(fc["pause_between_batches_s"],
                                                        60 * batch_calls / CALLS_PER_MINUTE)
        for b in range(0, len(group), size):
            batch = group[b:b + size]
            if not first_batch:
                time.sleep(pause)
            first_batch = False
            print(f"  weather batch: {len(batch)} cells, {past_days} past days", flush=True)
            results = fetch([(r[1], r[2]) for r in batch], params)
            if len(results) != len(batch):
                raise RuntimeError(f"Expected {len(batch)} locations, got {len(results)}")
            for row, res in zip(batch, results):
                cell = score_cell(row[0], res, dates, in_season, out["uncertainty"], model, n, rt, hist, today)
                if all(v is None for v in cell[1]):
                    failed += 1
                out["cells"][row[0]] = cell

    out["failedCells"] = failed
    if failed > MAX_FAILED_SHARE * len(rows):
        raise RuntimeError(f"Weather missing for {failed} of {len(rows)} cells")
    out["status"] = "ok"
    if not rt:
        return out, None
    current = {r[0] for r in rows}
    keep = rt.get("keep_days", rt["days"] + 15)
    oldest = (today - timedelta(days=keep)).isoformat()
    hist = {cid: {d: v for d, v in vals.items() if d >= oldest} for cid, vals in hist.items() if cid in current}
    return out, history_to_json(hist, today, keep, now, tz)


def score_cell(cid, res, dates, in_season, uncertainty, model, n, rt, hist, today) -> list:
    """Per day: score %, weighted rain mm, mean temperature °C, frost nights, drying loss %,
    and (with rain_total) the rain total mm and how much of it is forecast (today on)."""
    daily = res.get("daily") or {}
    by_date = {}
    for k, d in enumerate(daily.get("time", [])):
        by_date[d] = tuple(daily.get(v, [None] * (k + 1))[k] for v in DAILY_VARS)
    today_iso = today.isoformat()
    if rt is not None:
        h = hist.setdefault(cid, {})
        for d, vals in by_date.items():
            if d < today_iso and vals[0] is not None:
                h[d] = round(float(vals[0]), 1)

    s_list, r_list, t_list, f_list, d_list, a_list, af_list = [], [], [], [], [], [], []
    empty = (None,) * len(DAILY_VARS)
    for ahead, (d, season) in enumerate(zip(dates, in_season)):
        window = [(d - timedelta(days=n - 1 - k)).isoformat() for k in range(n)]
        res_day = score_day([by_date.get(w, empty) for w in window], model)
        score = None
        if season and res_day.score is not None:
            score = round(res_day.score * uncertainty[ahead] * 100)
        s_list.append(score)
        r_list.append(None if res_day.rain_mm is None else round(res_day.rain_mm, 1))
        t_list.append(None if res_day.tmean_c is None else round(res_day.tmean_c, 1))
        f_list.append(res_day.frost_nights)
        d_list.append(None if res_day.drying_loss is None else round(res_day.drying_loss * 100))
        if rt is not None:
            total, fcst = rain_total(hist.get(cid, {}), by_date, d, rt["days"], today_iso)
            a_list.append(total)
            af_list.append(fcst)
    cell = [s_list, r_list, t_list, f_list, d_list]
    if rt is not None:
        cell += [a_list, af_list]
    return cell


def rain_total(hist_c: dict[str, float], by_date: dict, end: date, days: int,
               today_iso: str) -> tuple[int | None, int | None]:
    """Rain over the `days` days ending on `end` (inclusive): past days from the history,
    today and later from the forecast. (None, None) if any day is missing."""
    total = fcst = 0.0
    for i in range(days):
        d = (end - timedelta(days=i)).isoformat()
        if d < today_iso:
            v = hist_c.get(d)
        else:
            v = (by_date.get(d) or (None,))[0]
            if v is not None:
                fcst += v
        if v is None:
            return None, None
        total += v
    return round(total), round(fcst)


def summarize(out: dict) -> str:
    if out.get("status") != "ok":
        return f"status: {out.get('status')}"
    lines = [f"status ok, {len(out['cells'])} cells, {out.get('failedCells', 0)} without weather, "
             f"~{out.get('calls')} Open-Meteo calls"]
    if out.get("backfill"):
        b = out["backfill"]
        lines.append(f"  rain history back-fill: {b['cells']} cells, {b['past_days']} past days, "
                     f"~{b['extra_calls']} extra calls")
    for k, d in enumerate(out["dates"]):
        scores = [c[0][k] for c in out["cells"].values() if c[0][k] is not None]
        totals = sorted(c[5][k] for c in out["cells"].values() if len(c) > 5 and c[5][k] is not None)
        rain = (f"; rain {out['rainTotal']['days']} d: {totals[0]}-{totals[len(totals) // 2]}-{totals[-1]} mm "
                f"(min-median-max, {len(totals)} cells)") if totals else ""
        if scores:
            good = sum(1 for s in scores if s >= 50)
            lines.append(f"  {d}: max {max(scores)}%, {good} cells >= 50%{rain}")
        else:
            lines.append(f"  {d}: no scores (out of season){rain}")
    return "\n".join(lines)


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def main() -> int:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    data_dir = ROOT / "site" / "data"
    cells_path = data_dir / "cells.json"
    cells_meta = json.loads(cells_path.read_text(encoding="utf-8")) if cells_path.exists() else None
    now = datetime.now(ZoneInfo(cfg["forecast"]["timezone"]))

    pages = os.environ.get("PAGES_URL", "").rstrip("/")
    hist_json, where = load_history(f"{pages}/data/{HISTORY_FILE}" if pages else None, data_dir / HISTORY_FILE)
    print(f"Rain history: {where}", flush=True)
    if should_skip(os.environ.get("GITHUB_EVENT_NAME", ""), cfg, now.date(), hist_json):
        print("Out of season and the rain history is already from today: nothing to publish.")
        set_output("publish", "false")
        return 0

    out, new_hist = build(cfg, cells_meta, now, fetch_open_meteo, history=history_from_json(hist_json))
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "scores.json").write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False),
                                          encoding="utf-8")
    if new_hist:
        (data_dir / HISTORY_FILE).write_text(json.dumps(new_hist, separators=(",", ":")), encoding="utf-8")
    print(summarize(out))
    set_output("publish", "true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
