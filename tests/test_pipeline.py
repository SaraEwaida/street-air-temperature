"""Milestone 1 tests on fixtures in the real formats (no network)."""

from __future__ import annotations

import io
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_origin

from streetair import dataset, era5, features, isd
from streetair.plots import station_map

CFG = {
    "city": "phoenix",
    "bbox": [-112.60, 33.20, -111.50, 33.80],
    "crs": "EPSG:32612",
    "period": {"start": "2023-07-01", "end": "2023-07-02"},
    "stations": {"min_coverage": 0.7, "valid_range_c": [-15, 55], "max_step_c": 8},
    "era5": {"model": "era5", "raw_grid_cell": True},
    "features": {"radii_m": [100, 500], "ndvi_period": ["2023-05-01", "2023-09-30"],
                 "ndvi_max_cloud": 10, "ndvi_scenes": 2},
}

ISD_HISTORY = '''"USAF","WBAN","STATION NAME","CTRY","STATE","ICAO","LAT","LON","ELEV(M)","BEGIN","END"
"722780","23183","PHOENIX SKY HARBOR INTL AIRPO","US","AZ","KPHX","+33.428","-112.004","+0337.4","19330101","20261110"
"722785","03184","DEER VALLEY AIRPORT","US","AZ","KDVT","+33.688","-112.082","+0446.8","20000101","20261110"
"722789","99999","CLOSED FIELD","US","AZ","","+33.500","-112.100","+0340.0","19900101","20051231"
"723740","23194","FLAGSTAFF","US","AZ","KFLG","+35.144","-111.666","+2134.6","19480101","20261110"
"999999","53126","NO USAF ID","US","AZ","","+33.400","-112.000","+0340.0","20000101","20261110"
'''


def _isd_csv(rows):
    head = '"STATION","DATE","SOURCE","LATITUDE","LONGITUDE","ELEVATION","NAME","REPORT_TYPE","CALL_SIGN","QUALITY_CONTROL","WND","TMP"\n'
    body = "".join(f'"72278023183","{d}","4","33.43","-112.00","337.4","PHX","{rt}","KPHX ","V020","270,1,N,0050,1","{t}"\n'
                   for d, rt, t in rows)
    return head + body


def test_list_stations_filters_bbox_period_and_ids():
    st = isd.list_stations(CFG, ISD_HISTORY)
    assert st.station.tolist() == ["72278023183", "72278503184"]     # closed, out-of-bbox, 999999 dropped
    assert st.loc[0, "icao"] == "KPHX" and st.loc[0, "elev_m"] == pytest.approx(337.4)


def test_parse_hourly_prefers_routine_report_and_drops_bad_values():
    csv = _isd_csv([
        ("2023-07-01T00:51:00", "FM-15", "+0352,5"),     # routine METAR -> hour 01
        ("2023-07-01T01:05:00", "FM-16", "+0360,5"),     # special in the same hour, lower priority
        ("2023-07-01T01:51:00", "FM-15", "+9999,9"),     # missing
        ("2023-07-01T02:51:00", "FM-15", "+0340,3"),     # erroneous quality code
        ("2023-07-01T03:51:00", "FM-15", "+0331,1"),
        ("2023-07-01T23:59:00", "SOD  ", "+0400,1"),     # daily summary, ignored
    ])
    h = isd.parse_hourly(io.StringIO(csv))
    assert h.time.dt.hour.tolist() == [1, 4]
    assert h.t_obs.tolist() == [35.2, 33.1]


