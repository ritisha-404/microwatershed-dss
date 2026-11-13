"""Assemble the watershed delineation JSON report (Milestone 2)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .. import __version__
from ..data.acquisition import AcquiredDem
from ..data.validation import DemValidationResult
from ..hydrology.delineation import DelineationResult
from ..metrics.base import Metric
from .json_report import PROVENANCE_NOTE, write_report  # noqa: F401  (re-exported)

DELINEATION_NOTE = (
    "The catchment is delineated on a hydrologically conditioned copy of the DEM "
    "using the D8 single-flow-direction model; terrain metrics are computed from "
    "the original (unconditioned) elevations within the catchment. The boundary "
    "follows 30 m raster cells and is a model result, not a surveyed divide."
)


def build_watershed_report(
    *,
    lat: float,
    lon: float,
    snap_distance_m: float,
    stream_threshold_cells: int,
    dem: AcquiredDem,
    validation: DemValidationResult,
    delineation: DelineationResult,
    slope_engine: str,
    metrics: list[Metric],
    warnings: list[str],
    outputs: dict[str, str],
    expansion_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "application": {"name": "microwatershed-dss", "version": __version__},
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request": {
            "outlet_latitude": lat,
            "outlet_longitude": lon,
            "snap_distance_m": snap_distance_m,
            "stream_threshold_cells": stream_threshold_cells,
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
            "hydrology_engine": delineation.engine,
            "depression_treatment": delineation.conditioning,
            "flow_model": "D8 single flow direction (O'Callaghan & Mark 1984), Whitebox pointer encoding",
            "outlet_snapping": (
                f"nearest cell with >= {stream_threshold_cells} upstream cells within "
                f"{snap_distance_m:g} m (Jenson 1991)"
            ),
            "slope_engine": slope_engine,
            "slope_algorithm": "Horn (1981) 3x3 third-order finite difference, degrees",
        },
        "delineation": {
            "snapped_outlet": delineation.snapped.to_dict(),
            "catchment": {
                "cell_count": delineation.cell_count,
                "area_km2": round(delineation.area_km2, 4),
                "area_ha": round(delineation.area_ha, 2),
                "cell_size_m": delineation.resolution_m,
            },
            "validation_checks": delineation.validation,
            "depression_treatment_comparison": delineation.depression_comparison,
            "boundary": {
                "touches_data_boundary": delineation.touches_boundary,
                "sides": delineation.boundary_sides,
            },
            "dem_expansion_history": expansion_history or [],
        },
        "metrics": [m.to_dict() for m in metrics],
        "warnings": warnings,
        "outputs": outputs,
        "provenance_note": PROVENANCE_NOTE,
        "delineation_note": DELINEATION_NOTE,
    }
