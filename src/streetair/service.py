"""Point predictions for the API: the same computation as the maps, for one location and hour.

Kept free of web-framework code so it can be tested directly. ERA5 is passed in by a fetcher
function, so tests run without network access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from .catalog import ERA5_STEP, era5_cells
from .dataset import LOCAL_TZ, era5_context
from .model import SITE, add_time_features

WEATHER_COLS = ("t_era5", "t_era5_change_3h", "t_era5_day_mean", "era5_wind_ms", "era5_cloud_pct",
                "era5_sw_wm2", "era5_rh_pct", "era5_cell_elev_m")


class PointError(ValueError):
    """A request that cannot be answered (outside the area, water, bad time)."""


def parse_time(value: str) -> pd.Timestamp:
    """ISO time to UTC, rounded to the hour. A time without a zone is read as Phoenix local time (MST)."""
    try:
        t = pd.Timestamp(value)
    except (ValueError, TypeError) as e:
        raise PointError(f"Cannot read time {value!r}; use ISO format, e.g. 2023-07-15T05:00") from e
    t = t.tz_localize(LOCAL_TZ) if t.tzinfo is None else t
    return t.tz_convert("UTC").round("h")


def cell_weights(lon: float, lat: float, cells: pd.DataFrame) -> list[tuple[str, float]]:
    """The 4 ERA5 cells around a point and their bilinear weights (same scheme as the maps)."""
    lons, lats = np.sort(cells.lon.unique()), np.sort(cells.lat.unique())
    key = {(round(a, 3), round(b, 3)): s for s, a, b in zip(cells.station, cells.lon, cells.lat)}

    def axis(v, c):
        f = float(np.clip((v - c[0]) / ERA5_STEP, 0, len(c) - 1))
        i0 = min(int(np.floor(f)), len(c) - 1)
        return i0, min(i0 + 1, len(c) - 1), f - i0

    x0, x1, wx = axis(lon, lons)
    y0, y1, wy = axis(lat, lats)
    out = {}
    for (i, j), w in (((x0, y0), (1 - wx) * (1 - wy)), ((x1, y0), wx * (1 - wy)),
                      ((x0, y1), (1 - wx) * wy), ((x1, y1), wx * wy)):
        s = key[(round(lons[i], 3), round(lats[j], 3))]
        out[s] = out.get(s, 0.0) + w
    return list(out.items())


@dataclass
class Service:
    cfg: dict[str, Any]
    grid: dict[str, Any]
    booster: Any
    features: list[str]
    aoa_threshold: float
    dissimilarity_path: Path | None
    era5_fetcher: Callable[[pd.DataFrame, str, str], pd.DataFrame]

    @classmethod
    def from_config(cls, cfg: dict[str, Any], era5_fetcher=None) -> "Service":
        import lightgbm as lgb

        from . import grid as grid_mod

        p = cfg["paths"]
        model_path = Path(p["model_lightgbm"])
        coll = Path(p["catalog_dir"]) / "collection.json"
        thr = json.loads(coll.read_text())["summaries"]["model"]["aoa_threshold"] if coll.exists() else None
        di = Path(p["catalog_dir"]) / "dissimilarity_index.tif"
        return cls(cfg=cfg, grid=grid_mod.load(p["grid_features"]),
                   booster=lgb.Booster(model_file=str(model_path)),
                   features=json.loads(model_path.with_suffix(".features.json").read_text()),
                   aoa_threshold=thr, dissimilarity_path=di if di.exists() else None,
                   era5_fetcher=era5_fetcher or fetch_era5_open_meteo(cfg))

    def site(self, lon: float, lat: float) -> dict[str, float]:
        from .grid import sample_at

        lon0, lat0, lon1, lat1 = self.cfg["bbox"]
        if not (lon0 <= lon <= lon1 and lat0 <= lat <= lat1):
            raise PointError(f"({lat}, {lon}) is outside the mapped area {self.cfg['bbox']} (lon/lat)")
        return {k: float(v[0]) for k, v in sample_at(self.grid, np.array([lon]), np.array([lat])).items()}

    def dissimilarity(self, lon: float, lat: float) -> float | None:
        if self.dissimilarity_path is None:
            return None
        import rasterio
        from rasterio.warp import transform as warp_points

        with rasterio.open(self.dissimilarity_path) as src:
            xs, ys = warp_points("EPSG:4326", src.crs, [lon], [lat])
            v = float(next(src.sample([(xs[0], ys[0])]))[0])
            return None if v == src.nodata or not np.isfinite(v) else v

    def predict(self, lat: float, lon: float, time: str) -> dict[str, Any]:
        t = parse_time(time)
        site = self.site(lon, lat)
        if site.get("frac_water_100m", 0) > self.cfg["catalog"]["water_threshold"]:
            raise PointError("The point is on water; the model covers land only")
        cells = era5_cells(self.cfg)
        weights = cell_weights(lon, lat, cells)
        local_day = t.tz_convert(LOCAL_TZ).date()
        start = str(pd.Timestamp(local_day) - pd.Timedelta(days=1))[:10]
        end = str(pd.Timestamp(local_day) + pd.Timedelta(days=1))[:10]
        sub = cells[cells.station.isin([s for s, _ in weights])]
        e = era5_context(self.era5_fetcher(sub, start, end))
        e = e[e.time == t].set_index("station")
        if e.empty:
            raise PointError(f"No ERA5 data for {t.isoformat()} (the archive lags real time by several days)")

        row = dict(site)
        for col in WEATHER_COLS:
            if col in e:
                row[col] = float(sum(w * e.loc[s, col] for s, w in weights))
        row["elev_minus_cell_m"] = row["elev_dem_m"] - row["era5_cell_elev_m"]
        local = t.tz_convert(LOCAL_TZ)
        row["hour_local"], row["doy"] = local.hour, local.dayofyear
        X = add_time_features(pd.DataFrame([row]))
        corr = float(self.booster.predict(X[self.features].to_numpy(np.float64))[0])
        di = self.dissimilarity(lon, lat)
        return {
            "lat": lat, "lon": lon,
            "time_utc": t.isoformat(), "time_local": local.isoformat(),
            "era5_c": round(row["t_era5"], 2),
            "correction_c": round(corr, 2),
            "temperature_c": round(row["t_era5"] + corr, 2),
            "dissimilarity_index": None if di is None else round(di, 3),
            "in_area_of_applicability": None if di is None or self.aoa_threshold is None else di <= self.aoa_threshold,
            "site": {k: round(row[k], 3) for k in SITE if k in row},
            "weather": {k: round(row[k], 2) for k in WEATHER_COLS if k in row},
        }


def fetch_era5_open_meteo(cfg: dict[str, Any]):
    """ERA5 fetcher with an in-memory cache (one request per set of cells and day)."""
    from . import era5

    @lru_cache(maxsize=256)
    def _cached(stations: tuple, lats: tuple, lons: tuple, start: str, end: str) -> pd.DataFrame:
        df = pd.DataFrame({"station": stations, "lat": lats, "lon": lons})
        return era5.fetch(df, {**cfg, "period": {"start": start, "end": end}})

    def fetch(cells: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
        return _cached(tuple(cells.station), tuple(cells.lat), tuple(cells.lon), start, end).copy()

    return fetch
