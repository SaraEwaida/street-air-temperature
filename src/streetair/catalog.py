"""Air-temperature maps for selected hours, written as Cloud-Optimised GeoTIFFs and indexed
in a STAC catalog.

For each hour, every land pixel gets: ERA5 temperature of its 0.25 deg cell (the same raw
grid-cell value the stations were compared with) plus the LightGBM correction predicted from
the pixel's surroundings, the hour, and the cell's ERA5 weather.

Each item has three assets:
- temperature: corrected 2 m air temperature (degC)
- era5: the raw ERA5 value on the same grid, for comparison (degC)
- correction: temperature minus ERA5 (degC)
The collection has two static assets: `area_of_applicability` (1 where a pixel's surroundings
are no more unusual than a held-out station's were during validation, after Meyer & Pebesma
2021) and `dissimilarity_index`, the underlying distance.
Water pixels (WorldCover water fraction within 100 m above a threshold) are nodata.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .dataset import LOCAL_TZ, era5_context
from .grid import pixel_lonlat
from .model import SITE, add_time_features

NODATA = -9999.0
ERA5_STEP = 0.25


def era5_cells(cfg: dict[str, Any]) -> pd.DataFrame:
    """Centres of the ERA5 0.25 deg cells covering the bbox."""
    lon0, lat0, lon1, lat1 = cfg["bbox"]
    lons = np.arange(np.floor(lon0 / ERA5_STEP), np.ceil(lon1 / ERA5_STEP) + 1) * ERA5_STEP
    lats = np.arange(np.floor(lat0 / ERA5_STEP), np.ceil(lat1 / ERA5_STEP) + 1) * ERA5_STEP
    LO, LA = np.meshgrid(lons, lats)
    return pd.DataFrame({"station": [f"cell_{i:03d}" for i in range(LO.size)],
                         "lat": LA.ravel().round(3), "lon": LO.ravel().round(3)})


def pixel_cells(grid: dict[str, Any], cells: pd.DataFrame) -> np.ndarray:
    """Index into `cells` of the nearest ERA5 cell centre for every pixel."""
    lon, lat = pixel_lonlat(grid["transform"], grid["shape"], grid["crs"])
    key = {(round(a, 3), round(b, 3)): i for i, (a, b) in enumerate(zip(cells.lon, cells.lat))}
    snap = lambda v: np.round(np.round(v / ERA5_STEP) * ERA5_STEP, 3)  # noqa: E731
    sl, sa = snap(lon), snap(lat)
    return np.vectorize(lambda a, b: key[(a, b)])(sl, sa).astype(np.int32)


def local_times(cfg: dict[str, Any]) -> list[pd.Timestamp]:
    return [pd.Timestamp(t).tz_localize(LOCAL_TZ).tz_convert("UTC") for t in cfg["catalog"]["times_local"]]


def fetch_era5_cells(cfg: dict[str, Any], cells: pd.DataFrame, times: list[pd.Timestamp]) -> pd.DataFrame:
    """ERA5 for every cell over the days around each map time, with the same context features
    (3-hour change, local-day mean) as the station table."""
    from . import era5

    frames = []
    for day in sorted({t.tz_convert(LOCAL_TZ).date() for t in times}):
        c = {**cfg, "period": {"start": str(pd.Timestamp(day) - pd.Timedelta(days=1))[:10],
                               "end": str(pd.Timestamp(day) + pd.Timedelta(days=1))[:10]}}
        frames.append(era5.fetch(cells, c))
    e = pd.concat(frames, ignore_index=True).drop_duplicates(["station", "time"])
    return era5_context(e)


def training_ranges(site_features: pd.DataFrame, dataset: pd.DataFrame) -> dict[str, tuple[float, float]]:
    rng = {c: (float(site_features[c].min()), float(site_features[c].max())) for c in SITE if c in site_features}
    e = dataset.groupby("station").elev_minus_cell_m.first()
    rng["elev_minus_cell_m"] = (float(e.min()), float(e.max()))
    return rng


# Absolute margins around the training stations' range. A relative margin fails for features
# that barely vary across 15 stations (water within 500 m spans 0-0.013): a pixel next to a canal
# would count as extrapolation.
MARGINS = {"frac": 0.05, "ndvi": 0.05, "elev_minus_cell_m": 25.0}


def _margin(name: str) -> float:
    return next((v for k, v in MARGINS.items() if name.startswith(k)), 0.0)


def support_by_feature(feats: dict[str, np.ndarray], ranges: dict[str, tuple[float, float]],
                       land: np.ndarray) -> pd.DataFrame:
    """Share of land pixels inside each feature's training range (missing values count as inside)."""
    rows = []
    for k, (lo, hi) in ranges.items():
        if k in feats:
            v, pad = feats[k], _margin(k)
            ok = ~np.isfinite(v) | ((v >= lo - pad) & (v <= hi + pad))
            rows.append({"feature": k, "train_min": round(lo, 3), "train_max": round(hi, 3), "margin": pad,
                         "land_share_in_range": round(float(ok[land].mean()), 3)})
    return pd.DataFrame(rows).sort_values("land_share_in_range")


