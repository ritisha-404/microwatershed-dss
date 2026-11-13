"""Assemble and write the terrain assessment JSON report."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .. import __version__
from ..data.acquisition import AcquiredDem
from ..data.validation import DemValidationResult
from ..metrics.base import Metric

PROVENANCE_NOTE = (
    "Metrics marked 'measured' are read directly from DEM cells and inherit the "
    "source data's vertical accuracy; metrics marked 'derived' are additionally "
    "computed by a documented algorithm (see each metric's methodology document). "
    "The DEM itself is a measurement product (radar interferometry and similar), "
    "not a ground survey."
)


def build_report(
    *,
    lat: float | None,
    lon: float | None,
    radius_km: float | None,
    dem: AcquiredDem,
    validation: DemValidationResult,
    slope_engine: str,
    metrics: list[Metric],
    warnings: list[str],
    outputs: dict[str, str],
) -> dict[str, Any]:
    return {
        "application": {"name": "microwatershed-dss", "version": __version__},
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request": {
            "latitude": lat,
            "longitude": lon,
            "radius_km": radius_km,
            "area_of_interest": (
                "circle of radius_km around the point" if radius_km is not None else "full extent of the supplied DEM"
            ),
        },
        "data_source": {
            "description": dem.source,
            "tile_zoom_level": dem.zoom if dem.zoom >= 0 else None,
            "tiles_downloaded": dem.n_tiles,
            "tiles_missing": dem.n_missing_tiles,
            "native_resolution_m": round(dem.native_resolution_m, 2),
            "analysis_crs": dem.crs,
            "validation": asdict(validation),
        },
        "processing": {
            "slope_engine": slope_engine,
            "slope_algorithm": "Horn (1981) 3x3 third-order finite difference, degrees",
        },
        "metrics": [m.to_dict() for m in metrics],
        "warnings": warnings,
        "outputs": outputs,
        "provenance_note": PROVENANCE_NOTE,
    }


def write_report(report: dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
        fh.write("\n")
