"""Milestone 3: grid features, prediction maps, COGs and the STAC catalog (no network)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
import rasterio

from streetair import catalog, features, grid, model
from streetair.plots import catalog_maps

CFG = {
    "city": "phoenix", "crs": "EPSG:32612", "bbox": [-112.10, 33.40, -112.00, 33.47],
    "features": {"radii_m": [100, 500]},
    "catalog": {"res_m": 100, "base_res_m": 50, "water_threshold": 0.5,
                "times_local": ["2023-07-15T05:00", "2023-07-15T15:00"]},
    "era5": {"model": "era5", "raw_grid_cell": True},
}


def test_disk_ratio_uniform_and_edge():
    a = np.ones((40, 40))
    assert np.allclose(grid.disk_ratio(a, a, 5), 1.0)
    half = np.zeros((40, 40))
    half[:, 20:] = 1
    r = grid.disk_ratio(half, np.ones((40, 40)), 5)
    assert r[20, 2] == pytest.approx(0, abs=1e-9) and r[20, 37] == pytest.approx(1) and 0.4 < r[20, 20] < 0.6


def test_grid_fractions_match_station_circles():
    """The 50 m disc approximation agrees with the stations' exact 10 m circles."""
    lc = np.full((1000, 1000), 60, np.uint8)            # 10 km of bare land at 10 m
    lc[480:520, 470:540] = 50                           # 400 m x 700 m built block
    lc[100:300, 100:300] = 80
    g = grid.landcover_features(lc, [100, 500], base_k=5, out_k=2)
    exact = features.landcover_fractions(lc[400:600, 405:605], 10, [100, 500])   # 2 km window around pixel (50, 50)
    for k in ("frac_built_100m", "frac_built_500m", "frac_bare_500m"):
        assert g[k][50, 50] == pytest.approx(exact[k], abs=0.06), k
    assert g["frac_water_100m"][20, 20] == pytest.approx(1.0)


def test_cells_cover_every_pixel():
    t, shape = grid.grid_spec(CFG, 100)
    g = {"transform": t, "shape": shape, "crs": CFG["crs"]}
    cells = catalog.era5_cells(CFG)
    idx = catalog.pixel_cells(g, cells)
    assert idx.shape == shape and idx.min() >= 0 and idx.max() < len(cells)
    lon, lat = grid.pixel_lonlat(t, shape, CFG["crs"])
    c = cells.iloc[idx[0, 0]]
    assert abs(c.lon - lon[0, 0]) <= 0.125 + 1e-9 and abs(c.lat - lat[0, 0]) <= 0.125 + 1e-9


