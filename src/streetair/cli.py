"""Command line: sat stations | era5 | features | build | map | model | grid | catalog | all."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .config import load_config


def _stations(cfg):
    from . import azmet, isd

    sources = cfg["stations"].get("sources", ["noaa-isd"])
    lists, frames = [], []
    if "noaa-isd" in sources:
        st = isd.list_stations(cfg).assign(network="noaa-isd")
        print(f"{len(st)} NOAA ISD stations in the bbox active for the whole period")
        raw_dir = Path(cfg["paths"]["observations"]).parent / "raw" / "isd"
        years = range(int(cfg["period"]["start"][:4]), int(cfg["period"]["end"][:4]) + 1)
        for s in st.itertuples():
            for y in years:
                path = isd.download(s.station, y, raw_dir)
                if path is None:
                    print(f"  {s.station} {s.name}: no {y} data at NOAA, skipped")
                    continue
                frames.append(isd.parse_hourly(path))
        lists.append(st)
    if "azmet" in sources:
        st = azmet.list_stations(cfg).assign(network="azmet")
        print(f"{len(st)} AZMet stations in the bbox: {', '.join(st.name)}")
        for s in st.itertuples():
            print(f"  downloading {s.name}...", flush=True)
            frames.append(azmet.fetch(s.station, cfg))
        lists.append(st)
    if not frames:
        raise RuntimeError("No station data downloaded; check network access to the sources")
    st = pd.concat(lists, ignore_index=True)
    obs = pd.concat(frames, ignore_index=True)
    obs = obs[obs.time.between(pd.Timestamp(cfg["period"]["start"], tz="UTC"),
                               pd.Timestamp(cfg["period"]["end"], tz="UTC") + pd.Timedelta(hours=23))]
    clean, report = isd.quality_control(obs, cfg)
    report = report.merge(st[["station", "name", "icao", "network"]], on="station", how="left")
    print(report.to_string(index=False))
    kept = st[st.station.isin(clean.station.unique())]
    for key in ("stations", "observations"):
        Path(cfg["paths"][key]).parent.mkdir(parents=True, exist_ok=True)
    kept.to_csv(cfg["paths"]["stations"], index=False)
    clean.to_parquet(cfg["paths"]["observations"], index=False)
    Path("outputs").mkdir(exist_ok=True)
    report.to_csv("outputs/qc_report.csv", index=False)
    print(f"Kept {len(kept)} stations ({', '.join(f'{k}: {v}' for k, v in kept.network.value_counts().items())}), "
          f"{len(clean):,} station-hours -> {cfg['paths']['observations']}")


def _era5(cfg):
    from . import era5

    st = pd.read_csv(cfg["paths"]["stations"], dtype={"station": str})
    df = era5.fetch(st, cfg)
    df.to_parquet(cfg["paths"]["era5"], index=False)
    print(f"ERA5 for {df.station.nunique()} stations, {len(df):,} hours -> {cfg['paths']['era5']}")


def _features(cfg):
    from .features import site_features

    st = pd.read_csv(cfg["paths"]["stations"], dtype={"station": str})
    print(f"Site features for {len(st)} stations (Planetary Computer)...")
    f = site_features(st, cfg)
    f.to_csv(cfg["paths"]["features"], index=False)
    print(f"Saved {cfg['paths']['features']}")


def _build(cfg):
    from .dataset import build, station_summary, summarise

    st = pd.read_csv(cfg["paths"]["stations"], dtype={"station": str})
    df = build(pd.read_parquet(cfg["paths"]["observations"]), pd.read_parquet(cfg["paths"]["era5"]), st,
               pd.read_csv(cfg["paths"]["features"], dtype={"station": str}))
    df.to_parquet(cfg["paths"]["dataset"], index=False)
    summ = station_summary(df)
    Path(cfg["paths"]["station_summary"]).parent.mkdir(parents=True, exist_ok=True)
    summ.to_csv(cfg["paths"]["station_summary"], index=False)
    print(summ[["icao", "name", "network", "hours", "era5_bias_c", "era5_bias_night_c", "era5_bias_day_c",
                "frac_built_500m", "ndvi_500m"]].to_string(index=False))
    print(json.dumps(summarise(df)))
    print(f"Saved {cfg['paths']['dataset']} and {cfg['paths']['station_summary']}")


def _map(cfg):
    from .plots import built_background, station_map

    summ = pd.read_csv(cfg["paths"]["station_summary"], dtype={"station": str})
    st = pd.read_csv(cfg["paths"]["stations"], dtype={"station": str})
    out = station_map(summ, st, cfg, cfg["paths"]["station_map"], background=built_background(cfg))
    print(f"Saved {out}")


def _model(cfg):
    from . import model
    from .plots import error_by_hour, error_by_station

    df = pd.read_parquet(cfg["paths"]["dataset"])
    print(f"Leave-one-station-out over {df.station.nunique()} stations, {len(df):,} station-hours")
    oof = model.leave_one_station_out(df, cfg)
    oof.to_parquet(cfg["paths"]["oof"], index=False)
    res = model.summarise(oof)
    out = Path(cfg["paths"]["results_dir"])
    out.mkdir(parents=True, exist_ok=True)
    for name, table in res.items():
        table.to_csv(out / f"model_{name}.csv", index=False)
    gbm, features, imp = model.fit_final(df, cfg)
    model.save_final(gbm, features, cfg["paths"]["model_lightgbm"])
    imp.to_csv(out / "model_importance.csv", index=False)
    error_by_hour(res["by_hour"], out / "figures" / "error_by_hour.png")
    error_by_station(res["by_station"], out / "figures" / "error_by_station.png")
    s = res["summary"]
    print(s[s.method.isin(model.METHODS)][["method", "mae_c", "rmse_c", "bias_c", "mae_night_c", "mae_day_c",
                                          "station_bias_abs_mean_c"]].to_string(index=False))
    print(s[~s.method.isin(model.METHODS)][["method", "n", "mae_c", "bias_c"]].to_string(index=False))
    print("Top LightGBM features: " + ", ".join(f"{r.feature} {r.gain_share:.0%}" for r in imp.head(6).itertuples()))
    print(f"Saved {out}/model_*.csv, figures, and {cfg['paths']['model_lightgbm']}")


def _grid(cfg):
    from . import grid

    print("Site features for every pixel (Planetary Computer)...")
    g = grid.static_features(cfg)
    grid.save(g, cfg["paths"]["grid_features"])
    # Consistency: grid features at the station pixels vs the stations' own 10 m circle features
    st = pd.read_csv(cfg["paths"]["stations"], dtype={"station": str})
    sf = pd.read_csv(cfg["paths"]["features"], dtype={"station": str}).set_index("station").loc[st.station]
    at = grid.sample_at(g, st.lon.to_numpy(), st.lat.to_numpy())
    rows = [{"feature": k, "mean_abs_diff": float(np.nanmean(np.abs(at[k] - sf[k].to_numpy()))),
             "station_range": float(sf[k].max() - sf[k].min())} for k in at if k in sf]
    chk = pd.DataFrame(rows).round(3)
    chk.to_csv(Path(cfg["paths"]["results_dir"]) / "grid_feature_check.csv", index=False)
    print(chk.to_string(index=False))
    print(f"Grid {g['shape'][0]} x {g['shape'][1]}, {g['ndvi_scenes']} Landsat scenes -> {cfg['paths']['grid_features']}")


def _catalog(cfg):
    import json

    import lightgbm as lgb

    from . import catalog, grid
    from .plots import catalog_maps

    g = grid.load(cfg["paths"]["grid_features"])
    cells = catalog.era5_cells(cfg)
    times = catalog.local_times(cfg)
    print(f"ERA5 for {len(cells)} cells around {len(times)} map times...")
    e = catalog.fetch_era5_cells(cfg, cells, times)
    e.to_parquet(cfg["paths"]["era5_cells"], index=False)

    booster = lgb.Booster(model_file=cfg["paths"]["model_lightgbm"])
    features = json.loads(Path(cfg["paths"]["model_lightgbm"]).with_suffix(".features.json").read_text())
    cell_idx = catalog.pixel_cells(g, cells)
    land = np.isfinite(g["features"]["frac_water_100m"]) & (g["features"]["frac_water_100m"] <= cfg["catalog"]["water_threshold"])
    ranges = catalog.training_ranges(pd.read_csv(cfg["paths"]["features"]), pd.read_parquet(cfg["paths"]["dataset"]))
    elev_cell = e.groupby("station").era5_cell_elev_m.first().reindex(cells.station).to_numpy()
    site = {**g["features"], "elev_minus_cell_m": g["features"]["elev_dem_m"] - elev_cell[cell_idx]}
    in_range = catalog.support_mask(site, ranges) & land
    by_feat = catalog.support_by_feature(site, ranges, land)
    by_feat.to_csv(Path(cfg["paths"]["results_dir"]) / "catalog_support.csv", index=False)
    f = g["features"]
    zones = {"urban (built-up >= 30% within 500 m)": land & (f["frac_built_500m"] >= 0.3),
             "farmland (grass/crop >= 30% within 500 m)": land & (f["frac_grass_crop_500m"] >= 0.3),
             "natural desert (shrub >= 50% within 500 m)": land & (f["frac_shrub_500m"] >= 0.5)}
    print(f"Land pixels inside the training stations' range: {in_range[land].mean():.0%}")
    for name, z in zones.items():
        print(f"  {name}: {z.sum() / land.sum():.0%} of land, {in_range[z].mean() if z.any() else 0:.0%} in range")
    print("Most limiting features:")
    print(by_feat.head(6).to_string(index=False))
    maps = {}
    for t in times:
        et = e[e.time == t]
        if et.empty:
            raise RuntimeError(f"No ERA5 for {t}")
        maps[t] = catalog.predict_hour(g, cell_idx, et, t, booster, features, land)
        print(f"  {t.tz_convert('America/Phoenix'):%Y-%m-%d %H:%M} MST mapped", flush=True)
    info = {"model": "LightGBM residual over raw ERA5", "validation": "leave-one-station-out, 15 stations",
            "features": features}
    path = catalog.build_catalog(cfg, g, maps, in_range, cfg["paths"]["catalog_dir"], info)
    summ = catalog.summary(maps, in_range)
    summ.to_csv(Path(cfg["paths"]["results_dir"]) / "catalog_summary.csv", index=False)
    print(summ.to_string(index=False))
    st = pd.read_csv(cfg["paths"]["stations"], dtype={"station": str})
    fig_times = [pd.Timestamp(x).tz_localize("America/Phoenix").tz_convert("UTC") for x in cfg["catalog"]["figure_times_local"]]
    catalog_maps(maps, fig_times, g, cfg["paths"]["catalog_figure"], stations=st)
    print(f"Saved STAC collection {path} and {cfg['paths']['catalog_figure']}")


STEPS = {"stations": _stations, "era5": _era5, "features": _features, "build": _build, "map": _map,
         "model": _model, "grid": _grid, "catalog": _catalog}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="sat", description="Street-level air temperature pipeline")
    p.add_argument("step", choices=[*STEPS, "all"])
    p.add_argument("config")
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    for name in (STEPS if args.step == "all" else [args.step]):
        print(f"== {name}", flush=True)
        STEPS[name](cfg)


if __name__ == "__main__":
    main()
