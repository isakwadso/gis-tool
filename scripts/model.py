"""Suitability model for field mushroom (Agaricus campestris) on pasture.

All thresholds come from config.json -> "model". For a target day D the window
is the `window_days` days ending on D (D-4 .. D for a 5-day window).

  rain factor   0 at <= rain_mm[0] (5 mm), rising linearly to 1 at >= rain_mm[1] (20 mm),
                using the window's total precipitation
  temp factor   window mean of daily mean temperature:
                0 at <= t0 (0 °C), linear up to 1 at t1 (5 °C), 1 up to t2 (12 °C),
                linear down to 0 at t3 (15 °C), 0 above
  frost         each day whose minimum temperature is below frost_below_c (0 °C)
                subtracts frost_penalty (0.25)

  score = clamp(rain factor x temp factor - frost penalty x frost days, 0, 1)

This is a relative suitability index, not a calibrated probability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def rain_factor(total_mm: float, rain_mm: Sequence[float]) -> float:
    lo, hi = rain_mm
    return clamp((total_mm - lo) / (hi - lo))


def temp_factor(mean_c: float, temp_c: Sequence[float]) -> float:
    t0, t1, t2, t3 = temp_c
    if mean_c <= t0 or mean_c >= t3:
        return 0.0
    if mean_c < t1:
        return (mean_c - t0) / (t1 - t0)
    if mean_c <= t2:
        return 1.0
    return (t3 - mean_c) / (t3 - t2)


@dataclass
class DayResult:
    score: float | None  # 0..1, None if data missing or out of season
    rain_mm: float | None
    tmean_c: float | None
    frost_days: int | None


def score_window(precip: Sequence[float | None], tmean: Sequence[float | None],
                 tmin: Sequence[float | None], model: dict) -> DayResult:
    """Score one window of daily values (oldest first)."""
    n = model["window_days"]
    if len(precip) != n or len(tmean) != n or len(tmin) != n:
        raise ValueError(f"window must have {n} days")
    if any(v is None for v in (*precip, *tmean, *tmin)):
        return DayResult(None, None, None, None)
    total = float(sum(precip))
    mean_t = float(sum(tmean)) / n
    frost = sum(1 for v in tmin if v < model["frost_below_c"])
    s = rain_factor(total, model["rain_mm"]) * temp_factor(mean_t, model["temp_c"])
    s = clamp(s - model["frost_penalty"] * frost)
    return DayResult(s, total, mean_t, frost)
