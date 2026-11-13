"""Shared metric infrastructure.

Every metric records not just a value but its provenance ``kind``:

- ``measured``: read directly from the source DEM cells. The DEM itself is a
  measurement *product* (e.g. radar interferometry), so these values inherit
  the source's vertical accuracy, but no additional model is applied by this
  software.
- ``derived``: computed from measured values by a documented algorithm
  (e.g. slope from elevation differences). Derived values add algorithmic
  assumptions on top of the source data's accuracy.

Each metric also points at the methodology document that explains what it
measures, the formula used, data source, resolution, and limitations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import rasterio

MEASURED = "measured"
DERIVED = "derived"


@dataclass
class Metric:
    name: str
    value: Any
    unit: str
    kind: str  # MEASURED or DERIVED
    description: str
    methodology: str  # path of the methodology document, relative to docs/

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "kind": self.kind,
            "description": self.description,
            "methodology": self.methodology,
        }


@dataclass
class RasterData:
    """A raster band loaded for analysis, with nodata as NaN."""

    values: np.ndarray  # float64, NaN where invalid
    resolution_m: float
    transform: rasterio.Affine
    crs: rasterio.crs.CRS
    aoi_mask: np.ndarray | None = None  # True inside the area of interest

    @property
    def valid(self) -> np.ndarray:
        """Finite cells inside the AOI (whole raster if no AOI mask)."""
        mask = np.isfinite(self.values)
        if self.aoi_mask is not None:
            mask &= self.aoi_mask
        return mask

    def samples(self) -> np.ndarray:
        """1-D array of valid values used for statistics."""
        return self.values[self.valid]

    @property
    def cell_area_km2(self) -> float:
        return (self.resolution_m**2) / 1e6


def load_raster(path: str, aoi: tuple[float, float, float] | None = None) -> RasterData:
    """Load band 1 of a projected raster, optionally with a circular AOI mask.

    ``aoi`` is (lat, lon, radius_km); the circle is evaluated in the raster's
    projected CRS so distances are true metres.
    """
    with rasterio.open(path) as src:
        values = src.read(1).astype("float64")
        if src.nodata is not None:
            values[values == src.nodata] = np.nan
        data = RasterData(
            values=values,
            resolution_m=float(abs(src.transform.a)),
            transform=src.transform,
            crs=src.crs,
        )
        if aoi is not None:
            lat, lon, radius_km = aoi
            from rasterio.warp import transform as warp_transform

            xs, ys = warp_transform("EPSG:4326", src.crs, [lon], [lat])
            cx, cy = xs[0], ys[0]
            rows, cols = np.indices(values.shape)
            px, py = rasterio.transform.xy(src.transform, rows, cols)
            px = np.asarray(px).reshape(values.shape)
            py = np.asarray(py).reshape(values.shape)
            data.aoi_mask = (px - cx) ** 2 + (py - cy) ** 2 <= (radius_km * 1000.0) ** 2
    return data