def test_support_mask_flags_extrapolation():
    f = {"frac_built_500m": np.array([[0.2, 0.9, 0.78]]), "ndvi_500m": np.array([[0.3, 0.3, np.nan]]),
         "frac_water_500m": np.array([[0.04, 0.0, 0.0]])}
    r = {"frac_built_500m": (0.0, 0.75), "ndvi_500m": (0.05, 0.56), "frac_water_500m": (0.0, 0.013)}
    # 0.9 built is outside 0.75 + 0.05; 0.78 is within the margin; missing NDVI is not extrapolation;
    # a little water (0.04) is within the absolute margin even though the stations span only 0-0.013
    assert catalog.support_mask(f, r).tolist() == [[True, False, True]]
    by = catalog.support_by_feature(f, r, np.ones((1, 3), bool)).set_index("feature")
    assert by.loc["frac_built_500m", "land_share_in_range"] == pytest.approx(2 / 3, abs=1e-3)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """A small grid with synthetic features, a small trained model, and ERA5 for its cells."""
    import lightgbm as lgb

    root = tmp_path_factory.mktemp("cat")
    t, shape = grid.grid_spec(CFG, 100)
    rng = np.random.default_rng(0)
    feats = {k: rng.uniform(0, 0.6, shape).astype(np.float32) for k in model.SITE if k.startswith(("frac", "ndvi"))}
    feats["frac_water_100m"] = np.zeros(shape, np.float32)
    feats["frac_water_100m"][:5, :5] = 1.0              # a pond -> nodata
    feats["elev_dem_m"] = np.full(shape, 340, np.float32)
    g = {"features": feats, "transform": t, "shape": shape, "crs": CFG["crs"]}

    cells = catalog.era5_cells(CFG)
    times = catalog.local_times(CFG)
    hours = pd.date_range(times[0] - pd.Timedelta(hours=30), times[-1] + pd.Timedelta(hours=30), freq="h")
    e = pd.concat([pd.DataFrame({"station": s, "time": hours, "t_era5": 30 + 8 * np.sin(np.arange(len(hours)) / 24 * 2 * np.pi),
                                 "era5_cell_elev_m": 360.0, "era5_wind_ms": 2.0, "era5_cloud_pct": 5.0,
                                 "era5_sw_wm2": 300.0, "era5_rh_pct": 20.0}) for s in cells.station])
    e = catalog.era5_context(e)

    # Train a tiny residual model whose correction grows with built-up fraction
    n = 3000
    X = pd.DataFrame({k: rng.uniform(0, 0.6, n) for k in model.SITE})
    X["hour_local"], X["doy"] = rng.integers(0, 24, n), rng.integers(1, 366, n)
    for k in model.WEATHER:
        X[k] = rng.normal(30, 5, n)
    X = model.add_time_features(X)
    feats_order = [c for c in model.SITE + model.TIME + model.WEATHER]
    y = 3 * X.frac_built_500m - 1
    booster = lgb.train({"objective": "regression", "verbose": -1, "num_leaves": 7}, lgb.Dataset(X[feats_order], y), 60)

    cell_idx = catalog.pixel_cells(g, cells)
    land = feats["frac_water_100m"] <= CFG["catalog"]["water_threshold"]
    maps = {tm: catalog.predict_hour(g, cell_idx, e[e.time == tm], tm, booster, feats_order, land) for tm in times}
    in_range = catalog.support_mask(feats, {"frac_built_500m": (0.0, 0.5)}) & land
    path = catalog.build_catalog(CFG, g, maps, in_range, root / "catalog", {"model": "test"})
    return g, maps, times, in_range, path


def test_maps_are_era5_plus_correction(built):
    g, maps, times, _, _ = built
    m = maps[times[0]]
    assert np.isnan(m["temperature"][:5, :5]).all()                       # water masked
    ok = np.isfinite(m["temperature"])
    assert ok.mean() > 0.9
    assert np.allclose(m["temperature"][ok], m["era5"][ok] + m["correction"][ok], atol=1e-4)
    b = g["features"]["frac_built_500m"]
    assert np.corrcoef(b[ok], m["correction"][ok])[0, 1] > 0.8            # correction follows built-up land


def test_stac_catalog_reads_back(built):
    import pystac

    g, maps, times, _, path = built
    coll = pystac.Collection.from_file(str(path))
    items = list(coll.get_items())
    assert len(items) == len(times) and {i.datetime for i in items} == {t.to_pydatetime() for t in times}
    item = items[0]
    assert set(item.assets) == {"temperature", "era5", "correction"}
    assert item.properties["proj:code"] == "EPSG:32612" and item.properties["proj:shape"] == list(g["shape"])
    assert "in_training_range" in coll.assets
    raw = json.loads(path.read_text())
    assert raw["type"] == "Collection" and raw["license"] == "CC-BY-4.0"
    item_json = json.loads(next(path.parent.glob("*/phoenix-air-temperature-*.json")).read_text())
    assert all(a["href"].startswith("./") for a in item_json["assets"].values())   # self-contained catalog
    tm = next(t for t in times if t.to_pydatetime() == item.datetime)
    with rasterio.open(item.assets["temperature"].get_absolute_href()) as src:
        assert src.crs.to_epsg() == 32612 and src.profile["tiled"] and src.nodata == catalog.NODATA
        a = src.read(1, masked=True)
        assert float(a.mean()) == pytest.approx(float(np.nanmean(maps[tm]["temperature"])), abs=1e-3)
        assert a.mask[:5, :5].all()                                        # water is nodata in the file


def test_summary_and_figure(built, tmp_path):
    g, maps, times, in_range, _ = built
    s = catalog.summary(maps, in_range)
    assert len(s) == 2 and (s.land_pixels > 0).all() and s.in_training_range.between(0, 1).all()
    st = pd.DataFrame({"lon": [-112.05], "lat": [33.43]})
    assert catalog_maps(maps, times, g, tmp_path / "m.png", stations=st).stat().st_size > 10_000
