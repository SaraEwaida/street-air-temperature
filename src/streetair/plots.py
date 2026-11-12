"""Station map: built-up land in the background, stations coloured by night-time ERA5 error."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import TwoSlopeNorm  # noqa: E402


def built_background(cfg: dict[str, Any], res_m: float = 300.0):
    """Built-up fraction at res_m over the bbox, from WorldCover read at 50 m."""
    import pystac_client
    import planetary_computer
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin
    from rasterio.warp import transform_bounds

    from .features import STAC_URL, read_window

    client = pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)
    items = list(client.search(collections=["esa-worldcover"], bbox=cfg["bbox"]).items())
    latest = max(i.datetime or pd.Timestamp(i.properties["start_datetime"]) for i in items)
    hrefs = [i.assets["map"].href.split("?")[0] for i in items
             if (i.datetime or pd.Timestamp(i.properties["start_datetime"])) == latest]
    left, bottom, right, top = transform_bounds("EPSG:4326", cfg["crs"], *cfg["bbox"])
    k = int(res_m // 50)
    w = int((right - left) // res_m) * k
    h = int((top - bottom) // res_m) * k
    lc = read_window(hrefs, from_origin(left, top, 50, 50), (h, w), cfg["crs"], Resampling.nearest, 0, np.uint8)
    frac = (lc == 50).reshape(h // k, k, w // k, k).mean(axis=(1, 3))
    return frac, (left, left + w * 50, top - h * 50, top)


def station_map(summary: pd.DataFrame, stations: pd.DataFrame, cfg: dict[str, Any], out: str | Path,
                background=None) -> Path:
    from rasterio.warp import transform as warp_points

    s = summary.merge(stations[["station", "lat", "lon"]], on="station")
    xs, ys = warp_points("EPSG:4326", cfg["crs"], s.lon.tolist(), s.lat.tolist())
    fig, ax = plt.subplots(figsize=(10, 6.2))
    if background is not None:
        frac, ext = background
        im = ax.imshow(frac, extent=[e / 1000 for e in ext], cmap="Greys", vmin=0, vmax=1, alpha=0.75)
        cb0 = fig.colorbar(im, ax=ax, shrink=0.6, pad=0.01, location="left")
        cb0.set_label("Built-up fraction (WorldCover)")
    v = s.era5_bias_night_c.to_numpy()
    lim = max(float(np.nanmax(np.abs(v))) if np.isfinite(v).any() else 1.0, 0.5)
    sc = ax.scatter(np.array(xs) / 1000, np.array(ys) / 1000, c=v, cmap="RdBu_r",
                    norm=TwoSlopeNorm(0, -lim, lim), s=90, edgecolor="black", linewidth=0.8, zorder=3)
    for x, y, lab in zip(xs, ys, s.icao.fillna(s.station.str[-5:])):
        ax.annotate(str(lab), (x / 1000, y / 1000), xytext=(5, 4), textcoords="offset points", fontsize=8)
    cb = fig.colorbar(sc, ax=ax, shrink=0.6, pad=0.01)
    cb.set_label("ERA5 minus station, night (°C)\nblue = station warmer than ERA5")
    ax.set_xlabel("Easting (km, UTM 12N)")
    ax.set_ylabel("Northing (km)")
    ax.set_aspect("equal")
    ax.set_title(f"{cfg['city'].title()}: weather stations and night-time ERA5 error, {cfg['period']['start'][:4]}",
                 loc="left", fontsize=11)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out
