"""Slope statistics and classification over the area of interest."""

from __future__ import annotations

import numpy as np

from .base import DERIVED, Metric, RasterData

SLOPE_METHODOLOGY = "methodology/slope.md"
CLASSIFICATION_METHODOLOGY = "methodology/slope_classification.md"

# Slope classes in degrees, [low, high) bounds — an FAO-style scheme commonly
# used in terrain analysis. See docs/methodology/slope_classification.md.
SLOPE_CLASSES = [
    ("flat_or_almost_flat", 0.0, 2.0, "Flat or almost flat"),
    ("gently_sloping", 2.0, 5.0, "Gently sloping"),
    ("moderately_sloping", 5.0, 10.0, "Moderately sloping"),
    ("strongly_sloping", 10.0, 15.0, "Strongly sloping"),
    ("steep", 15.0, 30.0, "Steep"),
    ("very_steep", 30.0, 90.0001, "Very steep"),
]


def slope_metrics(slope: RasterData) -> list[Metric]:
    """Mean slope in degrees over valid cells in the AOI."""
    samples = slope.samples()
    if samples.size == 0:
        raise ValueError("No valid slope cells inside the area of interest.")
    return [
        Metric(
            name="mean_slope",
            value=round(float(np.mean(samples)), 2),
            unit="degrees",
            kind=DERIVED,
            description="Arithmetic mean of per-cell slope (Horn's method) over the area of interest.",
            methodology=SLOPE_METHODOLOGY,
        )
    ]


def slope_classification(slope: RasterData) -> Metric:
    """Distribution of AOI area across FAO-style slope classes."""
    samples = slope.samples()
    if samples.size == 0:
        raise ValueError("No valid slope cells inside the area of interest.")

    cell_km2 = slope.cell_area_km2
    total_km2 = samples.size * cell_km2
    classes = []
    for key, low, high, label in SLOPE_CLASSES:
        count = int(np.count_nonzero((samples >= low) & (samples < high)))
        classes.append(
            {
                "class": key,
                "label": label,
                "range_degrees": [low, min(high, 90.0)],
                "area_km2": round(count * cell_km2, 4),
                "percent_of_area": round(100.0 * count / samples.size, 2),
            }
        )

    return Metric(
        name="slope_classification",
        value={"total_assessed_area_km2": round(total_km2, 4), "classes": classes},
        unit="km2 / percent",
        kind=DERIVED,
        description="Area distribution across slope classes (degrees), FAO-style class bounds.",
        methodology=CLASSIFICATION_METHODOLOGY,
    )
