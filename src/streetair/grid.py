"""Site features for every pixel of a 100 m grid, matching the station features.

Stations get features from circles of 100 m and 500 m around them (features.py). For the
maps, the same quantities are computed for every pixel:
- WorldCover is read at 10 m, turned into class fractions on a 50 m base grid, averaged
  over discs of 100 m and 500 m radius, then averaged to 100 m.
- Landsat summer NDVI: median over clear scenes at 50 m, disc-averaged the same way.
- Copernicus DEM at 100 m.
The disc averages on a 50 m base approximate the stations' 10 m circles; `sat grid`
reports the difference at the station pixels so the approximation is visible.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import transform as warp_points
from rasterio.warp import transform_bounds

from .features import LC_GROUPS, _items, landsat_ndvi, read_window


def grid_spec(cfg: dict[str, Any], res_m: float):
    """(transform, (height, width)) of a grid over the bbox, snapped to res_m."""
    left, bottom, right, top = transform_bounds("EPSG:4326", cfg["crs"], *cfg["bbox"])
    left, bottom = np.floor(left / res_m) * res_m, np.floor(bottom / res_m) * res_m
    right, top = np.ceil(right / res_m) * res_m, np.ceil(top / res_m) * res_m
    shape = (int(round((top - bottom) / res_m)), int(round((right - left) / res_m)))
    return from_origin(left, top, res_m, res_m), shape


def pixel_lonlat(transform, shape, crs: str):
    h, w = shape
    xs = transform.c + transform.a * (np.arange(w) + 0.5)
    ys = transform.f + transform.e * (np.arange(h) + 0.5)
    X, Y = np.meshgrid(xs, ys)
    lon, lat = warp_points(crs, "EPSG:4326", X.ravel().tolist(), Y.ravel().tolist())
    return np.array(lon).reshape(shape), np.array(lat).reshape(shape)


def block_mean(a: np.ndarray, k: int) -> np.ndarray:
    """Mean of k x k blocks, ignoring NaN (NaN where a block has no valid value)."""
    h, w = a.shape[0] // k * k, a.shape[1] // k * k
    b = a[:h, :w].reshape(h // k, k, w // k, k)
    with np.errstate(invalid="ignore"):
        return np.nanmean(b, axis=(1, 3)) if np.isnan(b).any() else b.mean(axis=(1, 3))


def disk(radius_px: float) -> np.ndarray:
    r = int(np.ceil(radius_px))
    y, x = np.mgrid[-r:r + 1, -r:r + 1]
    return ((x ** 2 + y ** 2) <= radius_px ** 2).astype(np.float64)


def disk_ratio(num: np.ndarray, den: np.ndarray, radius_px: float) -> np.ndarray:
    """sum(num) / sum(den) over a disc around each pixel (zero-padded at the edges)."""
    from scipy.signal import fftconvolve

    k = disk(radius_px)
    n = fftconvolve(num, k, mode="same")
    d = fftconvolve(den, k, mode="same")
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(d > 1e-6, n / d, np.nan)
    return out.astype(np.float32)


def landcover_features(lc10: np.ndarray, radii_m: list[float], base_k: int = 5, out_k: int = 2) -> dict[str, np.ndarray]:
    """Class fractions in discs, from 10 m WorldCover. base_k: 10 m -> 50 m; out_k: 50 m -> 100 m."""
    base_res = 10 * base_k
    h, w = lc10.shape[0] // base_k * base_k, lc10.shape[1] // base_k * base_k
    lc10 = lc10[:h, :w]

    def block_frac(mask: np.ndarray) -> np.ndarray:   # boolean counts: no large float copies
        return mask.reshape(h // base_k, base_k, w // base_k, base_k).sum(axis=(1, 3), dtype=np.int32) / base_k ** 2

    valid = block_frac(lc10 != 0)
    out = {}
    for name, codes in LC_GROUPS.items():
        cls = block_frac(np.isin(lc10, codes))
        for r in radii_m:
            frac = np.clip(disk_ratio(cls, valid, r / base_res), 0, 1)     # FFT round-off can give -1e-16
            out[f"frac_{name}_{int(r)}m"] = block_mean(frac, out_k)
    return out


def ndvi_features(ndvi50: np.ndarray, radii_m: list[float], base_res: float = 50, out_k: int = 2) -> dict[str, np.ndarray]:
    ok = np.isfinite(ndvi50)
    num = np.where(ok, ndvi50, 0.0)
    return {f"ndvi_{int(r)}m": block_mean(disk_ratio(num, ok.astype(np.float64), r / base_res), out_k)
            for r in radii_m}


def static_features(cfg: dict[str, Any], verbose: bool = True) -> dict[str, Any]:
    c = cfg["catalog"]
    res, base = float(c["res_m"]), float(c["base_res_m"])
    out_k, base_k = int(round(res / base)), int(round(base / 10))
    t100, s100 = grid_spec(cfg, res)
    crs = cfg["crs"]
    radii = [float(r) for r in cfg["features"]["radii_m"]]
    wc, dem, ls = _items(cfg)
    unsigned = lambda h: h.split("?")[0]  # noqa: E731

    if verbose:
        print(f"  grid {s100[0]} x {s100[1]} at {res:.0f} m; reading WorldCover at 10 m...", flush=True)
    t10 = from_origin(t100.c, t100.f, 10, 10)
    lc10 = read_window([unsigned(i.assets["map"].href) for i in wc], t10, (s100[0] * out_k * base_k, s100[1] * out_k * base_k),
                       crs, Resampling.nearest, 0, np.uint8)
    feats = landcover_features(lc10, radii, base_k, out_k)
    del lc10

    if verbose:
        print("  Landsat summer NDVI...", flush=True)
    t50 = from_origin(t100.c, t100.f, base, base)
    s50 = (s100[0] * out_k, s100[1] * out_k)
    scenes = []
    for item in ls[: cfg["features"]["ndvi_scenes"] * 2]:
        bands = [read_window([unsigned(item.assets[k].href)], t50, s50, crs, Resampling.nearest, 0, np.uint16)
                 for k in ("red", "nir08", "qa_pixel")]
        v = landsat_ndvi(*bands)
        if np.isfinite(v).mean() > 0.2:
            scenes.append(v)
        if len(scenes) >= cfg["features"]["ndvi_scenes"]:
            break
    with np.errstate(all="ignore"):
        ndvi50 = np.nanmedian(np.stack(scenes), axis=0) if scenes else np.full(s50, np.nan, np.float32)
    feats.update(ndvi_features(ndvi50, radii, base, out_k))

    if verbose:
        print("  Copernicus DEM...", flush=True)
    z = read_window([unsigned(i.assets["data"].href) for i in dem], t100, s100, crs, Resampling.bilinear, -32767.0, np.float32)
    feats["elev_dem_m"] = np.where(z == -32767.0, np.nan, z).astype(np.float32)
    return {"features": feats, "transform": t100, "shape": s100, "crs": crs, "ndvi_scenes": len(scenes)}


def sample_at(grid: dict[str, Any], lon: np.ndarray, lat: np.ndarray) -> dict[str, np.ndarray]:
    """Grid feature values at points (nearest pixel)."""
    xs, ys = warp_points("EPSG:4326", grid["crs"], list(lon), list(lat))
    t, (h, w) = grid["transform"], grid["shape"]
    col = np.clip(((np.array(xs) - t.c) / t.a).astype(int), 0, w - 1)
    row = np.clip(((np.array(ys) - t.f) / t.e).astype(int), 0, h - 1)
    return {k: v[row, col] for k, v in grid["features"].items()}


def save(grid: dict[str, Any], path) -> None:
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **grid["features"], _transform=np.array(list(grid["transform"])[:6]),
                        _shape=np.array(grid["shape"]), _crs=np.array(grid["crs"]))


def load(path) -> dict[str, Any]:
    from affine import Affine

    z = np.load(path)
    return {"features": {k: z[k] for k in z.files if not k.startswith("_")},
            "transform": Affine(*z["_transform"]), "shape": tuple(int(v) for v in z["_shape"]), "crs": str(z["_crs"])}
