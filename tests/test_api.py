"""Milestone 4: point service and API, on a synthetic grid and model (no network)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from streetair import catalog, grid, model, service

pytest.importorskip("fastapi")

CFG = {
    "city": "phoenix", "crs": "EPSG:32612", "bbox": [-112.30, 33.30, -111.90, 33.60],
    "features": {"radii_m": [100, 500]},
    "catalog": {"res_m": 100, "base_res_m": 50, "water_threshold": 0.5, "times_local": ["2023-07-15T05:00"]},
    "era5": {"model": "era5", "raw_grid_cell": True},
}


def fake_era5(cells: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Deterministic ERA5: varies smoothly with position and hour."""
    hours = pd.date_range(start, pd.Timestamp(end) + pd.Timedelta(hours=23), freq="h", tz="UTC")
    rows = []
    for c in cells.itertuples():
        base = 30 + 2 * (c.lon + 112) + 3 * (c.lat - 33.4)
        rows.append(pd.DataFrame({"station": c.station, "time": hours,
                                  "t_era5": base + 6 * np.sin(2 * np.pi * (hours.hour - 15) / 24),
                                  "era5_cell_elev_m": 350 + 100 * (c.lat - 33.3), "era5_wind_ms": 2.0,
                                  "era5_cloud_pct": 10.0, "era5_sw_wm2": 200.0, "era5_rh_pct": 25.0}))
    return pd.concat(rows, ignore_index=True)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    import lightgbm as lgb

    root = tmp_path_factory.mktemp("svc")
    t, shape = grid.grid_spec(CFG, 100)
    rng = np.random.default_rng(1)
    feats = {k: rng.uniform(0, 0.5, shape).astype(np.float32) for k in model.SITE if k.startswith(("frac", "ndvi"))}
    feats["frac_water_100m"] = np.zeros(shape, np.float32)
    feats["frac_water_100m"][150:160, 150:160] = 1.0      # a pond well inside the area
    feats["elev_dem_m"] = np.full(shape, 340, np.float32)
    g = {"features": feats, "transform": t, "shape": shape, "crs": CFG["crs"]}
    grid.save(g, root / "grid.npz")

    n = 4000
    X = pd.DataFrame({k: rng.uniform(0, 0.6, n) for k in model.SITE})
    X["hour_local"], X["doy"] = rng.integers(0, 24, n), rng.integers(1, 366, n)
    for k in model.WEATHER:
        X[k] = rng.normal(30, 5, n)
    X = model.add_time_features(X)
    order = model.SITE + model.TIME + model.WEATHER
    booster = lgb.train({"objective": "regression", "verbose": -1, "num_leaves": 15},
                        lgb.Dataset(X[order], 3 * X.frac_built_500m - X.ndvi_500m), 80)
    (root / "models").mkdir()
    booster.save_model(str(root / "models" / "lightgbm.txt"))
    (root / "models" / "lightgbm.features.json").write_text(json.dumps(order))

    di = np.where(feats["frac_built_500m"] > 0.4, 0.9, 0.2).astype(np.float32)
    (root / "catalog").mkdir()
    catalog.write_cog(root / "catalog" / "dissimilarity_index.tif", di, g)
    (root / "catalog" / "collection.json").write_text(json.dumps(
        {"type": "Collection", "id": "x", "summaries": {"model": {"aoa_threshold": 0.5}}}))

    cfg = {**CFG, "paths": {"grid_features": str(root / "grid.npz"), "model_lightgbm": str(root / "models" / "lightgbm.txt"),
                            "catalog_dir": str(root / "catalog")}}
    svc = service.Service.from_config(cfg, era5_fetcher=fake_era5)
    return svc, g, booster, order, di


def test_parse_time():
    assert service.parse_time("2023-07-15T05:00").isoformat() == "2023-07-15T12:00:00+00:00"   # MST = UTC-7
    assert service.parse_time("2023-07-15T12:20:00Z").isoformat() == "2023-07-15T12:00:00+00:00"
    with pytest.raises(service.PointError):
        service.parse_time("yesterday-ish")


def test_point_matches_map_pixel(env):
    """The API and the maps are the same computation: a point at a pixel centre gets the pixel's value."""
    svc, g, booster, order, _ = env
    r, c = 40, 120
    lon, lat = grid.pixel_lonlat(g["transform"], g["shape"], g["crs"])
    t = service.parse_time("2023-07-15T05:00")
    cells = catalog.era5_cells(CFG)
    e = catalog.era5_context(fake_era5(cells, "2023-07-14", "2023-07-16"))
    land = g["features"]["frac_water_100m"] <= 0.5
    m = catalog.predict_hour(g, catalog.pixel_cell_weights(g, cells), e[e.time == t], t, booster, order, land)
    out = svc.predict(float(lat[r, c]), float(lon[r, c]), "2023-07-15T05:00")
    assert out["temperature_c"] == pytest.approx(float(m["temperature"][r, c]), abs=0.02)
    assert out["era5_c"] == pytest.approx(float(m["era5"][r, c]), abs=0.02)
    assert out["temperature_c"] == pytest.approx(out["era5_c"] + out["correction_c"], abs=0.02)


def test_area_of_applicability_and_errors(env):
    svc, g, _, _, di = env
    lon, lat = grid.pixel_lonlat(g["transform"], g["shape"], g["crs"])
    inner = np.zeros(di.shape, bool)
    inner[60:-60, 60:-60] = True                        # well inside the lon/lat box
    r, c = np.argwhere((di > 0.5) & inner)[0]
    out = svc.predict(float(lat[r, c]), float(lon[r, c]), "2023-07-15T05:00")
    assert out["in_area_of_applicability"] is False and out["dissimilarity_index"] == pytest.approx(0.9)
    r, c = np.argwhere((di < 0.5) & (g["features"]["frac_water_100m"] < 0.5) & inner)[0]
    assert svc.predict(float(lat[r, c]), float(lon[r, c]), "2023-07-15T05:00")["in_area_of_applicability"] is True
    with pytest.raises(service.PointError, match="outside"):
        svc.predict(40.0, -100.0, "2023-07-15T05:00")
    with pytest.raises(service.PointError, match="Did you mean lon=-112.07"):
        svc.predict(33.45, 112.07, "2023-07-15T05:00")
    with pytest.raises(service.PointError, match="water"):
        svc.predict(float(lat[155, 155]), float(lon[155, 155]), "2023-07-15T05:00")


def test_http_endpoints(env, monkeypatch):
    from fastapi.testclient import TestClient

    from streetair import api

    svc = env[0]
    monkeypatch.setattr(api, "get_service", lambda: svc)
    client = TestClient(api.app)
    assert client.get("/health").json()["status"] == "ok"
    r = client.get("/temperature", params={"lat": 33.45, "lon": -112.07, "time": "2023-07-15T05:00"})
    assert r.status_code == 200 and {"temperature_c", "era5_c", "in_area_of_applicability"} <= set(r.json())
    assert client.get("/temperature", params={"lat": 40, "lon": -100, "time": "2023-07-15T05:00"}).status_code == 422
    assert client.get("/temperature", params={"lat": 33.45, "lon": -112.07}).status_code == 422   # missing time
    assert client.get("/catalog").json()["type"] == "Collection"
    assert "/temperature" in client.get("/openapi.json").json()["paths"]