def support_mask(feats: dict[str, np.ndarray], ranges: dict[str, tuple[float, float]]) -> np.ndarray:
    """True where every site feature lies within the training stations' range plus an absolute margin.
    Missing values (e.g. NDVI outside the Landsat scenes) are not counted as extrapolation."""
    ok = np.ones(next(iter(feats.values())).shape, bool)
    for k, (lo, hi) in ranges.items():
        if k in feats:
            v, pad = feats[k], _margin(k)
            ok &= ~np.isfinite(v) | ((v >= lo - pad) & (v <= hi + pad))
    return ok


def area_of_applicability(train: pd.DataFrame, grid_feats: dict[str, np.ndarray], weights: dict[str, float],
                          chunk: int = 200_000) -> tuple[np.ndarray, float, np.ndarray]:
    """Dissimilarity index (DI) and area of applicability, after Meyer & Pebesma (2021),
    "Predicting into unknown space? Estimating the area of applicability of spatial prediction
    models", Methods in Ecology and Evolution 12(9).

    Features are standardised with the training stations' mean and standard deviation and
    weighted by model importance. DI = distance to the nearest training station divided by the
    mean distance between training stations. The threshold is the upper whisker (75th
    percentile + 1.5 IQR, capped at the maximum) of the training stations' own DI to their
    nearest *other* station, which mirrors leave-one-station-out validation.
    Missing pixel values are set to the training mean (neutral).

    Returns (DI map, threshold, training DI per station).
    """
    cols = [c for c in train.columns if c in grid_feats and c in weights]
    X = train[cols].to_numpy(float)
    mu, sd = np.nanmean(X, axis=0), np.nanstd(X, axis=0)
    sd[sd == 0] = 1.0
    w = np.array([weights[c] for c in cols], float)
    w = w / w.sum() if w.sum() > 0 else np.ones_like(w) / len(w)
    Z = np.nan_to_num((X - mu) / sd) * w
    D = np.sqrt(((Z[:, None, :] - Z[None, :, :]) ** 2).sum(-1))
    n = len(Z)
    d_bar = D[~np.eye(n, dtype=bool)].mean()
    np.fill_diagonal(D, np.inf)
    di_train = D.min(axis=1) / d_bar
    q1, q3 = np.percentile(di_train, [25, 75])
    thr = float(min(q3 + 1.5 * (q3 - q1), di_train.max()))

    shape = next(iter(grid_feats.values())).shape
    G = np.stack([grid_feats[c].ravel().astype(float) for c in cols], axis=1)
    di = np.empty(G.shape[0], np.float32)
    for i in range(0, len(G), chunk):
        g = np.nan_to_num((G[i:i + chunk] - mu) / sd) * w
        di[i:i + chunk] = np.sqrt(((g[:, None, :] - Z[None, :, :]) ** 2).sum(-1)).min(axis=1) / d_bar
    return di.reshape(shape), thr, di_train