def test_quality_control_removes_spikes_range_and_low_coverage():
    hours = pd.date_range("2023-07-01", periods=48, freq="h", tz="UTC")
    t = 35 + 5 * np.sin(np.arange(48) / 24 * 2 * np.pi)
    good = pd.DataFrame({"station": "A", "time": hours, "t_obs": t})
    good.loc[10, "t_obs"] += 15            # spike
    good.loc[20, "t_obs"] = 70             # out of range
    sparse = pd.DataFrame({"station": "B", "time": hours[::4], "t_obs": t[::4]})
    clean, rep = isd.quality_control(pd.concat([good, sparse]), CFG)
    r = rep.set_index("station")
    assert r.loc["A", "removed_spike"] == 1 and r.loc["A", "removed_range"] == 1 and r.loc["A", "kept"]
    assert not r.loc["B", "kept"] and set(clean.station) == {"A"}
    assert clean.t_obs.max() < 41


def test_era5_params_and_parse():
    st = pd.DataFrame({"station": ["s1", "s2"], "lat": [33.4, 33.7], "lon": [-112.0, -112.1]})
    p = era5.build_params(st, CFG)
    assert p["elevation"] == "nan,nan" and p["models"] == "era5" and p["timezone"] == "GMT"
    payload = [{"elevation": 350.0, "hourly": {"time": ["2023-07-01T00:00", "2023-07-01T01:00"],
                                               "temperature_2m": [33.0, None]}},
               {"elevation": 420.0, "hourly": {"time": ["2023-07-01T00:00"], "temperature_2m": [31.5]}}]
    df = era5.parse_response(payload, st)
    assert len(df) == 2 and df.time.dt.tz is not None
    assert df.set_index("station").loc["s2", "era5_cell_elev_m"] == 420.0
    with pytest.raises(ValueError):
        era5.parse_response(payload[:1], st)


def test_circle_mask_and_fractions():
    m = circle_mask = features.circle_mask((100, 100), 10, 500)
    assert circle_mask.sum() == pytest.approx(np.pi * 50 ** 2, rel=0.02)
    lc = np.full((100, 100), 60, np.uint8)
    lc[40:60, 40:60] = 50                                       # 200 m built block in the centre
    f = features.landcover_fractions(lc, 10, [100, 500])
    assert f["frac_built_100m"] == pytest.approx(1.0) and f["frac_built_500m"] < 0.6
    assert f["frac_built_500m"] + f["frac_bare_500m"] == pytest.approx(1.0)
    assert m.dtype == bool


def test_ndvi_masks_clouds():
    red = np.full((3, 3), int((0.10 + 0.2) / features.SR_SCALE), np.uint16)
    nir = np.full((3, 3), int((0.30 + 0.2) / features.SR_SCALE), np.uint16)
    qa = np.full((3, 3), 21824, np.uint16)
    qa[0, 0] |= 1 << 3
    v = features.landsat_ndvi(red, nir, qa)
    assert np.isnan(v[0, 0]) and np.allclose(v[1:, 1:], 0.5, atol=1e-3)


def _tif(path, arr, crs, transform, nodata):
    with rasterio.open(path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1,
                       dtype=arr.dtype, crs=crs, transform=transform, nodata=nodata) as d:
        d.write(arr, 1)
    return str(path)


