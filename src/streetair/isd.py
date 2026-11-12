"""NOAA Integrated Surface Database (ISD), global-hourly CSV: station list, parsing and QC.

Air temperature is the TMP field, e.g. "+0285,1" = 28.5 degC with quality code 1.
Missing values are +9999. Quality codes 2, 3, 6 and 7 mark suspect or erroneous values.
Routine hourly METAR reports (FM-15) are preferred when several reports fall in one hour.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

STATION_LIST_URL = "https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv"
# NCEI ended direct HTTPS download of ISD CSV in 2026; the same files are served by NOAA's
# Open Data Dissemination (NODD) program on AWS S3, no account needed.
DATA_URL = "https://noaa-global-hourly-pds.s3.amazonaws.com/{year}/{station}.csv"

BAD_QUALITY = {"2", "3", "6", "7"}
REPORT_PRIORITY = {"FM-15": 0, "FM-12": 1, "FM-16": 2, "SAO": 3}   # others are ignored


def _get(url: str) -> str:
    import requests

    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.text


def list_stations(cfg: dict[str, Any], text: str | None = None) -> pd.DataFrame:
    """ISD stations inside the bbox that were active for the whole period."""
    text = text if text is not None else _get(STATION_LIST_URL)
    df = pd.read_csv(io.StringIO(text), dtype={"USAF": str, "WBAN": str})
    lon0, lat0, lon1, lat1 = cfg["bbox"]
    start = int(cfg["period"]["start"].replace("-", ""))
    end = int(cfg["period"]["end"].replace("-", ""))
    m = (df.LON.between(lon0, lon1) & df.LAT.between(lat0, lat1)
         & (df.BEGIN <= start) & (df.END >= end) & df.USAF.ne("999999"))
    out = df[m].copy()
    out["station"] = out.USAF.str.zfill(6) + out.WBAN.str.zfill(5)
    out = out.rename(columns={"STATION NAME": "name", "ICAO": "icao", "LAT": "lat", "LON": "lon", "ELEV(M)": "elev_m"})
    return out[["station", "name", "icao", "lat", "lon", "elev_m"]].reset_index(drop=True)


def download(station: str, year: int, cache_dir: str | Path) -> Path | None:
    """Cached download of one station-year; None if NOAA has no CSV for it."""
    import requests

    path = Path(cache_dir) / f"{station}_{year}.csv"
    if path.exists() and _looks_like_isd(path.read_text(errors="replace")[:300]):
        return path
    url = DATA_URL.format(year=year, station=station)
    r = requests.get(url, timeout=300, headers={"User-Agent": "street-air-temperature (research)"})
    if r.status_code in (403, 404):        # S3 answers a missing key with 403/404
        return None
    r.raise_for_status()
    if not _looks_like_isd(r.text[:300]):
        # NCEI answers some missing station-years with an HTML page and status 200
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(r.text)
    return path


def _looks_like_isd(head: str) -> bool:
    return head.lstrip("\ufeff").startswith('"STATION"')


def parse_hourly(source: str | Path | io.StringIO) -> pd.DataFrame:
    """One row per station and UTC hour: the best report nearest the top of the hour."""
    df = pd.read_csv(source, usecols=["STATION", "DATE", "REPORT_TYPE", "TMP"], dtype=str)
    df["REPORT_TYPE"] = df.REPORT_TYPE.str.strip()
    df = df[df.REPORT_TYPE.isin(REPORT_PRIORITY)].copy()
    val, qc = df.TMP.str.split(",", n=1, expand=True).reindex(columns=[0, 1]).T.values
    df["t_obs"] = pd.to_numeric(val, errors="coerce") / 10.0
    df["qc"] = qc
    df = df[(df.t_obs.abs() < 999) & ~df.qc.isin(BAD_QUALITY)]

    t = pd.to_datetime(df.DATE, utc=True)
    df["time"] = t.dt.round("h")
    df["offset_min"] = (t - df.time).abs().dt.total_seconds() / 60
    df["prio"] = df.REPORT_TYPE.map(REPORT_PRIORITY)
    df = df.sort_values(["STATION", "time", "prio", "offset_min"]).drop_duplicates(["STATION", "time"])
    df["station"] = df.STATION.str.zfill(11)
    return df[["station", "time", "t_obs"]].reset_index(drop=True)


def quality_control(obs: pd.DataFrame, cfg: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Range check, spike filter and coverage filter.

    Returns the cleaned observations and a per-station report.
    """
    s = cfg["stations"]
    lo, hi = s["valid_range_c"]
    hours = pd.date_range(cfg["period"]["start"], pd.Timestamp(cfg["period"]["end"]) + pd.Timedelta(hours=23),
                          freq="h", tz="UTC")
    kept, report = [], []
    for st, g in obs.groupby("station"):
        x = g.set_index("time").t_obs.reindex(hours)
        n_raw = int(x.notna().sum())
        x = x.where(x.between(lo, hi))
        n_range = n_raw - int(x.notna().sum())
        d_prev, d_next = x - x.shift(1), x - x.shift(-1)
        spike = ((d_prev.abs() > s["max_step_c"]) & (d_next.abs() > s["max_step_c"])
                 & (np.sign(d_prev) == np.sign(d_next)))
        x = x.where(~spike)
        cov = float(x.notna().mean())
        report.append({"station": st, "hours_raw": n_raw, "removed_range": n_range,
                       "removed_spike": int(spike.sum()), "coverage": round(cov, 3),
                       "kept": cov >= s["min_coverage"]})
        if cov >= s["min_coverage"]:
            kept.append(pd.DataFrame({"station": st, "time": x.index, "t_obs": x.values}).dropna())
    clean = pd.concat(kept, ignore_index=True) if kept else obs.iloc[:0][["station", "time", "t_obs"]]
    return clean, pd.DataFrame(report)
