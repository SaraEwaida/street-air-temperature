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
        cb0 = fig.colorbar(im, ax=ax, shrink=0.45, pad=0.12, location="bottom", aspect=30)
        cb0.set_label("Built-up fraction (WorldCover, 300 m)")
    v = s.era5_bias_night_c.to_numpy()
    lim = max(float(np.nanmax(np.abs(v))) if np.isfinite(v).any() else 1.0, 0.5)
    xs, ys = np.array(xs) / 1000, np.array(ys) / 1000
    net = s.network.fillna("") if "network" in s else pd.Series([""] * len(s))
    norm = TwoSlopeNorm(0, -lim, lim)
    sc = None
    for name, marker, label in (("noaa-isd", "o", "NOAA airport station"), ("azmet", "s", "AZMet station"),
                                ("", "o", None)):
        m = (net == name).to_numpy()
        if m.any():
            sc = ax.scatter(xs[m], ys[m], c=v[m], cmap="RdBu_r", norm=norm, marker=marker, s=90,
                            edgecolor="black", linewidth=0.8, zorder=3, label=label)
    if (net != "").any():
        from matplotlib.lines import Line2D

        handles = [Line2D([], [], marker=mk, linestyle="", markerfacecolor="#cccccc", markeredgecolor="black",
                          markersize=8, label=lab)
                   for name, mk, lab in (("noaa-isd", "o", "NOAA airport station"), ("azmet", "s", "AZMet station"))
                   if (net == name).any()]
        ax.legend(handles=handles, loc="best", fontsize=8, framealpha=0.9)
    # Airports labelled above-right, AZMet below-right, so co-located pairs (e.g. Buckeye) stay readable
    for x, y, lab, n in zip(xs, ys, s.icao.fillna(s.station.str[-5:]), net):
        off = (6, -11) if n == "azmet" else (6, 5)
        ax.annotate(str(lab), (x, y), xytext=off, textcoords="offset points", fontsize=8, clip_on=False,
                    bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.7))
    ax.margins(0.04)
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


METHOD_STYLE = {   # fixed colour per method, same in every figure
    "era5_raw": ("Raw ERA5", "#7f7f7f", "-"),
    "era5_lapse": ("ERA5 + lapse rate", "#7f7f7f", ":"),
    "era5_bias": ("ERA5 + constant offset", "#a6761d", "--"),
    "ridge": ("Ridge", "#1b6ca8", "-"),
    "lightgbm": ("LightGBM", "#d95f02", "-"),
}


def error_by_hour(by_hour: pd.DataFrame, out: str | Path) -> Path:
    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    for m, (label, color, ls) in METHOD_STYLE.items():
        g = by_hour[by_hour.method == m].sort_values("hour_local")
        if len(g):
            ax.plot(g.hour_local, g.mae_c, color=color, ls=ls, lw=2, label=label)
    ax.axvspan(-0.5, 6.5, color="#eeeeee", zorder=0)
    ax.axvspan(18.5, 23.5, color="#eeeeee", zorder=0)
    ax.set_xlim(-0.5, 23.5)
    ax.set_xticks(range(0, 24, 3))
    ax.set_xlabel("Hour of day (MST); shaded = night")
    ax.set_ylabel("MAE on held-out stations (°C)")
    ax.set_ylim(bottom=0)
    ax.grid(axis="y", color="#dddddd", lw=0.8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(fontsize=8, frameon=False, ncol=2)
    ax.set_title("Leave-one-station-out error by hour, Phoenix 2023", loc="left", fontsize=11)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


def error_by_station(by_station: pd.DataFrame, out: str | Path, methods=("era5_raw", "ridge", "lightgbm")) -> Path:
    piv = by_station.pivot_table(index=["icao", "network"], columns="method", values="mae_c").reset_index()
    piv = piv.sort_values(["network", methods[0]], ascending=[False, False])
    y = np.arange(len(piv))
    h = 0.8 / len(methods)
    fig, ax = plt.subplots(figsize=(7.5, 0.5 * len(piv) + 1.2))
    for i, m in enumerate(methods):
        label, color, _ = METHOD_STYLE[m]
        ax.barh(y + (i - (len(methods) - 1) / 2) * h, piv[m], height=h - 0.03, color=color, label=label)
    ax.set_yticks(y, [f"{a} ({'AZMet' if n == 'azmet' else 'NOAA'})" for a, n in zip(piv.icao, piv.network)], fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("MAE when the station is held out (°C)")
    ax.grid(axis="x", color="#dddddd", lw=0.8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(fontsize=8, frameon=False, loc="lower right")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


def catalog_maps(maps: dict, times: list, grid: dict, out: str | Path, stations: pd.DataFrame | None = None) -> Path:
    """For each time: raw ERA5 and corrected temperature side by side on one shared scale."""
    from .dataset import LOCAL_TZ

    t_, (h, w) = grid["transform"], grid["shape"]
    ext = [t_.c / 1000, (t_.c + w * t_.a) / 1000, (t_.f + h * t_.e) / 1000, t_.f / 1000]
    cmap = plt.get_cmap("inferno").with_extremes(bad="#d9d9d9")
    fig, axes = plt.subplots(len(times), 2, figsize=(12, 3.6 * len(times)), squeeze=False,
                             sharex=True, sharey=True, constrained_layout=True)
    for row, tm in zip(axes, times):
        m = maps[tm]
        both = np.concatenate([m["era5"][np.isfinite(m["era5"])], m["temperature"][np.isfinite(m["temperature"])]])
        vmin, vmax = np.percentile(both, [1, 99])
        label = tm.tz_convert(LOCAL_TZ).strftime("%d %b %Y, %H:%M MST")
        for ax, key, title in zip(row, ("era5", "temperature"), ("Raw ERA5 (~25 km)", "Corrected (100 m)")):
            im = ax.imshow(m[key], extent=ext, cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest")
            ax.set_title(f"{title}: {label}", loc="left", fontsize=10)
            ax.set_aspect("equal")
            if stations is not None:
                from rasterio.warp import transform as warp_points

                xs, ys = warp_points("EPSG:4326", grid["crs"], stations.lon.tolist(), stations.lat.tolist())
                ax.scatter(np.array(xs) / 1000, np.array(ys) / 1000, s=10, c="white", edgecolor="black", lw=0.5)
        cb = fig.colorbar(im, ax=row, shrink=0.85, pad=0.01)
        cb.set_label("2 m air temperature (°C)")
    for ax in axes[-1]:
        ax.set_xlabel("Easting (km, UTM 12N)")
    for ax in axes[:, 0]:
        ax.set_ylabel("Northing (km)")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out
