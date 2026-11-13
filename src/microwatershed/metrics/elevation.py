"""Elevation statistics over the area of interest."""

from __future__ import annotations

import numpy as np

from .base import MEASURED, Metric, RasterData

METHODOLOGY = "methodology/elevation_statistics.md"


def elevation_metrics(dem: RasterData) -> list[Metric]:
    """Min, max, mean and range of elevation from valid cells in the AOI."""
    samples = dem.samples()
    if samples.size == 0:
        raise ValueError("No valid elevation cells inside the area of interest.")

    zmin = float(np.min(samples))
    zmax = float(np.max(samples))
    zmean = float(np.mean(samples))

    def m(name: str, value: float, description: str) -> Metric:
        return Metric(
            name=name,
            value=round(value, 2),
            unit="m",
            kind=MEASURED,
            description=description,
            methodology=METHODOLOGY,
        )

    return [
        m("min_elevation", zmin, "Lowest elevation cell in the area of interest."),
        m("max_elevation", zmax, "Highest elevation cell in the area of interest."),
        m("mean_elevation", zmean, "Arithmetic mean of all valid elevation cells."),
        m("elevation_range", zmax - zmin, "Local relief: max elevation minus min elevation."),
    ]
