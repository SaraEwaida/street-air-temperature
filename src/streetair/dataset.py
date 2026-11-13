"""Join observations, ERA5 and site features into one station-hour table."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

LOCAL_TZ = "America/Phoenix"     # MST all year, no daylight saving
LAPSE_RATE_C_PER_M = -0.0065     # standard atmosphere, for the elevation-corrected baseline


def era5_context(era5: pd.DataFrame) -> pd.DataFrame:
    """Add ERA5-only context, computed on the complete hourly series before joining with stations:
    3-hour temperature change (cooling or warming rate) and the local-day mean."""
    e = era5.sort_values(["station", "time"]).copy()
    g = e.groupby("station").t_era5
    e["t_era5_change_3h"] = g.diff(3)
    day = e.time.dt.tz_convert(LOCAL_TZ).dt.date
    e["t_era5_day_mean"] = e.groupby(["station", day]).t_era5.transform("mean")
    return e


def build(obs: pd.DataFrame, era5: pd.DataFrame, stations: pd.DataFrame, feats: pd.DataFrame) -> pd.DataFrame:
    df = obs.merge(era5_context(era5), on=["station", "time"], how="inner")
    cols = [c for c in ("station", "name", "icao", "network", "lat", "lon", "elev_m") if c in stations]
    df = df.merge(stations[cols], on="station", how="left")
    df = df.merge(feats, on="station", how="left")
    local = df.time.dt.tz_convert(LOCAL_TZ)
    df["hour_local"] = local.dt.hour
    df["doy"] = local.dt.day_of_year
    df["month"] = local.dt.month
    df["is_night"] = ~df.hour_local.between(7, 18)
    # Elevation-corrected ERA5: move the grid-cell temperature to the station's height
    elev = df.elev_dem_m.fillna(df.elev_m)
    df["elev_minus_cell_m"] = elev - df.era5_cell_elev_m
    df["t_era5_elev"] = df.t_era5 + LAPSE_RATE_C_PER_M * df.elev_minus_cell_m
    df["era5_minus_obs"] = df.t_era5 - df.t_obs
    return df.sort_values(["station", "time"]).reset_index(drop=True)


def station_summary(df: pd.DataFrame) -> pd.DataFrame:
    def agg(g: pd.DataFrame) -> pd.Series:
        night, day = g[g.is_night], g[~g.is_night]
        return pd.Series({
            "name": g["name"].iloc[0], "icao": g["icao"].iloc[0],
            "network": g["network"].iloc[0] if "network" in g else "", "hours": len(g),
            "mean_obs_c": g.t_obs.mean(),
            "era5_bias_c": g.era5_minus_obs.mean(),
            "era5_bias_night_c": night.era5_minus_obs.mean() if len(night) else np.nan,
            "era5_bias_day_c": day.era5_minus_obs.mean() if len(day) else np.nan,
            "frac_built_500m": g.frac_built_500m.iloc[0] if "frac_built_500m" in g else np.nan,
            "ndvi_500m": g.ndvi_500m.iloc[0] if "ndvi_500m" in g else np.nan,
        })

    return df.groupby("station").apply(agg, include_groups=False).reset_index().round(3)


def summarise(df: pd.DataFrame) -> dict[str, Any]:
    return {"stations": int(df.station.nunique()), "station_hours": int(len(df)),
            "era5_bias_c": round(float(df.era5_minus_obs.mean()), 2),
            "era5_mae_c": round(float(df.era5_minus_obs.abs().mean()), 2)}
