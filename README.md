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
                                                          site/data/cells.json
Open-Meteo API ──(twice daily)──► Forecast and publish ──► site/data/scores.json ──► GitHub Pages
```

1. **Pasture layer** (`scripts/build_pasture.py`, workflow *Build pasture layer*):
   downloads the latest farm blocks (*jordbruksblock*) of type *Bete* for Skåne
   from Jordbruksverket's public WFS, simplifies the outlines to ~1.5 m, groups
   them into small files, and assigns each block to a ~5 km weather grid square.
2. **Scores** (`scripts/build_scores.py`, workflow *Forecast and publish*): asks
   Open-Meteo for daily rain, mean and minimum temperature for the past 4 days and
   the next 11 days at every grid square that contains pasture, then scores each
   day with the model below.
3. **Page** (`site/`): Leaflet map with an OpenStreetMap base map. Zoomed out it
   shows the grid squares; zoomed in (level 12+) it draws the individual pastures.

## The model (`scripts/model.py`, settings in `config.json`)

For a day D the window is the 5 days ending on D.

| Part | Rule |
|---|---|
| Rain | Window total: 0 at ≤ 5 mm, rising linearly to 1 at ≥ 20 mm |
| Temperature | Window mean of daily mean: 0 at ≤ 0 °C, rising to 1 at 5 °C, 1 up to 12 °C, falling to 0 at 15 °C, 0 above |
| Frost | Each day with minimum below 0 °C subtracts 0.25 |
| Score | clamp(rain × temperature − frost, 0, 1), shown as 0–100 |
| Season | Only days in July–November get a score |

This is a relative suitability index, not a calibrated probability.

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
  Each run uses roughly one call per grid square, sent in batches of 100 with a
  pause between them. Visitors never call Open-Meteo; only the workflow does.
- GitHub Pages has a soft limit of 100 GB traffic per month; the site is a few MB.

## Data and licences

- Weather: [Open-Meteo.com](https://open-meteo.com/), CC BY 4.0. The scores are derived from it.
- Pasture: [Jordbruksverket](https://jordbruksverket.se/e-tjanster-databaser-och-appar/e-tjanster-och-databaser-stod/kartor-och-gis),
  jordbruksblock (årslager), open data via their INSPIRE WFS.
- Base map: © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.
- Map library: [Leaflet](https://leafletjs.com/) 1.9.4 (BSD-2-Clause), included in `site/vendor/`.

## Safety

Field mushrooms can be confused with toxic species, including the yellow-staining
mushroom (*Agaricus xanthodermus*) and deadly white amanitas. Only eat mushrooms
you have identified with certainty. Pastures are private land: follow
allemansrätten.
