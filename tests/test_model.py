"""Milestone 2: leave-one-station-out evaluation on synthetic stations."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from streetair import model
from streetair.dataset import build
from streetair.plots import error_by_hour, error_by_station

CFG = {"model": {"ridge_alpha": 1.0,
                 "lightgbm": {"n_estimators": 150, "learning_rate": 0.1, "num_leaves": 7, "min_child_samples": 50,
                              "feature_fraction": 1.0, "bagging_fraction": 1.0, "seed": 0}}}


def make_data(n_st: int = 10, days: int = 20, seed: int = 0) -> pd.DataFrame:
    """Station temperature = ERA5 + a night-time effect of built-up land (warmer) and NDVI (cooler)."""
    rng = np.random.default_rng(seed)
    hours = pd.date_range("2023-06-01", periods=24 * days, freq="h", tz="UTC")
    built = np.linspace(0.05, 0.8, n_st)
    ndvi = rng.permutation(np.linspace(0.05, 0.55, n_st))
    obs, era, st, fe = [], [], [], []
    base = 30 + 8 * np.sin(2 * np.pi * (hours.hour - 15) / 24)
    for i in range(n_st):
        sid = f"s{i:02d}"
        local_h = (hours.hour - 7) % 24
        night = (local_h < 7) | (local_h > 18)
        effect = np.where(night, 3.0 * built[i] - 4.0 * ndvi[i], 0.5 * built[i])
        obs.append(pd.DataFrame({"station": sid, "time": hours, "t_obs": base + effect + rng.normal(0, 0.3, len(hours))}))
        era.append(pd.DataFrame({"station": sid, "time": hours, "t_era5": base, "era5_cell_elev_m": 350.0,
                                 "era5_wind_ms": 2.0, "era5_cloud_pct": 10.0}))
        st.append({"station": sid, "name": sid, "icao": sid.upper(), "network": "azmet" if i % 3 == 0 else "noaa-isd",
                   "lat": 33.4, "lon": -112.0, "elev_m": 350.0})
        row = {"station": sid, "elev_dem_m": 350.0, "ndvi_100m": ndvi[i], "ndvi_500m": ndvi[i]}
        for r in (100, 500):
            row.update({f"frac_built_{r}m": built[i], f"frac_tree_{r}m": 0.0, f"frac_shrub_{r}m": 0.0,
                        f"frac_grass_crop_{r}m": 0.0, f"frac_bare_{r}m": 1 - built[i], f"frac_water_{r}m": 0.0})
        fe.append(row)
    return build(pd.concat(obs), pd.concat(era), pd.DataFrame(st), pd.DataFrame(fe))


@pytest.fixture(scope="module")
def oof():
    return model.leave_one_station_out(make_data(), CFG, verbose=False)


def test_features_exclude_identity_and_target(oof):
    df = model.add_time_features(make_data(n_st=3, days=2))
    feats = model.feature_list(df)
    assert not set(feats) & {"station", "lat", "lon", "icao", "t_obs", "era5_minus_obs", "name"}
    assert "frac_built_500m" in feats and "hour_sin" in feats and "t_era5_change_3h" in feats


def test_every_row_predicted_once_by_a_model_that_never_saw_its_station(oof, monkeypatch):
    assert len(oof) == len(make_data()) and not oof.duplicated(["station", "time"]).any()
    seen = []
    real = model.fit_predict

    def spy(train, test, cfg, features):
        seen.append((set(train.station), set(test.station)))
        return real(train, test, cfg, features)

    monkeypatch.setattr(model, "fit_predict", spy)
    model.leave_one_station_out(make_data(n_st=4, days=3), CFG, verbose=False)
    assert len(seen) == 4 and all(len(te) == 1 and not (tr & te) for tr, te in seen)


def test_learned_methods_beat_era5_on_held_out_stations(oof):
    s = model.summarise(oof)["summary"].set_index("method")
    # A smooth site effect over few stations: the linear model recovers it almost fully, trees only
    # partly (they cannot interpolate between the handful of site values seen in training).
    assert s.loc["ridge", "mae_c"] < 0.5 * s.loc["era5_raw", "mae_c"]
    assert s.loc["lightgbm", "mae_c"] < 0.9 * s.loc["era5_raw", "mae_c"]
    assert s.loc["lightgbm", "mae_night_c"] < s.loc["era5_raw", "mae_night_c"]
    assert {"lightgbm [azmet]", "lightgbm [noaa-isd]"} <= set(s.index)


def test_constant_offset_uses_training_stations_only():
    df = model.add_time_features(make_data(n_st=3, days=2))
    train, test = df[df.station != "s02"], df[df.station == "s02"]
    p = model.fit_predict(train, test, CFG, model.feature_list(df))
    offset = (train.t_obs - train.t_era5).mean()
    assert np.allclose(p["era5_bias"], test.t_era5 + offset)


def test_summary_tables_and_figures(oof, tmp_path):
    res = model.summarise(oof)
    assert set(res["by_hour"].hour_local) == set(range(24))
    assert len(res["by_station"]) == 10 * len(model.METHODS)
    assert error_by_hour(res["by_hour"], tmp_path / "h.png").stat().st_size > 5000
    assert error_by_station(res["by_station"], tmp_path / "s.png").stat().st_size > 5000


def test_final_model_and_importance(tmp_path):
    gbm, feats, imp = model.fit_final(make_data(n_st=5, days=5), CFG)
    assert imp.gain_share.sum() == pytest.approx(1.0, abs=1e-3)
    path = model.save_final(gbm, feats, tmp_path / "m" / "lightgbm.txt")
    assert path.exists() and path.with_suffix(".features.json").exists()
