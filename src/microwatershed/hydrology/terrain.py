"""Terrain derivatives: slope and hillshade.

Slope is computed with WhiteboxTools when available (the native
``whitebox_tools`` binary on PATH, or the ``whitebox`` pip package), using
Horn's (1981) third-order finite difference method in degrees. When
WhiteboxTools is unavailable, an equivalent NumPy implementation of the same
Horn method is used; both paths are the same algorithm, and the engine
actually used is recorded in the report provenance.

All inputs must be projected DEMs with horizontal units in metres (the
acquisition module guarantees this).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import numpy as np
import rasterio

log = logging.getLogger(__name__)

SLOPE_NODATA = -9999.0


class TerrainProcessingError(Exception):
    """Raised when a terrain derivative cannot be computed."""


def compute_slope(dem_path: str, out_path: str, prefer_whitebox: bool = True) -> str:
    """Compute slope in degrees from a projected DEM GeoTIFF.

    Returns the name of the engine used: ``"whitebox-binary"``,
    ``"whitebox-pip"`` or ``"numpy-horn"``.
    """
    if prefer_whitebox:
        engine = _try_whitebox_slope(dem_path, out_path)
        if engine is not None:
            return engine
        log.info("WhiteboxTools not available; using the built-in NumPy Horn implementation.")
    _numpy_horn_slope(dem_path, out_path)
    return "numpy-horn"


def _try_whitebox_slope(dem_path: str, out_path: str) -> str | None:
    """Run WhiteboxTools Slope if a working installation can be found."""
    dem_abs, out_abs = str(Path(dem_path).resolve()), str(Path(out_path).resolve())

    binary = shutil.which("whitebox_tools")
    if binary:
        cmd = [
            binary,
            "--run=Slope",
            f"--dem={dem_abs}",
            f"--output={out_abs}",
            "--units=degrees",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and Path(out_abs).exists():
            return "whitebox-binary"
        log.warning("whitebox_tools binary failed (%s); falling back.", result.stderr.strip()[:200])

    try:
        import whitebox  # type: ignore

        wbt = whitebox.WhiteboxTools()
        wbt.verbose = False
        code = wbt.slope(dem_abs, out_abs, units="degrees")
        if code == 0 and Path(out_abs).exists():
            return "whitebox-pip"
        log.warning("whitebox pip package failed (exit %s); falling back.", code)
    except ImportError:
        pass
    except Exception as exc:  # whitebox raises bare Exceptions on runtime errors
        log.warning("whitebox pip package failed (%s); falling back.", exc)
    return None


def _numpy_horn_slope(dem_path: str, out_path: str) -> None:
    """Slope in degrees via Horn's (1981) 3x3 third-order finite difference.

    This is the same algorithm WhiteboxTools and GDAL use. Edge cells and
    cells whose 3x3 neighbourhood touches nodata are set to nodata.
    """
    with rasterio.open(dem_path) as src:
        if src.crs is None or not src.crs.is_projected:
            raise TerrainProcessingError(
                "Slope computation requires a projected DEM with metre units."
            )
        z = src.read(1).astype("float64")
        nodata = src.nodata
        profile = src.profile.copy()
        cellsize_x = abs(src.transform.a)
        cellsize_y = abs(src.transform.e)

    invalid = ~np.isfinite(z)
    if nodata is not None:
        invalid |= z == nodata
    z = np.where(invalid, np.nan, z)

    # 3x3 neighbourhood views, Horn weighting:
    #   dz/dx = ((c + 2f + i) - (a + 2d + g)) / (8 * dx)
    #   dz/dy = ((g + 2h + i) - (a + 2b + c)) / (8 * dy)
    # with the neighbourhood labelled  a b c / d e f / g h i.
    a = z[:-2, :-2]; b = z[:-2, 1:-1]; c = z[:-2, 2:]
    d = z[1:-1, :-2];                  f = z[1:-1, 2:]
    g = z[2:, :-2];  h = z[2:, 1:-1];  i = z[2:, 2:]

    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8.0 * cellsize_x)
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8.0 * cellsize_y)
    slope_deg = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))

    out = np.full(z.shape, SLOPE_NODATA, dtype="float32")
    inner = out[1:-1, 1:-1]
    # A cell needs a finite gradient AND a valid centre value (Horn's formula
    # does not reference the centre cell, but slope at a nodata cell is undefined).
    valid = np.isfinite(slope_deg) & np.isfinite(z[1:-1, 1:-1])
    inner[valid] = slope_deg[valid].astype("float32")

    for key in ("blockxsize", "blockysize", "tiled"):
        profile.pop(key, None)
    profile.update(dtype="float32", nodata=SLOPE_NODATA, compress="deflate")
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(out, 1)


def compute_hillshade(dem_path: str, out_path: str, azimuth: float = 315.0, altitude: float = 45.0) -> None:
    """Hillshade (0-255) for visualization only; not used in any metric."""
    with rasterio.open(dem_path) as src:
        z = src.read(1).astype("float64")
        nodata = src.nodata
        profile = src.profile.copy()
        dx = abs(src.transform.a)
        dy = abs(src.transform.e)

    invalid = ~np.isfinite(z)
    if nodata is not None:
        invalid |= z == nodata
    z = np.where(invalid, np.nan, z)

    gy, gx = np.gradient(z, dy, dx)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az = np.radians(360.0 - azimuth + 90.0)
    alt = np.radians(altitude)
    shaded = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    shaded = np.clip(shaded, 0.0, 1.0) * 255.0

    out = np.where(np.isfinite(shaded), shaded, 0.0).astype("uint8")
    for key in ("blockxsize", "blockysize", "tiled"):
        profile.pop(key, None)
    profile.update(dtype="uint8", nodata=0, compress="deflate")
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(out, 1)
