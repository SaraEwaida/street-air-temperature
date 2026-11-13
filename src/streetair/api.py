"""FastAPI service: street-level 2 m air temperature for a point and hour.

Run locally:   uvicorn streetair.api:app --reload
Config path:   environment variable SAT_CONFIG (default configs/phoenix.yaml)
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .config import load_config
from .service import PointError, Service

app = FastAPI(
    title="Street-level air temperature, Phoenix",
    description="ERA5 2 m air temperature corrected to street level from land cover, vegetation, elevation "
                "and weather. Trained on 15 weather stations and validated leave-one-station-out. "
                "Source: https://github.com/SaraEwaida/street-air-temperature",
    version="0.1.0",
)


@lru_cache(maxsize=1)
def get_service() -> Service:
    return Service.from_config(load_config(os.environ.get("SAT_CONFIG", "configs/phoenix.yaml")))


@app.get("/health")
def health() -> dict:
    s = get_service()
    return {"status": "ok", "city": s.cfg["city"], "grid_shape": list(s.grid["shape"]),
            "model_features": len(s.features), "area_of_applicability": s.dissimilarity_path is not None}


@app.get("/temperature")
def temperature(
    lat: float = Query(..., description="Latitude (WGS84)", examples=[33.4484]),
    lon: float = Query(..., description="Longitude (WGS84)", examples=[-112.074]),
    time: str = Query(..., description="ISO time; without a zone it is read as Phoenix local time (MST)",
                      examples=["2023-07-15T05:00"]),
) -> dict:
    try:
        return get_service().predict(lat, lon, time)
    except PointError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@app.get("/catalog")
def catalog() -> JSONResponse:
    path = Path(get_service().cfg["paths"]["catalog_dir"]) / "collection.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No catalog yet; run `sat catalog`")
    return JSONResponse(json.loads(path.read_text()))
