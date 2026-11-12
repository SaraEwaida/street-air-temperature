"""Load a city config (YAML)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    with path.open() as f:
        cfg = yaml.safe_load(f)
    missing = [k for k in ("city", "bbox", "crs", "period", "stations", "era5", "features", "paths") if k not in cfg]
    if missing:
        raise ValueError(f"{path}: missing keys {missing}")
    return cfg
