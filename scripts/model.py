"""Suitability model for field mushroom (Agaricus campestris) on pasture.

All settings come from config.json -> "model". Every part is a factor from 0 to 1
and the parts are multiplied:

  score = rain x temperature x drying x frost

  rain         Weighted rain over the last `rain.days` days (10). Weights rise from the
               day itself to a peak `peak_days_back` days earlier (2), then fade by
               `fade_per_day` (20 %) per older day. Weights are scaled to add up to 5, so
               steady rain gives the same number as a plain 5-day total and `rain.mm`
               keeps its meaning: 0 at <= 5 mm, rising linearly to 1 at >= 50 mm.
  temperature  Mean of daily mean temperature over `temperature.days` days:
               0 at <= c0, linear to 1 at c1, 1 up to c2, linear down to 0 at c3, 0 above.
  drying       Season-aware drying from reference evapotranspiration (ET0, FAO-56), the
               water a lawn loses in a day from sun, warmth, dry air and wind. For each of
               the last `drying.days` days a share is removed:
                   loss = max_loss x day_weight x clamp((ET0 - e0) / (e1 - e0))
               so a dull day (ET0 <= e0) costs nothing and a strongly drying day
               (ET0 >= e1) costs max_loss, with recent days weighted most.
  frost        Each night in the last `frost.days` days with minimum below `below_c`
               multiplies the score by `keep_per_night`.

The score is a relative suitability index, not a calibrated probability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

Day = tuple  # (precipitation mm, mean temperature °C, minimum temperature °C, ET0 mm)


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def history_days(model: dict) -> int:
    """How many days of weather, ending on the scored day, the model needs."""
    return max(model["rain"]["days"], model["temperature"]["days"],
               model["frost"]["days"], model["drying"]["days"])


def rain_weights(days: int, peak_days_back: int, fade_per_day: float, total: float = 5.0) -> list[float]:
    """Weight per day, index = days back (0 = the scored day itself)."""
    raw = []
    for i in range(days):
        if i < peak_days_back:
            raw.append((i + 1) / (peak_days_back + 1))
        else:
            raw.append((1 - fade_per_day) ** (i - peak_days_back))
    s = sum(raw)
    return [w * total / s for w in raw]


def rain_factor(weighted_mm: float, mm: Sequence[float]) -> float:
    lo, hi = mm
    return clamp((weighted_mm - lo) / (hi - lo))


def temp_factor(mean_c: float, c: Sequence[float]) -> float:
    t0, t1, t2, t3 = c
    if mean_c <= t0 or mean_c >= t3:
        return 0.0
    if mean_c < t1:
        return (mean_c - t0) / (t1 - t0)
    if mean_c <= t2:
        return 1.0
    return (t3 - mean_c) / (t3 - t2)


def drying_factor(et0_newest_first: Sequence[float], drying: dict) -> float:
    e0, e1 = drying["et0_mm"]
    f = 1.0
    for et0, w in zip(et0_newest_first, drying["day_weights"]):
        f *= 1 - drying["max_loss"] * w * clamp((et0 - e0) / (e1 - e0))
    return f


@dataclass
class DayResult:
    score: float | None      # 0..1, None if weather is missing
    rain_mm: float | None    # weighted rain (5-day equivalent)
    tmean_c: float | None
    frost_nights: int | None
    drying_loss: float | None  # 0..1, share removed by drying


MISSING = DayResult(None, None, None, None, None)


def score_day(history: Sequence[Day], model: dict) -> DayResult:
    """Score one day. `history` holds the days up to and including the scored day,
    oldest first; it must be at least history_days(model) long."""
    need = history_days(model)
    if len(history) < need:
        raise ValueError(f"need {need} days of history, got {len(history)}")
    newest = list(reversed(history[-need:]))  # index = days back

    rain_cfg, temp_cfg = model["rain"], model["temperature"]
    frost_cfg, dry_cfg = model["frost"], model["drying"]
    precip = [d[0] for d in newest[:rain_cfg["days"]]]
    tmean = [d[1] for d in newest[:temp_cfg["days"]]]
    tmin = [d[2] for d in newest[:frost_cfg["days"]]]
    et0 = [d[3] for d in newest[:dry_cfg["days"]]]
    if any(v is None for v in (*precip, *tmean, *tmin, *et0)):
        return MISSING

    weights = rain_weights(rain_cfg["days"], rain_cfg["peak_days_back"], rain_cfg["fade_per_day"])
    rain_mm = sum(w * p for w, p in zip(weights, precip))
    mean_c = sum(tmean) / len(tmean)
    nights = sum(1 for t in tmin if t < frost_cfg["below_c"])
    dry = drying_factor(et0, dry_cfg)

    score = (rain_factor(rain_mm, rain_cfg["mm"]) * temp_factor(mean_c, temp_cfg["c"])
             * dry * frost_cfg["keep_per_night"] ** nights)
    return DayResult(clamp(score), rain_mm, mean_c, nights, 1 - dry)
