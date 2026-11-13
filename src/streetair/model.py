"""Leave-one-station-out evaluation of ERA5 corrections.

Every method predicts the station temperature. Learned methods predict the residual
(station minus raw ERA5) and add it back. In each fold one station is held out entirely:
models, scalers and imputers are fitted on the other stations only.

Methods
- era5_raw      raw ERA5 grid-cell value
- era5_lapse    ERA5 moved to the station's elevation with a standard lapse rate
- era5_bias     ERA5 plus the mean residual of the training stations (constant offset)
- ridge         linear model on the same features, with site x night interactions
- lightgbm      gradient-boosted trees on site, time and ERA5 weather features

No station coordinates or identifiers are features, so a held-out station can only be
predicted from its surroundings and the weather.

Setting: reconstruction of past hours. `t_era5_day_mean` uses the whole local day, which is
available for historical maps but not for real-time nowcasting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

SITE = [f"frac_{g}_{r}m" for r in (100, 500) for g in ("built", "tree", "shrub", "grass_crop", "bare", "water")] + [
    "ndvi_100m", "ndvi_500m", "elev_minus_cell_m"]
TIME = ["hour_sin", "hour_cos", "doy_sin", "doy_cos"]
WEATHER = ["t_era5", "t_era5_change_3h", "t_era5_day_mean",
           "era5_wind_ms", "era5_cloud_pct", "era5_sw_wm2", "era5_rh_pct"]
METHODS = ["era5_raw", "era5_lapse", "era5_bias", "ridge", "lightgbm"]


def add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    h = 2 * np.pi * df.hour_local / 24
    d = 2 * np.pi * (df.doy - 1) / 365.25
    df["hour_sin"], df["hour_cos"] = np.sin(h), np.cos(h)
    df["doy_sin"], df["doy_cos"] = np.sin(d), np.cos(d)
    return df


def feature_list(df: pd.DataFrame) -> list[str]:
    return [c for c in SITE + TIME + WEATHER if c in df.columns and df[c].notna().any()]


def _ridge(alpha: float):
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=alpha))


def _ridge_matrix(df: pd.DataFrame, features: list[str]) -> np.ndarray:
    """Features plus site x night interactions, so the linear model can separate day and night effects."""
    site = [c for c in SITE if c in features]
    night = df.is_night.to_numpy(float)[:, None]
    return np.hstack([df[features].to_numpy(float), df[site].to_numpy(float) * night])


def _lgbm(p: dict[str, Any]):
    import lightgbm as lgb

    return lgb.LGBMRegressor(
        n_estimators=p["n_estimators"], learning_rate=p["learning_rate"], num_leaves=p["num_leaves"],
        min_child_samples=p["min_child_samples"], subsample=p["bagging_fraction"], subsample_freq=1,
        colsample_bytree=p["feature_fraction"], random_state=p.get("seed", 0), verbose=-1)


def fit_predict(train: pd.DataFrame, test: pd.DataFrame, cfg: dict[str, Any], features: list[str],
                methods: tuple[str, ...] = tuple(METHODS)) -> dict[str, np.ndarray]:
    """Predictions for the test rows from the requested methods, fitted on the train rows only."""
    m = cfg["model"]
    y = (train.t_obs - train.t_era5).to_numpy()
    out = {"era5_raw": test.t_era5.to_numpy()}
    if "era5_lapse" in methods:
        out["era5_lapse"] = test.t_era5_elev.to_numpy()
    if "era5_bias" in methods:
        out["era5_bias"] = test.t_era5.to_numpy() + y.mean()
    if "ridge" in methods:
        ridge = _ridge(m["ridge_alpha"]).fit(_ridge_matrix(train, features), y)
        out["ridge"] = test.t_era5.to_numpy() + ridge.predict(_ridge_matrix(test, features))
    if "lightgbm" in methods:
        gbm = _lgbm(m["lightgbm"]).fit(train[features], y)
        out["lightgbm"] = test.t_era5.to_numpy() + gbm.predict(test[features])
    return out


def leave_one_station_out(df: pd.DataFrame, cfg: dict[str, Any], verbose: bool = True,
                          features: list[str] | None = None, methods: tuple[str, ...] = tuple(METHODS)) -> pd.DataFrame:
    """Out-of-fold predictions for every station-hour. `features` defaults to every available feature."""
    df = add_time_features(df)
    features = features or feature_list(df)
    keep = ["station", "icao", "network", "time", "hour_local", "is_night", "month", "t_obs"]
    folds = []
    for st in sorted(df.station.unique()):
        test = df[df.station == st]
        train = df[df.station != st]
        preds = fit_predict(train, test, cfg, features, methods)
        f = test[[c for c in keep if c in test]].copy()
        for k, v in preds.items():
            f[f"pred_{k}"] = v
        folds.append(f)
        if verbose:
            mae = {k: np.abs(v - test.t_obs.to_numpy()).mean() for k, v in preds.items()}
            names = {"era5_raw": "ERA5", "lightgbm": "LightGBM", "ridge": "Ridge"}
            print(f"  held out {test.icao.iloc[0]:<5} " + "  ".join(f"{names[k]} {mae[k]:.2f}" for k in names if k in mae)
                  + " C", flush=True)
    return pd.concat(folds, ignore_index=True)


def _scores(err: np.ndarray) -> dict[str, float]:
    return {"n": int(err.size), "mae_c": float(np.abs(err).mean()),
            "rmse_c": float(np.sqrt((err ** 2).mean())), "bias_c": float(err.mean())}


def summarise(oof: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Overall, per-station and per-hour scores. Errors are prediction minus station."""
    rows, by_station, by_hour = [], [], []
    for m in [m for m in METHODS if f"pred_{m}" in oof]:
        e = oof[f"pred_{m}"] - oof.t_obs
        per_st = oof.assign(e=e).groupby("station").e
        rows.append({"method": m, **_scores(e.to_numpy()),
                     "mae_night_c": float(e[oof.is_night].abs().mean()),
                     "mae_day_c": float(e[~oof.is_night].abs().mean()),
                     "station_mae_mean_c": float(per_st.apply(lambda s: s.abs().mean()).mean()),
                     "station_bias_abs_mean_c": float(per_st.mean().abs().mean())})
        for net, g in oof.assign(e=e).groupby("network"):
            rows.append({"method": f"{m} [{net}]", **_scores(g.e.to_numpy())})
        for st, g in oof.assign(e=e).groupby("station"):
            by_station.append({"method": m, "station": st, "icao": g.icao.iloc[0], "network": g.network.iloc[0],
                               **_scores(g.e.to_numpy())})
        for h, g in oof.assign(e=e).groupby("hour_local"):
            by_hour.append({"method": m, "hour_local": int(h), **_scores(g.e.to_numpy())})
    return {"summary": pd.DataFrame(rows).round(3), "by_station": pd.DataFrame(by_station).round(3),
            "by_hour": pd.DataFrame(by_hour).round(3)}


def fit_final(df: pd.DataFrame, cfg: dict[str, Any]):
    """LightGBM on all stations, for maps and the API. Returns (model, features, gain importance)."""
    df = add_time_features(df)
    features = feature_list(df)
    gbm = _lgbm(cfg["model"]["lightgbm"]).fit(df[features], (df.t_obs - df.t_era5).to_numpy())
    gain = gbm.booster_.feature_importance(importance_type="gain")
    imp = pd.DataFrame({"feature": features, "gain_share": gain / max(gain.sum(), 1e-12)})
    return gbm, features, imp.sort_values("gain_share", ascending=False).round(4)


def save_final(gbm, features: list[str], path: str | Path) -> Path:
    import json

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    gbm.booster_.save_model(str(path))
    path.with_suffix(".features.json").write_text(json.dumps(features, indent=2))
    return path
