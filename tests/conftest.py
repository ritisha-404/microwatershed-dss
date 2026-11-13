import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

# Allow running the tests without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def write_dem(path, data, resolution=30.0, crs="EPSG:32643", nodata=-32768.0):
    """Write a float32 GeoTIFF for tests (projected UTM by default)."""
    transform = from_origin(500_000.0, 1_960_000.0, resolution, resolution)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": data.shape[1],
        "height": data.shape[0],
        "count": 1,
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data.astype("float32"), 1)
    return str(path)


@pytest.fixture
def tilted_plane_dem(tmp_path):
    """A 50x50 plane dipping east at exactly 10 degrees, 30 m cells."""
    resolution = 30.0
    cols = np.arange(50, dtype="float64")
    z = np.tile(cols * resolution * np.tan(np.radians(10.0)), (50, 1))
    return write_dem(tmp_path / "plane.tif", z, resolution=resolution)


# ---------------------------------------------------------------------------
# Synthetic DEMs with analytically known drainage, shared by the hydrology
# tests (test_flow, test_delineation, test_delineate_cli).
# ---------------------------------------------------------------------------

DIAMOND_OUTLET = (30, 70)  # (row, col) of the designed outlet cell
DIAMOND_CATCHMENT_CELLS = 29 * 59 + 1  # rows 16..44 x cols 11..69, + outlet


def diamond_valley():
    """61x81 DEM with a fully interior basin draining east through (30, 70).

    z(r, c) = A(r) + B(c):
      A: valley cross-section, minimum at row 30, divides at |r-30| = 15,
         descending away from the basin beyond the divides.
      B: along-valley profile with a local crest at col 10, dipping gently
         east of it (the valley) and steeply west of it (off the west edge);
         east of col 70 the channel plunges steeply so flow exits east.

    Gradients are chosen so interior basin cells flow cardinally toward the
    valley axis (drop 3 > diagonal (3+1)/sqrt(2)), then east along the axis.
    West of the col-10 crest the westward drop (6) makes flow leave the basin
    (diagonally west+valleyward, (6+3)/sqrt(2) > 3), so the divide is sharp.
    The exact catchment of (30, 70) is rows 16..44 x cols 11..69 plus the
    outlet cell itself: 29 * 59 + 1 = 1712 cells.
    """
    rows = np.arange(61)[:, None].astype("float64")
    cols = np.arange(81)[None, :].astype("float64")
    dr = np.abs(rows - 30.0)
    A = np.where(dr <= 15.0, 3.0 * dr, 45.0 - 6.0 * (dr - 15.0))
    B = np.where(
        cols >= 10.0,
        np.where(cols <= 70.0, 100.0 - (cols - 10.0), 40.0 - 6.0 * (cols - 70.0)),
        100.0 - 6.0 * (10.0 - cols),
    )
    return A + B


def cell_latlon(dem_path, row, col):
    """WGS84 lat/lon of a cell centre of a projected test DEM."""
    from rasterio.warp import transform as warp_transform

    with rasterio.open(dem_path) as src:
        x, y = rasterio.transform.xy(src.transform, row, col)
        lons, lats = warp_transform(src.crs, "EPSG:4326", [x], [y])
    return float(lats[0]), float(lons[0])