def test_site_features_end_to_end(tmp_path, monkeypatch):
    """Real code path with local rasters standing in for Planetary Computer assets."""
    lon, lat = -112.004, 33.428
    res = 1 / 12000
    lc = np.full((1200, 1200), 60, np.uint8)
    lc[585:615, 585:615] = 50                                   # ~250 m built core around the station
    wc = _tif(tmp_path / "wc.tif", lc, "EPSG:4326", from_origin(lon - 0.05, lat + 0.05, res, res), 0)
    dem = _tif(tmp_path / "dem.tif", np.full((400, 400), 340, np.float32), "EPSG:4326",
               from_origin(lon - 0.05, lat + 0.05, 1 / 3600, 1 / 3600), -32767.0)
    from rasterio.warp import transform as wp
    x, y = wp("EPSG:4326", "EPSG:32612", [lon], [lat])
    t = from_origin(x[0] - 3000, y[0] + 3000, 30, 30)
    red = _tif(tmp_path / "red.tif", np.full((200, 200), int(0.3 / features.SR_SCALE), np.uint16), "EPSG:32612", t, 0)
    nir = _tif(tmp_path / "nir.tif", np.full((200, 200), int(0.5 / features.SR_SCALE), np.uint16), "EPSG:32612", t, 0)
    qa = _tif(tmp_path / "qa.tif", np.full((200, 200), 21824, np.uint16), "EPSG:32612", t, 1)

    asset = lambda h: SimpleNamespace(href=h)  # noqa: E731
    poly = {"type": "Polygon", "coordinates": [[[lon - 1, lat - 1], [lon + 1, lat - 1], [lon + 1, lat + 1],
                                                [lon - 1, lat + 1], [lon - 1, lat - 1]]]}
    scenes = [SimpleNamespace(assets={"red": asset(red), "nir08": asset(nir), "qa_pixel": asset(qa)},
                              geometry=poly, properties={"eo:cloud_cover": c}) for c in (1, 2, 3)]
    monkeypatch.setattr(features, "_items", lambda cfg: (
        [SimpleNamespace(assets={"map": asset(wc)})], [SimpleNamespace(assets={"data": asset(dem)})], scenes))

    st = pd.DataFrame({"station": ["72278023183"], "name": ["PHX"], "lat": [lat], "lon": [lon]})
    f = features.site_features(st, CFG, verbose=False).iloc[0]
    assert f.frac_built_100m > 0.95 and 0.05 < f.frac_built_500m < 0.5
    assert f.elev_dem_m == pytest.approx(340, abs=0.5)
    expected = (0.5 + features.SR_OFFSET - (0.3 + features.SR_OFFSET)) / (0.5 + 0.3 + 2 * features.SR_OFFSET)
    assert f.ndvi_100m == pytest.approx(expected, abs=0.01) and f.ndvi_scenes_100m == 2   # stops at ndvi_scenes


def test_dataset_build_summary_and_map(tmp_path):
    hours = pd.date_range("2023-07-01", periods=48, freq="h", tz="UTC")
    obs = pd.concat([pd.DataFrame({"station": s, "time": hours, "t_obs": 35 + d + np.zeros(48)})
                     for s, d in (("A", 2.0), ("B", 0.0))])
    e5 = pd.concat([pd.DataFrame({"station": s, "time": hours, "t_era5": 34.0, "era5_cell_elev_m": 400.0})
                    for s in ("A", "B")])
    st = pd.DataFrame({"station": ["A", "B"], "name": ["Downtown", "Desert"], "icao": ["KAAA", "KBBB"],
                       "lat": [33.45, 33.70], "lon": [-112.05, -112.30], "elev_m": [340.0, 500.0]})
    fe = pd.DataFrame({"station": ["A", "B"], "elev_dem_m": [340.0, 500.0],
                       "frac_built_500m": [0.8, 0.1], "ndvi_500m": [0.1, 0.05]})
    df = dataset.build(obs, e5, st, fe)
    assert len(df) == 96 and df.hour_local.between(0, 23).all()
    # Phoenix is UTC-7 all year
    assert df.loc[0, "hour_local"] == 17
    a = df[df.station == "A"].iloc[0]
    assert a.t_era5_elev == pytest.approx(34.0 + 0.0065 * 60)       # lower than the ERA5 cell -> warmer
    s = dataset.station_summary(df).set_index("station")
    assert s.loc["A", "era5_bias_c"] == pytest.approx(-3.0) and s.loc["B", "era5_bias_c"] == pytest.approx(-1.0)
    png = station_map(s.reset_index(), st, CFG, tmp_path / "map.png")
    assert png.exists() and png.stat().st_size > 5000


def test_download_rejects_non_csv(tmp_path, monkeypatch):
    import requests

    class Resp:
        status_code, text = 200, "<html><body>Service notice</body></html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    assert isd.download("72278023183", 2023, tmp_path) is None    # HTML page = no data, skipped
    assert not list(tmp_path.iterdir())                          # nothing bad cached
    assert isd._looks_like_isd('﻿"STATION","DATE"')
