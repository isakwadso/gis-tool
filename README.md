# Field mushroom map (Skåne)

A phone-friendly map of where the weather has suited the field mushroom
(*Agaricus campestris*, ängschampinjon) on permanent pasture, for today and the
next 10 days. A second view shows how much rain has fallen over the last 60 days.
It runs entirely on free GitHub services: no server, no API keys.

**Map:** https://isakwadso.github.io/gis-tool/ — on a phone, open it and choose
"Add to Home Screen" to use it like an app (it also works offline with the last
downloaded data).

## How it works

```
Jordbruksverket WFS ──(yearly)──► Build pasture layer ──► site/data/pasture/*.json (committed)
OSM Overpass API ────(yearly)──┘                         site/data/cells.json, places.json
Open-Meteo API ──(every 3 h)───► Forecast and publish ──► site/data/scores.json ──► GitHub Pages
                 rain history ◄── read back from the live site ──┘  site/data/rain_history.json
```

1. **Pasture layer** (`scripts/build_pasture.py`, workflow *Build pasture layer*):
   downloads the latest farm blocks (*jordbruksblock*) of type *Bete* for Skåne
   from Jordbruksverket's public WFS and keeps only blocks that have been pasture
   in each of the last 10 yearly layers (`pasture.min_years`). A block passes a year
   if its id is in that year's layer, or — when Jordbruksverket redrew it under a new
   id — if at least half its area (`pasture.history_overlap`) was pasture that year.
   It then simplifies the outlines to ~1.5 m, groups them into small files, and
   assigns each block to a ~5 km weather grid square.
   The same workflow fetches city, town and village names for the region from
   OpenStreetMap (`scripts/build_places.py`); the page draws them on top of the
   coloured squares so you can see where you are when zoomed out. The public
   Overpass servers often refuse requests from GitHub's machines; when that
   happens the existing `site/data/places.json` (fetched 8 Oct 2026) is kept.
2. **Scores** (`scripts/build_scores.py`, workflow *Forecast and publish*): asks
   Open-Meteo for daily rain, mean and minimum temperature and reference
   evapotranspiration (ET0) for the past 9 days and the next 11 days at every grid
   square that contains pasture, then scores each day with the model below.
   It also keeps a **rain history**: the daily rain of the last 75 days per square,
   published with the site as `data/rain_history.json` and read back by the next run.
   Every run fetches the last 9 days anyway, so only days the history lacks are fetched
   again (the first run, a new square, or an outage longer than 9 days); those squares
   get more past days in the same request (`rain_total.max_backfill_calls` caps it).
   If the live history cannot be read, the run fails rather than start from scratch.
   Out of season (no day on the slider in July–November) scheduled runs fetch once a
   day and skip publishing otherwise.
3. **Page** (`site/`): Leaflet map with an OpenStreetMap base map. Zoomed out it
   shows the grid squares; zoomed in (level 12+) it draws the individual pastures.
   The switch at the top changes between **Suitability** and **Rain, 60 days**
   (`?view=rain` opens the rain view; the last choice is remembered).

## The model (`scripts/model.py`, settings in `config.json` → `model`)

Every part is a factor from 0 to 1 and the parts are multiplied:
**score = rain × temperature × drying × frost**, shown as 0–100.

| Part | Rule |
|---|---|
| Rain | Last 10 days, weighted: rain 2 days before the scored day counts most, the day itself ⅓ of that, the day before ⅔; older rain fades 20 %/day. Weights add up to 5, so steady rain equals a 5-day total. 0 at ≤ 5 mm, rising linearly to 1 at ≥ 30 mm |
| Temperature | 5-day mean of daily mean: 0 at ≤ 0 °C, rising to 1 at 5 °C, 1 up to 12 °C, falling to 0 at 15 °C, 0 above |
| Drying | Reference evapotranspiration (ET0, FAO-56) on each of the last 5 days: none below 1.0 mm, up to 30 % removed at ≥ 3.5 mm, with day weights 1, 1, 0.75, 0.5, 0.25 (today first). Season-aware: a clear summer day dries far more than a clear November day |
| Frost | Each night below 0 °C in the last 5 days multiplies by 0.75 |
| Days ahead | Optional discount for forecast uncertainty: the score for day +k is multiplied by `forecast_uncertainty.keep_per_day`^k. Set to 1.0 (no decline); e.g. 0.95 gives day +5 ×0.77 and day +10 ×0.60. Only the score is lowered; the rain, temperature and drying shown in the popup are not |
| Season | Only days in July–November get a score |
| Land | Pasture (*Bete*) in each of the last 10 yearly layers |

This is a relative suitability index, not a calibrated probability.

**Rain target (15 Oct 2026).** Full rain score at 30 mm marks a really rainy period
for Skåne in autumn. At 19 SMHI gauges (Jul–Nov 1996–2025) the weighted rain has a
median of 9 mm; the wettest 10 % of days reach 27 mm and the wettest 5 % 35 mm.
Open-Meteo's model rain, which the map uses, runs about 10 % lower (wettest 10 % ≈ 23 mm).
With 5→30 mm, about 9 % of autumn days reach 75 % of the rain score and 5 % reach
full score, and most years every area gets at least one full-score spell. The earlier
50 mm target was reached on only ~1 % of days.