def predict_hour(grid: dict[str, Any], cell_idx: np.ndarray, era5_t: pd.DataFrame, time_utc: pd.Timestamp,
                 booster, features: list[str], land: np.ndarray) -> dict[str, np.ndarray]:
    """Temperature, ERA5 and correction maps for one hour. era5_t: one row per cell for this hour."""
    shape = grid["shape"]
    w = era5_t.set_index("station").reindex([f"cell_{i:03d}" for i in range(cell_idx.max() + 1)])
    rows = np.nonzero(land.ravel())[0]
    ci = cell_idx.ravel()[rows]
    df = pd.DataFrame({k: v.ravel()[rows] for k, v in grid["features"].items()})
    for col in ("t_era5", "t_era5_change_3h", "t_era5_day_mean", "era5_wind_ms", "era5_cloud_pct",
                "era5_sw_wm2", "era5_rh_pct", "era5_cell_elev_m"):
        if col in w:
            df[col] = w[col].to_numpy()[ci]
    df["elev_minus_cell_m"] = df.elev_dem_m - df.era5_cell_elev_m
    local = time_utc.tz_convert(LOCAL_TZ)
    df["hour_local"], df["doy"] = local.hour, local.dayofyear
    df = add_time_features(df)
    corr = booster.predict(df[features].to_numpy(np.float64))
    out = {}
    for name, vals in (("era5", df.t_era5.to_numpy()), ("correction", corr), ("temperature", df.t_era5.to_numpy() + corr)):
        a = np.full(shape[0] * shape[1], np.nan, np.float32)
        a[rows] = vals
        out[name] = a.reshape(shape)
    return out


def write_cog(path: str | Path, arr: np.ndarray, grid: dict[str, Any], dtype="float32", nodata=NODATA) -> Path:
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.shutil import copy as rio_copy

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.where(np.isfinite(arr), arr, nodata).astype(dtype) if dtype == "float32" else arr.astype(dtype)
    prof = dict(driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype=dtype,
                crs=grid["crs"], transform=grid["transform"], nodata=nodata)
    with MemoryFile() as mem:
        with mem.open(**prof) as tmp:
            tmp.write(data, 1)
        with mem.open() as src:
            rio_copy(src, str(path), driver="COG", compress="DEFLATE", overview_resampling="average")
    with rasterio.open(path) as chk:   # fail early if GDAL did not write a valid tiled file
        assert chk.driver == "GTiff" and chk.profile.get("tiled", False)
    return path


