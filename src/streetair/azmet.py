"""Arizona Meteorological Network (AZMet), University of Arizona: hourly API.

Endpoint: https://api.azmet.arizona.edu/v1/observations/hourly/{id}/{start}/{interval}
Response: {"data": [{"meta_station_id": ..., "date_datetime": ..., "temp_airC": ..., ...}]}

`temp_airC` is the average air temperature over the previous 60 minutes. Timestamps are in
Arizona time (MST, UTC-7, no daylight saving) at the end of each hour; midnight is stamped
"23:59:59". Each value is assigned to the hour it ends, which lines up with ISD's routine
reports a few minutes before the hour and with ERA5's instantaneous values on the hour.

Several Phoenix sites are on irrigated turf (Encanto, Greenway) or farmland (Buckeye, Queen
Creek): different surroundings from the airport stations, which is the contrast the model needs.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

API_URL = "https://api.azmet.arizona.edu/v1/observations/hourly/{station}/{start}/{interval}"
LOCAL_TZ = "America/Phoenix"
CHUNK_HOURS = 240          # 10 days per request

# id, name, symbol, lat, lon, elevation (m), first date in the API. Source: uace-azmet/azmetr station_info.
STATIONS = [
    ("az01", "Tucson", "TUC", 32.2806, -110.9459, 714, "2020-01-01"),
    ("az02", "Yuma Valley", "YUV", 32.7118, -114.7041, 36, "2020-01-01"),
    ("az04", "Safford", "SAF", 32.813, -109.6793, 903, "2020-01-01"),
    ("az05", "Coolidge", "COO", 32.98, -111.6057, 423, "2020-01-01"),
    ("az06", "Maricopa", "MAC", 33.0689, -111.9722, 362, "2020-01-01"),
    ("az07", "Aguila", "AGU", 33.9462, -113.1865, 657, "2020-01-01"),
    ("az08", "Parker", "PAR", 33.9643, -114.4855, 98, "2020-01-01"),
    ("az09", "Bonita", "BON", 32.4775, -109.9327, 1349, "2020-01-01"),
    ("az12", "Phoenix Greenway", "PGR", 33.6233, -112.1077, 403, "2020-01-01"),
    ("az14", "Yuma N.Gila", "YUG", 32.7804, -114.5163, 43, "2020-01-01"),
    ("az15", "Phoenix Encanto", "PEN", 33.4784, -112.098, 334, "2020-01-01"),
    ("az16", "Eloy", "ELO", 32.7861, -111.5594, 465, "2026-10-09"),
    ("az19", "Paloma", "GIB", 32.9271, -112.8967, 221, "2020-01-01"),
    ("az20", "Mohave", "MOH", 34.9675, -114.6111, 147, "2020-01-01"),
    ("az22", "Queen Creek", "QCR", 33.1939, -111.5326, 462, "2020-01-01"),
    ("az23", "Harquahala", "HAR", 33.4907, -113.1112, 348, "2020-01-01"),
    ("az24", "Roll", "ROL", 32.7629, -113.9675, 81, "2020-01-01"),
    ("az26", "Buckeye", "BUC", 33.4092, -112.6778, 301, "2020-01-01"),
    ("az27", "Desert Ridge", "DES", 33.6882, -111.9634, 511, "2020-01-01"),
    ("az28", "Mohave-2", "MO2", 34.8782, -114.5668, 142, "2020-01-01"),
    ("az32", "Payson", "PAY", 34.2325, -111.345, 1479, "2020-01-01"),
    ("az33", "Bowie", "BOW", 32.2891, -109.4721, 1162, "2020-01-01"),
    ("az35", "Parker-2", "PA2", 33.8557, -114.4751, 92, "2020-01-01"),
    ("az36", "Yuma South", "YUS", 32.5655, -114.744, 28, "2020-01-01"),
    ("az37", "San Simon", "SAN", 32.2001, -109.1239, 1130, "2020-01-01"),
    ("az38", "Sahuarita", "SAH", 31.9689, -110.9456, 822, "2020-01-01"),
    ("az39", "Willcox Bench", "WIB", 32.1181, -109.7145, 1306, "2020-01-01"),
    ("az40", "Ft Mohave CA", "MOC", 34.9691, -114.645, 147, "2020-01-01"),
    ("az41", "Salome", "SAL", 33.8485, -113.5398, 585, "2020-01-01"),
    ("az42", "Wellton ETo", "WEL", 32.6932, -114.0674, 73, "2023-05-02"),
    ("az43", "Yuma Valley ETo", "YUE", 32.7118, -114.7041, 36, "2023-05-02"),
    ("az44", "Mohave ETo", "MOE", 34.8951, -114.6157, 145, "2023-07-20"),
    ("az45", "Chino Valley", "CHV", 34.7842, -112.4166, 1414, "2025-04-25"),
    ("az46", "Duncan", "DUN", 32.7333, -109.1164, 1109, "2026-02-21"),
    ("az47", "Elgin", "ELG", 31.6606, -110.5826, 1493, "2025-06-27"),
]


def list_stations(cfg: dict[str, Any]) -> pd.DataFrame:
    df = pd.DataFrame(STATIONS, columns=["station", "name", "icao", "lat", "lon", "elev_m", "start_date"])
    lon0, lat0, lon1, lat1 = cfg["bbox"]
    m = df.lon.between(lon0, lon1) & df.lat.between(lat0, lat1) & (df.start_date <= cfg["period"]["start"])
    return df[m].drop(columns="start_date").reset_index(drop=True)


def parse(payload: dict[str, Any]) -> pd.DataFrame:
    rows = payload.get("data") or []
    df = pd.DataFrame([r for r in rows if r], columns=["meta_station_id", "date_datetime", "temp_airC"])
    if df.empty:
        return pd.DataFrame(columns=["station", "time", "t_obs"])
    local = pd.to_datetime(df.date_datetime)
    midnight = local.dt.strftime("%H:%M:%S") == "23:59:59"
    local = local.where(~midnight, local.dt.ceil("D"))
    return pd.DataFrame({
        "station": df.meta_station_id,
        "time": local.dt.round("h").dt.tz_localize(LOCAL_TZ).dt.tz_convert("UTC"),
        "t_obs": pd.to_numeric(df.temp_airC, errors="coerce"),
    }).dropna()


def fetch(station: str, cfg: dict[str, Any]) -> pd.DataFrame:
    import requests

    start = pd.Timestamp(cfg["period"]["start"])
    end = pd.Timestamp(cfg["period"]["end"]) + pd.Timedelta(days=1)
    frames = []
    t = start
    while t < end:
        hours = int(min(CHUNK_HOURS, (end - t) / pd.Timedelta(hours=1)))
        url = API_URL.format(station=station, start=t.strftime("%Y-%m-%dT%H:%M"), interval=f"PT{hours}H")
        r = requests.get(url, timeout=120, headers={"Accept": "application/json",
                                                     "User-Agent": "street-air-temperature (research)"})
        r.raise_for_status()
        frames.append(parse(r.json()))
        t += pd.Timedelta(hours=hours)
    out = pd.concat(frames, ignore_index=True).drop_duplicates(["station", "time"])
    return out.sort_values("time").reset_index(drop=True)
