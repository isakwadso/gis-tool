"""Tests for the scoring model and the daily job. Run: python -m unittest discover -s scripts"""

import json
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_scores  # noqa: E402
from model import rain_factor, score_window, temp_factor  # noqa: E402

CFG = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
MODEL = CFG["model"]


class FactorTests(unittest.TestCase):
    def test_rain_scale(self):
        r = MODEL["rain_mm"]
        self.assertEqual(rain_factor(0, r), 0)
        self.assertEqual(rain_factor(5, r), 0)
        self.assertAlmostEqual(rain_factor(12.5, r), 0.5)
        self.assertEqual(rain_factor(20, r), 1)
        self.assertEqual(rain_factor(60, r), 1)

    def test_temperature_scale(self):
        t = MODEL["temp_c"]
        self.assertEqual(temp_factor(-3, t), 0)
        self.assertEqual(temp_factor(0, t), 0)
        self.assertAlmostEqual(temp_factor(2.5, t), 0.5)
        self.assertEqual(temp_factor(5, t), 1)
        self.assertEqual(temp_factor(9, t), 1)
        self.assertEqual(temp_factor(12, t), 1)
        self.assertAlmostEqual(temp_factor(13.5, t), 0.5)
        self.assertEqual(temp_factor(15, t), 0)
        self.assertEqual(temp_factor(18, t), 0)


class WindowTests(unittest.TestCase):
    def test_ideal_conditions(self):
        r = score_window([4, 4, 4, 4, 4], [9] * 5, [5] * 5, MODEL)
        self.assertEqual(r.score, 1)
        self.assertEqual(r.rain_mm, 20)
        self.assertEqual(r.frost_days, 0)

    def test_frost_subtracts(self):
        r = score_window([4] * 5, [6] * 5, [3, -1, 2, -2, 1], MODEL)
        self.assertAlmostEqual(r.score, 0.5)
        self.assertEqual(r.frost_days, 2)

    def test_frost_never_below_zero(self):
        r = score_window([1] * 5, [6] * 5, [-1] * 5, MODEL)
        self.assertEqual(r.score, 0)

    def test_combined(self):
        # 12.5 mm -> 0.5, mean 13.5 C -> 0.5, no frost -> 0.25
        r = score_window([2.5] * 5, [13.5] * 5, [8] * 5, MODEL)
        self.assertAlmostEqual(r.score, 0.25)

    def test_missing_data(self):
        r = score_window([1, None, 1, 1, 1], [6] * 5, [3] * 5, MODEL)
        self.assertIsNone(r.score)


def fake_open_meteo(precip=4.0, tmean=8.0, tmin=3.0, missing_last=False):
    """Stand-in for Open-Meteo returning the same daily values everywhere."""
    def fetch(points, params):
        tz = ZoneInfo(params["timezone"])
        today = datetime.now(tz).date() if not hasattr(fetch, "today") else fetch.today
        days = [today - timedelta(days=params["past_days"]) + timedelta(days=k)
                for k in range(params["past_days"] + params["forecast_days"])]
        out = []
        for lat, lon in points:
            p = [precip] * len(days)
            if missing_last:
                p[-1] = None
            out.append({
                "latitude": lat, "longitude": lon, "timezone": params["timezone"],
                "daily_units": {"precipitation_sum": "mm"},
                "daily": {
                    "time": [d.isoformat() for d in days],
                    "precipitation_sum": p,
                    "temperature_2m_mean": [tmean] * len(days),
                    "temperature_2m_min": [tmin] * len(days),
                },
            })
        return out
    return fetch


CELLS = {"version": "test", "cells": [["7_9", 55.68, 13.17, 6.9, 8], ["7_10", 55.68, 13.24, 14.5, 6],
                                      ["8_9", 55.70, 13.14, 13.4, 7]]}


class JobTests(unittest.TestCase):
    def run_job(self, day, fetch, cells=CELLS):
        now = datetime.combine(day, datetime.min.time()).replace(hour=5, tzinfo=ZoneInfo("Europe/Stockholm"))
        fetch.today = day
        return build_scores.build(CFG, cells, now, fetch, pause_s=0)

    def test_in_season(self):
        out = self.run_job(date(2026, 9, 10), fake_open_meteo())
        self.assertEqual(out["status"], "ok")
        self.assertEqual(len(out["dates"]), CFG["forecast"]["days_ahead"] + 1)
        s, r, t, f = out["cells"]["7_9"]
        self.assertEqual(s, [100] * 11)
        self.assertEqual(r[0], 20.0)
        self.assertEqual(t[0], 8.0)
        self.assertEqual(f[0], 0)

    def test_season_edge(self):
        # 25 Nov: days from 1 Dec on are out of season -> score None, weather still shown
        out = self.run_job(date(2026, 11, 25), fake_open_meteo())
        s, r, _, _ = out["cells"]["7_9"]
        self.assertEqual(out["inSeason"], [True] * 6 + [False] * 5)
        self.assertEqual(s[:6], [100] * 6)
        self.assertEqual(s[6:], [None] * 5)
        self.assertEqual(r[6], 20.0)

    def test_out_of_season_skips_api(self):
        def never(points, params):
            raise AssertionError("API should not be called out of season")
        out = self.run_job(date(2026, 1, 15), never)
        self.assertEqual(out["status"], "out-of-season")

    def test_no_pasture_layer(self):
        out = self.run_job(date(2026, 9, 10), fake_open_meteo(), cells=None)
        self.assertEqual(out["status"], "no-pasture-layer")

    def test_missing_last_day(self):
        out = self.run_job(date(2026, 9, 10), fake_open_meteo(missing_last=True))
        s = out["cells"]["7_9"][0]
        self.assertIsNone(s[-1])
        self.assertEqual(s[0], 100)

    def test_batching(self):
        calls = []
        inner = fake_open_meteo()
        inner.today = date(2026, 8, 1)

        def counting(points, params):
            calls.append(len(points))
            return inner(points, params)
        many = {"version": "t", "cells": [[f"{i}_0", 55.5, 13.0, 1.0, 1] for i in range(250)]}
        out = self.run_job(date(2026, 8, 1), counting, cells=many)
        self.assertEqual(calls, [100, 100, 50])
        self.assertEqual(len(out["cells"]), 250)
        self.assertEqual(out["cells"]["249_0"][0][0], 100)


if __name__ == "__main__":
    unittest.main()
