"""ERA5 2 m air temperature at each station from the Open-Meteo historical archive.

`elevation=nan` returns the raw grid-cell value (~25 km, grid-cell mean height). Without it
the API applies its own lapse-rate correction to the point's elevation, which would hide
part of what the model is meant to learn. That correction is computed separately instead.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def build_params(stations: pd.DataFrame, cfg: dict[str, Any]) -> dict[str, str]:
    e = cfg["era5"]
    p = {
        "latitude": ",".join(f"{v:.5f}" for v in stations.lat),
        "longitude": ",".join(f"{v:.5f}" for v in stations.lon),
        "start_date": cfg["period"]["start"],
        "end_date": cfg["period"]["end"],
        "hourly": "temperature_2m",
        "models": e["model"],
        "timezone": "GMT",
    }
    if e.get("raw_grid_cell", True):
        p["elevation"] = ",".join("nan" for _ in range(len(stations)))
    return p


def parse_response(payload: Any, stations: pd.DataFrame) -> pd.DataFrame:
    """Open-Meteo returns one object per location (a list when there are several)."""
    items = payload if isinstance(payload, list) else [payload]
    if len(items) != len(stations):
        raise ValueError(f"expected {len(stations)} locations, got {len(items)}")
    frames = []
    for st, item in zip(stations.station, items):
        h = item["hourly"]
        frames.append(pd.DataFrame({
            "station": st,
            "time": pd.to_datetime(h["time"], utc=True),
            "t_era5": pd.to_numeric(pd.Series(h["temperature_2m"]), errors="coerce"),
            "era5_cell_elev_m": item.get("elevation"),
        }))
    return pd.concat(frames, ignore_index=True).dropna(subset=["t_era5"])


def fetch(stations: pd.DataFrame, cfg: dict[str, Any], chunk: int = 25) -> pd.DataFrame:
    import requests

    out = []
    for i in range(0, len(stations), chunk):
        part = stations.iloc[i:i + chunk]
        r = requests.get(ARCHIVE_URL, params=build_params(part, cfg), timeout=300)
        r.raise_for_status()
        out.append(parse_response(r.json(), part))
    return pd.concat(out, ignore_index=True)
