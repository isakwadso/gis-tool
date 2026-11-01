"""Tests for the scoring model and the daily job. Run: python -m unittest discover -s scripts"""

import copy
import json
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_scores  # noqa: E402
from model import (drying_factor, history_days, rain_factor, rain_weights,  # noqa: E402
                   score_day, temp_factor)

CONFIG = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
# Fixed settings, so the tests keep checking the logic when config.json is tuned.
MODEL = {
    "season_months": [7, 8, 9, 10, 11],
    "rain": {"days": 10, "peak_days_back": 2, "fade_per_day": 0.2, "mm": [5, 50]},
    "temperature": {"days": 5, "c": [0, 5, 12, 15]},
    "drying": {"days": 5, "et0_mm": [1.0, 3.5], "max_loss": 0.3, "day_weights": [1, 1, 0.75, 0.5, 0.25]},
    "frost": {"days": 5, "below_c": 0.0, "keep_per_night": 0.75},
    "forecast_uncertainty": {"keep_per_day": 1.0},
}
RAIN_TOTAL = {"days": 60, "keep_days": 75, "max_backfill_calls": 3000, "classes_mm": [60, 90, 120, 160, 200]}
# Scores only (no rain history), and scores plus the 60-day rain total
CFG = {**{k: v for k, v in CONFIG.items() if k != "rain_total"}, "model": MODEL}
CFG_RAIN = {**CFG, "rain_total": RAIN_TOTAL}


def days(precip=10.0, tmean=9.0, tmin=4.0, et0=0.5, n=10):
    """History of n identical days, oldest first."""
    return [(precip, tmean, tmin, et0)] * n


class ConfigTests(unittest.TestCase):
    def test_config_is_sane(self):
        m = CONFIG["model"]
        self.assertEqual(set(m), set(MODEL))
        for part in ("rain", "temperature", "drying", "frost", "forecast_uncertainty"):
            self.assertEqual(set(m[part]), set(MODEL[part]), part)
        self.assertLess(m["rain"]["mm"][0], m["rain"]["mm"][1])
        self.assertLess(m["rain"]["peak_days_back"], m["rain"]["days"])
        self.assertEqual(sorted(m["temperature"]["c"]), m["temperature"]["c"])
        self.assertLess(m["drying"]["et0_mm"][0], m["drying"]["et0_mm"][1])
        self.assertEqual(len(m["drying"]["day_weights"]), m["drying"]["days"])
        self.assertTrue(0 < m["frost"]["keep_per_night"] <= 1)
        self.assertTrue(0 < m["forecast_uncertainty"]["keep_per_day"] <= 1)
        self.assertTrue(set(m["season_months"]) <= set(range(1, 13)))
        self.assertGreaterEqual(CONFIG["pasture"]["min_years"], 1)

    def test_rain_total_config(self):
        rt = CONFIG["rain_total"]
        self.assertEqual(rt["classes_mm"], sorted(rt["classes_mm"]))
        self.assertGreater(rt["keep_days"], rt["days"])
        self.assertLessEqual(rt["days"] - 1, build_scores.MAX_PAST_DAYS)

    def test_open_meteo_budget(self):
        # First run back-fills 60 days for every square; it must fit the safety cap, and
        # that day's total (back-fill + 7 ordinary runs) must stay under 10,000 calls.
        cells = json.loads((Path(__file__).resolve().parent.parent / "site/data/cells.json").read_text())["cells"]
        fdays = CONFIG["forecast"]["days_ahead"] + 1
        past = CONFIG["rain_total"]["days"] - 1
        first = build_scores.call_weight(len(cells), past + fdays, 4)
        ordinary = build_scores.call_weight(len(cells), history_days(CONFIG["model"]) - 1 + fdays, 4)
        self.assertLess(first - ordinary, CONFIG["rain_total"]["max_backfill_calls"])
        self.assertLess(first + 7 * ordinary, 10000)


