"""Site features around each station from Microsoft Planetary Computer.

For every station, small windows are warped to a local metric grid centred on it:
- ESA WorldCover (10 m): class fractions inside circles of each radius,
- Copernicus DEM GLO-30: mean elevation within 100 m,
- Landsat Collection 2 Level-2: summer median NDVI inside each radius, clear pixels only.
Only the windows are read, never whole scenes. No station coordinates are kept as features.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform as warp_points

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

LC_GROUPS = {
    "built": [50],
    "tree": [10],
    "shrub": [20],
    "grass_crop": [30, 40],
    "bare": [60],
    "water": [80],
}
QA_BAD_BITS = (0, 1, 2, 3, 4, 5)   # fill, dilated cloud, cirrus, cloud, shadow, snow
SR_SCALE, SR_OFFSET = 0.0000275, -0.2


def _open(href: str):
    if href.startswith("http"):
        import planetary_computer

        href = planetary_computer.sign(href)
    return rasterio.open(href)


def window_grid(lon: float, lat: float, radius_m: float, res_m: float, crs: str):
    """Transform and shape of a square grid centred on a point, in a metric CRS."""
    xs, ys = warp_points("EPSG:4326", crs, [lon], [lat])
    n = int(np.ceil(2 * radius_m / res_m))
    return from_origin(xs[0] - n * res_m / 2, ys[0] + n * res_m / 2, res_m, res_m), (n, n)


def circle_mask(shape: tuple[int, int], res_m: float, radius_m: float) -> np.ndarray:
    n = shape[0]
    c = (np.arange(n) + 0.5) * res_m - n * res_m / 2
    return (c[:, None] ** 2 + c[None, :] ** 2) <= radius_m ** 2


def read_window(hrefs: list[str], transform, shape, crs: str, resampling: Resampling,
                nodata: float, dtype) -> np.ndarray:
    """Warp one or more tiles onto the window; the first valid value wins."""
    out = np.full(shape, nodata, dtype=dtype)
    for href in hrefs:
        with _open(href) as src:
            src_nodata = src.nodata if src.nodata is not None else nodata
            with WarpedVRT(src, crs=crs, transform=transform, width=shape[1], height=shape[0],
                           resampling=resampling, nodata=src_nodata) as vrt:
                a = vrt.read(1).astype(dtype)
        fill = (out == nodata) & (a != src_nodata)
        out[fill] = a[fill]
    return out


def landcover_fractions(lc: np.ndarray, res_m: float, radii: list[float]) -> dict[str, float]:
    out = {}
    valid = lc != 0
    for r in radii:
        m = circle_mask(lc.shape, res_m, r) & valid
        n = max(int(m.sum()), 1)
        for name, codes in LC_GROUPS.items():
            out[f"frac_{name}_{int(r)}m"] = float(np.isin(lc[m], codes).sum() / n)
    return out


def landsat_ndvi(red: np.ndarray, nir: np.ndarray, qa: np.ndarray) -> np.ndarray:
    bad = np.zeros(qa.shape, bool)
    for b in QA_BAD_BITS:
        bad |= ((qa.astype(np.uint32) >> b) & 1) == 1
    r = red.astype(np.float32) * SR_SCALE + SR_OFFSET
    n = nir.astype(np.float32) * SR_SCALE + SR_OFFSET
    with np.errstate(divide="ignore", invalid="ignore"):
        v = (n - r) / (n + r)
    return np.where(~bad & (red > 0) & (nir > 0) & np.isfinite(v), np.clip(v, -1, 1), np.nan)


def _items(cfg: dict[str, Any]):
    import planetary_computer
    import pystac_client

    client = pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)
    bbox = cfg["bbox"]
    wc = list(client.search(collections=["esa-worldcover"], bbox=bbox).items())
    latest = max(i.datetime or pd.Timestamp(i.properties["start_datetime"]) for i in wc)
    wc = [i for i in wc if (i.datetime or pd.Timestamp(i.properties["start_datetime"])) == latest]
    dem = list(client.search(collections=["cop-dem-glo-30"], bbox=bbox).items())
    f = cfg["features"]
    ls = list(client.search(collections=["landsat-c2-l2"], bbox=bbox,
                            datetime="/".join(f["ndvi_period"]),
                            query={"eo:cloud_cover": {"lt": f["ndvi_max_cloud"]},
                                   "platform": {"in": ["landsat-8", "landsat-9"]}}).items())
    ls.sort(key=lambda i: i.properties["eo:cloud_cover"])
    return wc, dem, ls


def _contains(item, lon: float, lat: float) -> bool:
    from matplotlib.path import Path as MplPath

    g = item.geometry
    polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    return any(MplPath(np.asarray(p[0])[:, :2]).contains_point((lon, lat)) for p in polys)


def site_features(stations: pd.DataFrame, cfg: dict[str, Any], verbose: bool = True) -> pd.DataFrame:
    crs = cfg["crs"]
    radii = [float(r) for r in cfg["features"]["radii_m"]]
    rmax = max(radii)
    wc, dem, ls = _items(cfg)
    unsigned = lambda h: h.split("?")[0]  # noqa: E731
    wc_hrefs = [unsigned(i.assets["map"].href) for i in wc]
    dem_hrefs = [unsigned(i.assets["data"].href) for i in dem]

    rows = []
    for st in stations.itertuples():
        row = {"station": st.station}
        t10, s10 = window_grid(st.lon, st.lat, rmax, 10, crs)
        lc = read_window(wc_hrefs, t10, s10, crs, Resampling.nearest, 0, np.uint8)
        row.update(landcover_fractions(lc, 10, radii))

        t30, s30 = window_grid(st.lon, st.lat, rmax, 30, crs)
        z = read_window(dem_hrefs, t30, s30, crs, Resampling.bilinear, -32767.0, np.float32)
        z = np.where(z == -32767.0, np.nan, z)
        row["elev_dem_m"] = float(np.nanmean(z[circle_mask(s30, 30, 100)]))

        per_scene = {r: [] for r in radii}
        for item in (i for i in ls if _contains(i, st.lon, st.lat)):
            bands = [read_window([unsigned(item.assets[k].href)], t30, s30, crs, Resampling.nearest, 0, np.uint16)
                     for k in ("red", "nir08", "qa_pixel")]
            ndvi = landsat_ndvi(*bands)
            for r in radii:
                v = ndvi[circle_mask(s30, 30, r)]
                if np.isfinite(v).mean() > 0.8:
                    per_scene[r].append(float(np.nanmean(v)))
            if min(len(v) for v in per_scene.values()) >= cfg["features"]["ndvi_scenes"]:
                break
        for r in radii:
            row[f"ndvi_{int(r)}m"] = float(np.median(per_scene[r])) if per_scene[r] else np.nan
            row[f"ndvi_scenes_{int(r)}m"] = len(per_scene[r])
        rows.append(row)
        if verbose:
            print(f"  {st.station} {st.name[:28]:<28} built500={row['frac_built_500m']:.2f} "
                  f"ndvi500={row['ndvi_500m']:.2f} elev={row['elev_dem_m']:.0f} m", flush=True)
    return pd.DataFrame(rows)
