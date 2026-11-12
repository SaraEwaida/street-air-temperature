# street-air-temperature

![tests](https://github.com/SaraEwaida/street-air-temperature/actions/workflows/tests.yml/badge.svg)

Estimating street-level 2 m **air** temperature in Phoenix from coarse ERA5 reanalysis (~25 km), land cover, vegetation and elevation, validated on weather stations the model never saw.

**Status:** milestone 1 done: 15 stations, 120,617 station-hours, ERA5 and site features. Model, temperature catalog and API come next (see [Roadmap](#roadmap)).

Companion to [Urban-Heat-Downscaling](https://github.com/SaraEwaida/Urban-Heat-Downscaling), which downscales satellite **land surface** temperature. Surface temperature is the skin of roofs, roads and sand; air temperature at 2 m is what people feel. This project targets the second.

## Why

Reanalysis such as ERA5 gives hourly air temperature everywhere, but one value per ~25 km cell: it cannot separate a downtown street from an irrigated park or open desert a few kilometres away. Weather stations measure the real local value, but only at a few points. The goal is to learn the local correction from what surrounds each station, so it can be applied anywhere in the city.

## Data

All sources are free and need no account.

| Data | Source | Resolution | Use |
|---|---|---|---|
| Station air temperature | NOAA Integrated Surface Database (global-hourly CSV), via NOAA Open Data Dissemination on AWS | point, hourly | target (airports) |
| Station air temperature | Arizona Meteorological Network (AZMet) API, University of Arizona | point, hourly | target (turf, farmland, suburbs) |
| ERA5 2 m air temperature | Open-Meteo historical archive, `models=era5` | 0.25° (~25 km), hourly | coarse input |
| Land cover | ESA WorldCover v200, Planetary Computer | 10 m | site features |
| Elevation | Copernicus DEM GLO-30, Planetary Computer | 30 m | site features, lapse-rate baseline |
| Vegetation | Landsat 8/9 Collection 2 Level-2 NDVI, Planetary Computer | 30 m | site features |

Details that matter:

- **Station files** come from the `noaa-global-hourly-pds` S3 bucket: NCEI ended direct HTTPS download of ISD CSV in 2026, and ISD is superseded by GHCNh with no updates after August 2025. The 2023 study year is fully covered; GHCNh is the source to switch to for recent years.
- **ERA5 is requested as the raw grid-cell value** (`elevation=nan`). By default Open-Meteo adjusts temperature to the point's elevation; that correction is computed separately here as an explicit baseline (standard lapse rate, -6.5 °C/km), so the model's gain over it is visible.
- **Two station networks.** All NOAA stations around Phoenix are airports: open, paved and similar to each other, so on their own they cannot show what makes a park or farmland different. AZMet adds Phoenix Encanto and Phoenix Greenway (irrigated turf), Desert Ridge, Queen Creek and Buckeye (farmland and suburban edge). AZMet values are hourly averages stamped at the end of the hour, so they are assigned to the hour they end, matching ISD's routine reports a few minutes before the hour and ERA5 on the hour.
- **Station quality control:** ISD quality codes 2, 3, 6 and 7 are dropped; routine hourly METAR reports (FM-15) are preferred when several reports fall in one hour; values outside -15 to 55 °C are removed; single-hour spikes of more than 8 °C against both neighbouring hours are removed; stations with less than 55% of hours left are dropped. The threshold is 55%, not higher, because Chandler, Glendale and Goodyear airports report only about 60% of hours; errors will be reported by hour of day, so partial coverage does not bias the comparison. A per-station report is written to `outputs/qc_report.csv`.
- **Site features** are computed in circles of 100 m and 500 m around each station on a local UTM 12N grid: land-cover fractions (built-up, tree, shrub, grass/crop, bare, water), mean elevation, and median summer NDVI over up to five clear Landsat scenes. Station coordinates are deliberately not features, so the model has to learn from surroundings, not memorise places.
- Phoenix stays on MST (UTC-7) all year, so local hour is unambiguous.

## Setup

```bash
git clone https://github.com/SaraEwaida/street-air-temperature.git
cd street-air-temperature
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

## Reproduce milestone 1

```bash
sat stations configs/phoenix.yaml   # NOAA stations, download, QC -> data/phoenix/observations.parquet
sat era5 configs/phoenix.yaml       # ERA5 at each station        -> data/phoenix/era5.parquet
sat features configs/phoenix.yaml   # site features                -> data/phoenix/site_features.csv
sat build configs/phoenix.yaml      # station-hour table           -> data/phoenix/dataset.parquet
sat map configs/phoenix.yaml        # station map                  -> outputs/figures/phoenix_stations.png
```

Or all steps at once: `sat all configs/phoenix.yaml`.

## Results

### Milestone 1: ERA5 against 15 stations, 2023

All 15 stations passed quality control (10 NOAA airports, 5 AZMet sites), giving 120,617 station-hours. Bias is ERA5 minus station in °C, so **negative = station warmer than ERA5**. Night is 19:00-06:00 local time.

![Station map](outputs/figures/phoenix_stations.png)

| Station | Network | Bias, all hours | Night | Day | Built-up within 500 m | NDVI within 500 m |
|---|---|---:|---:|---:|---:|---:|
| Phoenix Sky Harbor (KPHX) | NOAA | -1.22 | **-1.72** | -0.73 | 0.61 | 0.05 |
| Falcon Field (KFFZ) | NOAA | -0.63 | -1.07 | -0.19 | 0.62 | 0.18 |
| Deer Valley (KDVT) | NOAA | -0.39 | -0.41 | -0.36 | 0.39 | 0.13 |
| Chandler (KCHD) | NOAA | +0.24 | -0.38 | +0.44 | 0.53 | 0.11 |
| Goodyear (KGYR) | NOAA | -0.01 | -0.08 | +0.01 | 0.41 | 0.14 |
| Scottsdale (KSDL) | NOAA | +0.24 | -0.05 | +0.53 | 0.73 | 0.14 |
| Glendale (KGEU) | NOAA | +0.23 | +0.12 | +0.26 | 0.50 | 0.10 |
| Luke AFB (KLUF) | NOAA | +0.24 | +0.28 | +0.20 | 0.17 | 0.13 |
| Williams Gateway (KIWA) | NOAA | +0.95 | +0.84 | +1.06 | 0.43 | 0.09 |
| Buckeye airport (KBXK) | NOAA | +1.24 | +1.99 | +0.50 | 0.21 | 0.13 |
| Desert Ridge (DES) | AZMet | +0.56 | +0.80 | +0.33 | 0.40 | 0.37 |
| Phoenix Greenway (PGR) | AZMet | +0.92 | +0.89 | +0.95 | 0.58 | 0.26 |
| Phoenix Encanto (PEN) | AZMet | +1.03 | +1.23 | +0.84 | 0.41 | 0.42 |
| Buckeye (BUC) | AZMet | +1.36 | +2.05 | +0.67 | 0.08 | 0.55 |
| Queen Creek (QCR) | AZMet | +1.63 | **+2.23** | +1.03 | 0.04 | 0.56 |

Overall: bias +0.45 °C, MAE 1.27 °C.

- **ERA5's night-time error follows the surroundings.** Across the 15 stations it correlates at -0.74 with built-up fraction and +0.71 with NDVI: ERA5 is too cool over dense, paved sites (the urban heat island, strongest at Sky Harbor) and too warm over irrigated parks and farmland (evaporative cooling, strongest at Queen Creek and Buckeye).
- **The effect is largest at night**, when the urban heat island is strongest, which is why errors will be reported by hour of day.
- **Without AZMet this was invisible:** airports alone span too narrow a range of surroundings. With 15 stations these correlations are a strong hint, not a result; testing on held-out stations in milestone 2 decides how much of it a model can use.
- Desert Ridge lost 42 hours to the range check, probably a sensor fault; every other station lost fewer than 3 hours to quality control.

## Roadmap

1. Station data pipeline: stations, QC, ERA5, site features, joined table (this milestone).
2. Model: LightGBM correction to ERA5, validated by leaving whole stations out; baselines raw ERA5 and lapse-rate ERA5; errors by hour of day and season.
3. Temperature catalog: 100 m air-temperature maps as Cloud-Optimised GeoTIFFs, indexed in a STAC catalog.
4. Service: FastAPI endpoint (location and time in, temperature out) in Docker.
5. Results and limitations write-up.

## Layout

```
configs/phoenix.yaml   bbox, period, QC thresholds, feature radii, paths
src/streetair/
  isd.py               NOAA ISD station list, parsing, quality control (shared by both networks)
  azmet.py             AZMet station table and hourly API
  era5.py              Open-Meteo ERA5 request and parsing
  features.py          site features from Planetary Computer (windowed reads)
  dataset.py           station-hour table, lapse-rate baseline, station summary
  plots.py             station map
  cli.py               `sat` command
tests/                 fixtures in the real formats; no network needed
```
