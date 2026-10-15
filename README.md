# Field mushroom map (Skåne)

A phone-friendly map of where the weather has suited the field mushroom
(*Agaricus campestris*, ängschampinjon) on permanent pasture, for today and the
next 10 days. It runs entirely on free GitHub services: no server, no API keys.

**Map:** https://isakwadso.github.io/gis-tool/ — on a phone, open it and choose
"Add to Home Screen" to use it like an app (it also works offline with the last
downloaded data).

## How it works

```
Jordbruksverket WFS ──(yearly)──► Build pasture layer ──► site/data/pasture/*.json (committed)
OSM Overpass API ────(yearly)──┘                         site/data/cells.json, places.json
Open-Meteo API ──(every 3 h)───► Forecast and publish ──► site/data/scores.json ──► GitHub Pages
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
3. **Page** (`site/`): Leaflet map with an OpenStreetMap base map. Zoomed out it
   shows the grid squares; zoomed in (level 12+) it draws the individual pastures.

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
  1.4 calls per square (~650 per run, ~5,300 a day at 8 runs). Squares are sent in
  batches of 100 with a pause sized to stay under the per-minute limit. Visitors
  never call Open-Meteo; only the workflow does.
- GitHub Pages has a soft limit of 100 GB traffic per month; the site is a few MB.

## Data and licences

- Weather: [Open-Meteo.com](https://open-meteo.com/), CC BY 4.0. The scores are derived from it.
- Pasture: [Jordbruksverket](https://jordbruksverket.se/e-tjanster-databaser-och-appar/e-tjanster-och-databaser-stod/kartor-och-gis),
  jordbruksblock (årslager), open data via their INSPIRE WFS.
- Base map and place names: © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL).
- Map library: [Leaflet](https://leafletjs.com/) 1.9.4 (BSD-2-Clause), included in `site/vendor/`.

## Safety

Field mushrooms can be confused with toxic species, including the yellow-staining
mushroom (*Agaricus xanthodermus*) and deadly white amanitas. Only eat mushrooms
you have identified with certainty. Pastures are private land: follow
allemansrätten.
