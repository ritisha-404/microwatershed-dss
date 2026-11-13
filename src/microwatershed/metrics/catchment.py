"""Catchment area metrics derived from the delineated watershed raster."""

from __future__ import annotations

from .base import DERIVED, Metric

METHODOLOGY = "methodology/catchment_area.md"


def catchment_area_metrics(cell_count: int, resolution_m: float) -> list[Metric]:
    """Catchment area in hectares and square kilometres from the cell count.

    The area is the number of catchment cells times the cell size squared,
    evaluated in the local UTM zone so cells are true square metres.
    """
    area_m2 = cell_count * resolution_m * resolution_m
    return [
        Metric(
            name="catchment_area_ha",
            value=round(area_m2 / 1e4, 2),
            unit="ha",
            kind=DERIVED,
            description="Area of the delineated upstream catchment in hectares.",
            methodology=METHODOLOGY,
        ),
        Metric(
            name="catchment_area_km2",
            value=round(area_m2 / 1e6, 4),
            unit="km2",
            kind=DERIVED,
            description="Area of the delineated upstream catchment in square kilometres.",
            methodology=METHODOLOGY,
        ),
    ]
