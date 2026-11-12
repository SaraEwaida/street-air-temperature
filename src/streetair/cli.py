"""Command line: sat stations | era5 | features | build | map | all."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

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


STEPS = {"stations": _stations, "era5": _era5, "features": _features, "build": _build, "map": _map}


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