class RainTests(unittest.TestCase):
    def test_weights_sum_to_five_and_peak_two_days_back(self):
        w = rain_weights(10, 2, 0.2)
        self.assertEqual(len(w), 10)
        self.assertAlmostEqual(sum(w), 5.0)
        self.assertEqual(w.index(max(w)), 2)
        self.assertAlmostEqual(w[0] / w[2], 1 / 3)
        self.assertAlmostEqual(w[1] / w[2], 2 / 3)
        self.assertAlmostEqual(w[3] / w[2], 0.8)
        self.assertAlmostEqual(w[9] / w[2], 0.8 ** 7)

    def test_no_delay(self):
        w = rain_weights(10, 0, 0.2)
        self.assertEqual(w.index(max(w)), 0)
        self.assertAlmostEqual(sum(w), 5.0)

    def test_rain_scale(self):
        self.assertEqual(rain_factor(5, [5, 50]), 0)
        self.assertAlmostEqual(rain_factor(27.5, [5, 50]), 0.5)
        self.assertEqual(rain_factor(80, [5, 50]), 1)

    def test_steady_rain_equals_five_day_total(self):
        r = score_day(days(precip=10.0), MODEL)
        self.assertAlmostEqual(r.rain_mm, 50.0)
        self.assertAlmostEqual(r.score, 1.0)

    def test_rain_two_days_ago_counts_most(self):
        def one_shower(days_back):
            h = days(precip=0.0)
            h[-1 - days_back] = (30.0, 9.0, 4.0, 0.5)
            return score_day(h, MODEL).rain_mm
        self.assertGreater(one_shower(2), one_shower(0))
        self.assertGreater(one_shower(2), one_shower(5))
        self.assertGreater(one_shower(5), one_shower(9))


class TemperatureTests(unittest.TestCase):
    def test_temperature_scale(self):
        c = MODEL["temperature"]["c"]
        self.assertEqual(temp_factor(0, c), 0)
        self.assertAlmostEqual(temp_factor(2.5, c), 0.5)
        self.assertEqual(temp_factor(9, c), 1)
        self.assertAlmostEqual(temp_factor(13.5, c), 0.5)
        self.assertEqual(temp_factor(16, c), 0)

    def test_only_last_five_days_count(self):
        h = days(tmean=9.0)
        for i in range(5):           # five old days far too warm
            h[i] = (10.0, 25.0, 4.0, 0.5)
        self.assertAlmostEqual(score_day(h, MODEL).tmean_c, 9.0)


class DryingTests(unittest.TestCase):
    def test_dull_days_cost_nothing(self):
        self.assertEqual(drying_factor([0.8] * 5, MODEL["drying"]), 1.0)

    def test_strong_drying_today(self):
        self.assertAlmostEqual(drying_factor([4.0, 0, 0, 0, 0], MODEL["drying"]), 0.7)

    def test_older_days_count_less(self):
        today = drying_factor([4.0, 0, 0, 0, 0], MODEL["drying"])
        old = drying_factor([0, 0, 0, 0, 4.0], MODEL["drying"])
        self.assertAlmostEqual(old, 1 - 0.3 * 0.25)
        self.assertGreater(old, today)

    def test_partial_drying(self):
        # ET0 2.25 mm is halfway between 1.0 and 3.5 -> loses half of 30 %
        self.assertAlmostEqual(drying_factor([2.25, 0, 0, 0, 0], MODEL["drying"]), 0.85)

    def test_drying_multiplies_score(self):
        h = days(precip=10.0)
        h[-1] = (10.0, 9.0, 4.0, 4.0)
        r = score_day(h, MODEL)
        self.assertAlmostEqual(r.score, 0.7)
        self.assertAlmostEqual(r.drying_loss, 0.3)


class FrostTests(unittest.TestCase):
    def test_frost_multiplies(self):
        h = days(precip=10.0)
        h[-1] = (10.0, 9.0, -1.0, 0.5)
        h[-3] = (10.0, 9.0, -2.0, 0.5)
        r = score_day(h, MODEL)
        self.assertEqual(r.frost_nights, 2)
        self.assertAlmostEqual(r.score, 0.75 ** 2)

    def test_old_frost_ignored(self):
        h = days(precip=10.0)
        h[0] = (10.0, 9.0, -3.0, 0.5)  # 9 days back, outside the 5-day frost window
        self.assertEqual(score_day(h, MODEL).frost_nights, 0)


class ScoreDayTests(unittest.TestCase):
    def test_missing_value_gives_none(self):
        h = days()
        h[-2] = (None, 9.0, 4.0, 0.5)
        self.assertIsNone(score_day(h, MODEL).score)

    def test_needs_full_history(self):
        with self.assertRaises(ValueError):
            score_day(days(n=9), MODEL)

    def test_history_days(self):
        self.assertEqual(history_days(MODEL), 10)