**Checked against finds (15 Oct 2026).** 233 dated finds in Skåne (GBIF, which
includes Artportalen; Jul–Nov, 2005–2026) were compared with the 10 days before
and after at the same place, using Open-Meteo's historical weather. A rule with
no skill ranks the find day at 50 %. The old 5-day rain total scored 51 %, the
10-day weighted rain about 54 % (delays of 1–5 days all similar), and ET0 drying
added little that the finds can resolve. The temperature rule gives 0 on 90 of
the 233 find days (5-day mean ≥ 15 °C, mostly July–September). Sunshine hours
from Open-Meteo were not used: at Lund they ran 50–85 % above SMHI's measured
sunshine in Jul–Sep 2026, while solar radiation (which ET0 uses) was within 2–13 %.

## Rain, 60 days (separate view; settings in `config.json` → `rain_total`)

Total rain over the 60 days ending on the chosen day, per weather square: past days
from the stored rain history, today and later days from the forecast. It is not part
of the suitability score. Colour classes (mm): under 60, 60–80, 80–110, 110–150,
150–190 and 190 or more, roughly the 10th, 25th, 50th, 75th and 90th percentiles of
60-day rain ending in July–November at 49 SMHI stations in Skåne (1996–2025), scaled
by 0.85 because Open-Meteo's model rain over 60 days came out 3–24 % below four gauges
in 2023–2025 (mean 15 %). The popup compares the total with the month's normal
(`rain_total.normal_mm`, same stations and scaling).

**Checked against finds (1 Nov 2026).** `tools/rain_analysis.py` (workflow *Rain
analysis*, results on the `analysis` branch) took 249 dated finds in Skåne (GBIF,
Jul–Nov 2005–2026) and the nearest SMHI station (median 7 km), and ranked the rain
before each find against the same weeks in the 10 years either side (50 % = no relation,
95 % intervals resample whole years):

| Rain before the find | Rank |
|---|---|
| 10 days | 57 % (54–61) |
| 20 days | 59 % (55–65) |
| 30 days | 56 % (51–63) |
| 45 days | 48 % (42–57) |
| 60 days | 44 % (37–55) |
| 90 days | 39 % (30–52) |
| days 11–60 only | 41 % (34–51) |

So rain in the last 2–4 weeks was above normal at finds, but the 60-day total was not
(median 93 % of normal; 25 % of finds came after a 60-day total in the wettest third).
Years with a wet late summer (60 days to 18 Sep, mean of the stations) had fewer finds
per 10,000 fungus records in GBIF (Spearman −0.50, p = 0.02, 21 years; −0.37 for the
raw counts after removing the trend in reporting). An exploratory check, made after
seeing these numbers: 40 % of finds had a wetter than normal last 10 days after drier
than normal days 11–60, against 26 % expected from the same dates in other years
(difference 7–23 points). That "dry spell, then rain" pattern needs new seasons to
confirm before it goes into the score.

## Running things by hand

In the repository: **Actions** → pick a workflow → **Run workflow**.

- *Build pasture layer*: re-download the pasture data (also runs every 15 February).
- *Forecast and publish*: refresh the weather and republish the site now.

To change thresholds, the season, or the region, edit `config.json` and commit;
the workflows rerun by themselves. For another county, change
`region.region_kod_prefix` (the first two digits of Jordbruksverket's
`region_kod`, which is the county code; Skåne is `12`) and `region.name`.

Local test: `python -m unittest discover -s scripts`

## Costs and limits

- GitHub Actions and Pages are free for public repositories on standard runners.
  The workflows use only GitHub-made actions and `ubuntu-latest`.
- Open-Meteo's free tier is for non-commercial use (600 calls/min, 10,000/day).
  Each run asks for 20 days per grid square, which Open-Meteo counts as about
  1.4 calls per square (~670 per run, ~5,300 a day at 8 runs). Squares are sent in
  batches of 100 with a pause sized to stay under the per-minute limit. Visitors
  never call Open-Meteo; only the workflow does.
- The rain history adds nothing to an ordinary run. Filling it the first time costs
  about 1,700 extra calls once (70 days per square); a gap longer than 9 days costs a
  little extra for the missing days only. Out of season the weather is fetched once a
  day (~670 calls). The rain analysis uses SMHI and GBIF; its Open-Meteo check
  (~160 calls) ran once and is reused.
- GitHub Pages has a soft limit of 100 GB traffic per month; the site is a few MB.

## Data and licences

- Weather: [Open-Meteo.com](https://open-meteo.com/), CC BY 4.0. The scores are derived from it.
- Rain normals and the find analysis: [SMHI](https://www.smhi.se/data/om-smhis-data/villkor-for-anvandning) open data, CC BY 4.0;
  finds from [GBIF.org](https://www.gbif.org/) (mostly Artportalen).
- Pasture: [Jordbruksverket](https://jordbruksverket.se/e-tjanster-databaser-och-appar/e-tjanster-och-databaser-stod/kartor-och-gis),
  jordbruksblock (årslager), open data via their INSPIRE WFS.
- Base map and place names: © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL).
- Map library: [Leaflet](https://leafletjs.com/) 1.9.4 (BSD-2-Clause), included in `site/vendor/`.

## Safety

Field mushrooms can be confused with toxic species, including the yellow-staining
mushroom (*Agaricus xanthodermus*) and deadly white amanitas. Only eat mushrooms
you have identified with certainty. Pastures are private land: follow
allemansrätten.
