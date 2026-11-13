"""Input and DEM validation.

Validation happens at two points:
1. Before acquisition: coordinates and radius must be plausible.
2. After acquisition (or when the user supplies a GeoTIFF): the DEM must be
   readable, georeferenced, and contain plausible elevation values with
   adequate coverage of the area of interest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import rasterio

# Plausible elevation bounds for land-surface DEMs (metres above EGM96 geoid).
# The Dead Sea shore is ~-430 m; Everest is 8849 m. Bathymetry in global
# tilesets can reach ~-11000 m, so values below MIN_PLAUSIBLE_M usually mean
# the AOI is open ocean or the tile decode went wrong.
MIN_PLAUSIBLE_M = -500.0
MAX_PLAUSIBLE_M = 9000.0

# Analysis radius limits (km). The upper bound keeps tile downloads and
# processing bounded; this is a microwatershed tool, not a basin-scale one.
MIN_RADIUS_KM = 0.25
MAX_RADIUS_KM = 50.0

# Minimum fraction of the AOI that must contain valid elevation data.
MIN_VALID_COVERAGE = 0.5


class ValidationError(Exception):
    """Raised when inputs or acquired data fail validation."""


@dataclass
class DemValidationResult:
    """Outcome of DEM validation, kept for the report's provenance section."""

    path: str
    crs: str
    width: int
    height: int
    resolution_m: float
    valid_fraction: float
    min_elevation: float
    max_elevation: float
    warnings: list[str] = field(default_factory=list)


def validate_point(lat: float, lon: float) -> None:
    """Check that a latitude/longitude pair is a usable coordinate."""
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValidationError(f"Coordinates must be finite numbers, got lat={lat}, lon={lon}.")
    if not -90.0 <= lat <= 90.0:
        raise ValidationError(f"Latitude {lat} is outside the valid range [-90, 90].")
    if not -180.0 <= lon <= 180.0:
        raise ValidationError(f"Longitude {lon} is outside the valid range [-180, 180].")
    if abs(lat) > 85.0:
        raise ValidationError(
            f"Latitude {lat} is outside the coverage of the web-mercator terrain "
            "tileset (|lat| <= 85). Supply a local GeoTIFF with --dem instead."
        )


def validate_radius(radius_km: float) -> None:
    """Check that the analysis radius is within supported limits."""
    if not math.isfinite(radius_km):
        raise ValidationError(f"Radius must be a finite number, got {radius_km}.")
    if not MIN_RADIUS_KM <= radius_km <= MAX_RADIUS_KM:
        raise ValidationError(
            f"Radius {radius_km} km is outside the supported range "
            f"[{MIN_RADIUS_KM}, {MAX_RADIUS_KM}] km."
        )


def validate_dem(path: str) -> DemValidationResult:
    """Validate a DEM GeoTIFF before analysis.

    Checks: file opens, has a CRS, has data, elevations are plausible, and
    valid-data coverage meets MIN_VALID_COVERAGE. Returns a summary used in
    the report; raises ValidationError on hard failures.
    """
    warnings: list[str] = []
    try:
        src = rasterio.open(path)
    except rasterio.errors.RasterioIOError as exc:
        raise ValidationError(f"Cannot open DEM '{path}': {exc}") from exc

    with src:
        if src.crs is None:
            raise ValidationError(f"DEM '{path}' has no coordinate reference system.")
        if src.width < 10 or src.height < 10:
            raise ValidationError(
                f"DEM '{path}' is too small for analysis ({src.width}x{src.height} px); "
                "need at least 10x10 pixels. Try a larger radius."
            )

        data = src.read(1, masked=True).astype("float64")
        if src.crs.is_projected:
            resolution_m = float(abs(src.transform.a))
        else:
            # Approximate ground resolution of a geographic-CRS DEM at its centre.
            centre_lat = (src.bounds.top + src.bounds.bottom) / 2.0
            resolution_m = float(abs(src.transform.a) * 111_320.0 * math.cos(math.radians(centre_lat)))

    valid = np.asarray(~np.ma.getmaskarray(data) & np.isfinite(data.data))
    valid_fraction = float(valid.sum()) / float(valid.size)
    if valid_fraction == 0.0:
        raise ValidationError(f"DEM '{path}' contains no valid elevation data.")
    if valid_fraction < MIN_VALID_COVERAGE:
        raise ValidationError(
            f"DEM '{path}' covers only {valid_fraction:.0%} of the area of interest "
            f"with valid data (minimum {MIN_VALID_COVERAGE:.0%}). The location may be "
            "outside the tileset's data extent."
        )
    if valid_fraction < 1.0:
        warnings.append(
            f"{1 - valid_fraction:.1%} of the area of interest has no elevation data; "
            "metrics are computed from valid cells only."
        )

    values = data.compressed() if np.ma.isMaskedArray(data) else data[valid]
    zmin, zmax = float(values.min()), float(values.max())
    if zmax < MIN_PLAUSIBLE_M or zmin > MAX_PLAUSIBLE_M:
        raise ValidationError(
            f"DEM '{path}' elevations ({zmin:.0f}..{zmax:.0f} m) are outside the "
            f"plausible land-surface range [{MIN_PLAUSIBLE_M:.0f}, {MAX_PLAUSIBLE_M:.0f}] m. "
            "The area may be open ocean, or the file is not an elevation raster."
        )
    if zmin < MIN_PLAUSIBLE_M:
        warnings.append(
            f"Minimum elevation {zmin:.0f} m is below {MIN_PLAUSIBLE_M:.0f} m; the area "
            "likely includes sea/bathymetry cells from the global tileset."
        )
    if zmax > MAX_PLAUSIBLE_M:
        warnings.append(f"Maximum elevation {zmax:.0f} m exceeds {MAX_PLAUSIBLE_M:.0f} m; check the data source.")

    with rasterio.open(path) as src:
        return DemValidationResult(
            path=path,
            crs=str(src.crs),
            width=src.width,
            height=src.height,
            resolution_m=resolution_m,
            valid_fraction=valid_fraction,
            min_elevation=zmin,
            max_elevation=zmax,
            warnings=warnings,
        )