def fake_open_meteo(precip=10.0, tmean=8.0, tmin=3.0, et0=0.5, missing_last=False):
    """Stand-in for Open-Meteo returning the same daily values everywhere."""
    def fetch(points, params):
        today = fetch.today
        span = params["past_days"] + params["forecast_days"]
        first = today - timedelta(days=params["past_days"])
        times = [(first + timedelta(days=k)).isoformat() for k in range(span)]
        fetch.calls.append((len(points), params))
        out = []
        for lat, lon in points:
            p = [precip] * span
            if missing_last:
                p[-1] = None
            out.append({"latitude": lat, "longitude": lon, "daily": {
                "time": times, "precipitation_sum": p, "temperature_2m_mean": [tmean] * span,
                "temperature_2m_min": [tmin] * span, "et0_fao_evapotranspiration": [et0] * span}})
        return out
    fetch.calls = []
    return fetch


CELLS = {"version": "test", "cells": [["7_9", 55.68, 13.17, 6.9, 8], ["7_10", 55.68, 13.24, 14.5, 6],
                                      ["8_9", 55.70, 13.14, 13.4, 7]]}


class JobTests(unittest.TestCase):
    def run_job(self, day, fetch, cells=CELLS, cfg=CFG, history=None):
        now = datetime.combine(day, datetime.min.time()).replace(hour=5, tzinfo=ZoneInfo("Europe/Stockholm"))
        fetch.today = day
        out, self.history = build_scores.build(cfg, cells, now, fetch, pause_s=0, history=history)
        return out

    def test_in_season(self):
        fetch = fake_open_meteo()
        out = self.run_job(date(2026, 9, 10), fetch)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(len(out["dates"]), CFG["forecast"]["days_ahead"] + 1)
        self.assertEqual(len(out["cells"]["7_9"]), 5)
        s, r, t, f, d = out["cells"]["7_9"]
        self.assertEqual(s, [100] * 11)
        self.assertEqual(r[0], 50.0)
        self.assertEqual(t[0], 8.0)
        self.assertEqual(f[0], 0)
        self.assertEqual(d[0], 0)
        self.assertEqual(fetch.calls[0][1]["past_days"], 9)
        self.assertEqual(fetch.calls[0][1]["forecast_days"], 11)

    def test_drying_and_rain_reported(self):
        out = self.run_job(date(2026, 8, 10), fake_open_meteo(precip=5.0, et0=3.5))
        s, r, t, f, d = out["cells"]["7_9"]
        self.assertEqual(r[0], 25.0)
        expected_loss = 1 - (0.7 * 0.7 * (1 - 0.3 * 0.75) * (1 - 0.3 * 0.5) * (1 - 0.3 * 0.25))
        self.assertEqual(d[0], round(expected_loss * 100))
        self.assertEqual(s[0], round((20 / 45) * (1 - expected_loss) * 100))

    def test_no_uncertainty_decline_by_default(self):
        out = self.run_job(date(2026, 9, 10), fake_open_meteo())
        self.assertEqual(out["uncertainty"], [1.0] * 11)
        self.assertEqual(out["cells"]["7_9"][0], [100] * 11)

    def test_uncertainty_decline_when_switched_on(self):
        cfg = copy.deepcopy(CFG)
        cfg["model"]["forecast_uncertainty"]["keep_per_day"] = 0.9
        out = self.run_job(date(2026, 9, 10), fake_open_meteo(), cfg=cfg)
        s = out["cells"]["7_9"][0]
        self.assertEqual(s[0], 100)                       # today is not lowered
        self.assertEqual(s[1], 90)
        self.assertEqual(s[10], round(100 * 0.9 ** 10))  # 35
        self.assertAlmostEqual(out["uncertainty"][5], 0.9 ** 5, places=4)
        # weather details are not discounted, only the score
        self.assertEqual(out["cells"]["7_9"][1][10], 50.0)

    def test_season_edge(self):
        out = self.run_job(date(2026, 11, 25), fake_open_meteo())
        s, r, _, _, _ = out["cells"]["7_9"]
        self.assertEqual(out["inSeason"], [True] * 6 + [False] * 5)
        self.assertEqual(s[:6], [100] * 6)
        self.assertEqual(s[6:], [None] * 5)
        self.assertEqual(r[6], 50.0)

    def test_out_of_season_skips_api(self):
        def never(points, params):
            raise AssertionError("API should not be called out of season")
        out = self.run_job(date(2026, 1, 15), never)
        self.assertEqual(out["status"], "out-of-season")
        self.assertIsNone(self.history)

    def test_no_pasture_layer(self):
        out = self.run_job(date(2026, 9, 10), fake_open_meteo(), cells=None)
        self.assertEqual(out["status"], "no-pasture-layer")

    def test_missing_last_day(self):
        out = self.run_job(date(2026, 9, 10), fake_open_meteo(missing_last=True))
        s = out["cells"]["7_9"][0]
        self.assertIsNone(s[-1])
        self.assertEqual(s[0], 100)

    def test_batching_and_call_count(self):
        fetch = fake_open_meteo()
        many = {"version": "t", "cells": [[f"{i}_0", 55.5, 13.0, 1.0, 1] for i in range(250)]}
        out = self.run_job(date(2026, 8, 1), fetch, cells=many)
        self.assertEqual([c[0] for c in fetch.calls], [100, 100, 50])
        self.assertEqual(len(out["cells"]), 250)
        self.assertEqual(out["cells"]["249_0"][0][0], 100)
        self.assertEqual(out["calls"], round(250 * 20 / 14))

    def test_burst_pause_respects_minute_limit(self):
        # 100 locations x 20 days = 143 calls per batch; at 500/min that needs >= 17 s
        weight = build_scores.call_weight(100, 20, 4)
        self.assertAlmostEqual(weight, 100 * 20 / 14)
        self.assertGreaterEqual(CONFIG["forecast"]["pause_between_batches_s"], 60 * weight / 600)