def _bbox_geometry(bbox):
    lon0, lat0, lon1, lat1 = bbox
    return {"type": "Polygon", "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}


PROJ_EXT = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"
RASTER_EXT = "https://stac-extensions.github.io/raster/v1.1.0/schema.json"


def build_catalog(cfg: dict[str, Any], grid: dict[str, Any], maps: dict[pd.Timestamp, dict[str, np.ndarray]],
                  in_range: np.ndarray, root: str | Path, model_info: dict[str, Any],
                  dissimilarity: np.ndarray | None = None) -> Path:
    import pystac

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    times = sorted(maps)
    proj = {"proj:code": grid["crs"], "proj:shape": list(grid["shape"]), "proj:transform": list(grid["transform"])[:6]}
    band = lambda desc: {"raster:bands": [{"data_type": "float32", "nodata": NODATA, "unit": "degC",  # noqa: E731
                                           "description": desc}]}

    coll = pystac.Collection(
        id=f"{cfg['city']}-air-temperature",
        title=f"{cfg['city'].title()} 2 m air temperature, {int(cfg['catalog']['res_m'])} m",
        description="ERA5 2 m air temperature corrected to street level by a LightGBM model trained on "
                    "15 weather stations (NOAA ISD and AZMet) and validated leave-one-station-out. "
                    "See https://github.com/SaraEwaida/street-air-temperature.",
        extent=pystac.Extent(pystac.SpatialExtent([cfg["bbox"]]),
                             pystac.TemporalExtent([[times[0].to_pydatetime(), times[-1].to_pydatetime()]])),
        license="CC-BY-4.0",
        keywords=["air temperature", "urban heat", "downscaling", "ERA5", cfg["city"]],
        extra_fields={"summaries": {"model": model_info}},
    )
    coll.stac_extensions = [PROJ_EXT, RASTER_EXT]
    write_cog(root / "area_of_applicability.tif", in_range.astype(np.uint8), grid, dtype="uint8", nodata=255)
    coll.add_asset("area_of_applicability", pystac.Asset(
        href=str((root / "area_of_applicability.tif").resolve()), media_type=pystac.MediaType.COG, roles=["metadata"],
        title="1 = inside the model's area of applicability (Meyer & Pebesma 2021), 0 = outside"))
    if dissimilarity is not None:
        write_cog(root / "dissimilarity_index.tif", dissimilarity, grid)
        coll.add_asset("dissimilarity_index", pystac.Asset(
            href=str((root / "dissimilarity_index.tif").resolve()), media_type=pystac.MediaType.COG,
            roles=["metadata"], title="Dissimilarity of each pixel's surroundings to the nearest training station",
            extra_fields={"aoa_threshold": model_info.get("aoa_threshold")}))

    for t in times:
        tid = t.tz_convert(LOCAL_TZ).strftime("%Y%m%dT%H%M")
        item_id = f"{cfg['city']}-air-temperature-{tid}"
        item_dir = root / item_id                     # pystac's default layout: <root>/<item id>/<item id>.json
        item = pystac.Item(id=item_id, geometry=_bbox_geometry(cfg["bbox"]), bbox=list(cfg["bbox"]),
                           datetime=t.to_pydatetime(),
                           properties={**proj, "local_time": t.tz_convert(LOCAL_TZ).isoformat()},
                           stac_extensions=[PROJ_EXT, RASTER_EXT])
        for name, desc in (("temperature", "Corrected 2 m air temperature"), ("era5", "Raw ERA5 2 m air temperature"),
                           ("correction", "Temperature minus ERA5")):
            path = write_cog(item_dir / f"{name}.tif", maps[t][name], grid)
            item.add_asset(name, pystac.Asset(href=str(path.resolve()), media_type=pystac.MediaType.COG,
                                              roles=["data"], title=desc, extra_fields=band(desc)))
        coll.add_item(item)
    coll.normalize_hrefs(str(root.resolve()))
    coll.make_all_asset_hrefs_relative()          # portable: the catalog folder can be moved or uploaded as is
    coll.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
    return root / "collection.json"


def summary(maps: dict[pd.Timestamp, dict[str, np.ndarray]], in_range: np.ndarray) -> pd.DataFrame:
    rows = []
    for t, m in sorted(maps.items()):
        c = m["correction"]
        ok = np.isfinite(c)
        rows.append({"local_time": t.tz_convert(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
                     "era5_mean_c": float(np.nanmean(m["era5"])), "temperature_mean_c": float(np.nanmean(m["temperature"])),
                     "correction_p5_c": float(np.nanpercentile(c, 5)), "correction_p95_c": float(np.nanpercentile(c, 95)),
                     "spread_p95_p5_c": float(np.nanpercentile(m["temperature"], 95) - np.nanpercentile(m["temperature"], 5)),
                     "land_pixels": int(ok.sum()), "in_aoa": float(in_range[ok].mean())})
    return pd.DataFrame(rows).round(2)


def save_model_info(path: str | Path, info: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(info, indent=2))