def history_through(day, cells=("7_9", "7_10", "8_9"), mm=10.0, days=75):
    """A history with `days` complete days ending the day before `day`."""
    return {cid: {(day - timedelta(days=i)).isoformat(): mm for i in range(1, days + 1)} for cid in cells}


class RainTotalTests(JobTests):
    def test_first_run_backfills_and_sums_60_days(self):
        fetch = fake_open_meteo(precip=2.0)
        out = self.run_job(date(2026, 9, 10), fetch, cfg=CFG_RAIN)
        # one group: every square needs 59 past days
        self.assertEqual([(c[0], c[1]["past_days"]) for c in fetch.calls], [(3, 59)])
        self.assertEqual(out["backfill"], {"cells": 3, "past_days": 59,
                                           "extra_calls": round(3 * 70 / 14 - 3 * 20 / 14)})
        cell = out["cells"]["7_9"]
        self.assertEqual(len(cell), 7)
        total, fcst = cell[5], cell[6]
        self.assertEqual(total, [120] * 11)           # 60 days x 2 mm
        self.assertEqual(fcst[0], 2)                   # today's rain is forecast
        self.assertEqual(fcst[10], 22)                 # day +10: 11 forecast days
        self.assertEqual(cell[0][0], 11)               # suitability as before: 10 mm weighted
        self.assertEqual(out["rainTotal"]["days"], 60)

    def test_history_file(self):
        self.run_job(date(2026, 9, 10), fake_open_meteo(precip=2.0), cfg=CFG_RAIN)
        h = self.history
        self.assertEqual(h["version"], 1)
        self.assertEqual(h["start"], "2026-06-27")     # 75 days before 10 Sep
        vals = h["cells"]["8_9"]
        self.assertEqual(len(vals), 75)
        self.assertEqual(vals[-59:], [2.0] * 59)        # 12 Jul - 9 Sep fetched
        self.assertEqual(vals[:16], [None] * 16)        # older days were never needed
        # the last day kept is yesterday: today is not complete yet
        self.assertEqual(date.fromisoformat(h["start"]) + timedelta(days=74), date(2026, 9, 9))

    def test_next_run_fetches_only_new_days(self):
        day = date(2026, 9, 10)
        self.run_job(day, fake_open_meteo(precip=2.0), cfg=CFG_RAIN)
        hist = build_scores.history_from_json(self.history)
        fetch = fake_open_meteo(precip=4.0)
        out = self.run_job(day + timedelta(days=1), fetch, cfg=CFG_RAIN, history=hist)
        self.assertEqual([c[1]["past_days"] for c in fetch.calls], [9])
        self.assertNotIn("backfill", out)
        # 50 old days at 2 mm + 9 re-fetched past days and today at 4 mm
        self.assertEqual(out["cells"]["7_9"][5][0], 50 * 2 + 10 * 4)

    def test_gap_longer_than_nine_days_is_refilled(self):
        day = date(2026, 9, 10)
        hist = history_through(day - timedelta(days=12))  # last run 12 days ago
        fetch = fake_open_meteo()
        out = self.run_job(day, fetch, cfg=CFG_RAIN, history=hist)
        self.assertEqual([c[1]["past_days"] for c in fetch.calls], [12])
        self.assertEqual(out["cells"]["7_9"][5][0], 600)

    def test_new_square_backfilled_alone(self):
        day = date(2026, 9, 10)
        hist = history_through(day, cells=("7_9", "7_10"))
        fetch = fake_open_meteo()
        out = self.run_job(day, fetch, cfg=CFG_RAIN, history=hist)
        self.assertEqual([(c[0], c[1]["past_days"]) for c in fetch.calls], [(2, 9), (1, 59)])
        self.assertEqual(out["backfill"]["cells"], 1)
        self.assertEqual(out["cells"]["8_9"][5][0], 600)

    def test_backfill_cap(self):
        cfg = copy.deepcopy(CFG_RAIN)
        cfg["rain_total"]["max_backfill_calls"] = 1   # 14 * 1 / 3 -> 4 extra days
        fetch = fake_open_meteo()
        out = self.run_job(date(2026, 9, 10), fetch, cfg=cfg)
        self.assertEqual(fetch.calls[0][1]["past_days"], 13)
        self.assertIsNone(out["cells"]["7_9"][5][0])     # not enough days for a total
        self.assertEqual(out["cells"]["7_9"][0][0], 100)  # the score is unaffected

    def test_out_of_season_still_gives_rain(self):
        day = date(2026, 1, 15)
        out = self.run_job(day, fake_open_meteo(), cfg=CFG_RAIN, history=history_through(day))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["cells"]["7_9"][0], [None] * 11)
        self.assertEqual(out["cells"]["7_9"][5], [600] * 11)

    def test_squares_no_longer_in_layer_dropped(self):
        day = date(2026, 9, 10)
        hist = history_through(day, cells=("7_9", "7_10", "8_9", "99_99"))
        self.run_job(day, fake_open_meteo(), cfg=CFG_RAIN, history=hist)
        self.assertEqual(sorted(self.history["cells"]), ["7_10", "7_9", "8_9"])

    def test_missing_day_gives_no_total(self):
        by_date = {"2026-09-10": (1.0,)}
        hist = {(date(2026, 9, 10) - timedelta(days=i)).isoformat(): 1.0 for i in range(1, 60)}
        self.assertEqual(build_scores.rain_total(hist, by_date, date(2026, 9, 10), 60, "2026-09-10"), (60, 1))
        del hist["2026-08-01"]
        self.assertEqual(build_scores.rain_total(hist, by_date, date(2026, 9, 10), 60, "2026-09-10"), (None, None))

    def test_history_json_round_trip_and_bad_input(self):
        day = date(2026, 9, 10)
        hist = history_through(day, mm=1.5)
        now = datetime(2026, 9, 10, 5, tzinfo=ZoneInfo("Europe/Stockholm"))
        js = build_scores.history_to_json(hist, day, 75, now, "Europe/Stockholm")
        self.assertEqual(build_scores.history_from_json(json.loads(json.dumps(js))), hist)
        self.assertEqual(build_scores.history_from_json(None), {})
        self.assertEqual(build_scores.history_from_json({"version": 2}), {})
        self.assertEqual(build_scores.history_from_json({"version": 1, "start": "x", "cells": {}}), {})

    def test_skip_out_of_season_once_updated_today(self):
        jan = date(2026, 1, 15)
        fresh = {"updated": "2026-01-15T01:23+01:00"}
        self.assertTrue(build_scores.should_skip("schedule", CFG_RAIN, jan, fresh))
        self.assertFalse(build_scores.should_skip("schedule", CFG_RAIN, jan, {"updated": "2026-01-14T22:23+01:00"}))
        self.assertFalse(build_scores.should_skip("push", CFG_RAIN, jan, fresh))
        self.assertFalse(build_scores.should_skip("schedule", CFG_RAIN, jan, None))
        sep = {"updated": "2026-09-15T01:23+02:00"}
        self.assertFalse(build_scores.should_skip("schedule", CFG_RAIN, date(2026, 9, 15), sep))
        # late June: day +10 is in July, so every run counts
        self.assertFalse(build_scores.should_skip("schedule", CFG_RAIN, date(2026, 6, 25),
                                                  {"updated": "2026-06-25T01:23+02:00"}))


if __name__ == "__main__":
    unittest.main()
