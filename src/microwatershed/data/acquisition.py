"""Automatic DEM acquisition from the AWS Terrain Tiles public dataset.

Source: https://registry.opendata.aws/terrain-tiles/ (Mapzen/Nextzen terrain
tiles hosted by AWS Open Data; no API key required). Tiles are 256x256 PNG
images in web-mercator (EPSG:3857) "Terrarium" encoding:

    elevation_m = (R * 256 + G + B / 256) - 32768

The underlying elevation comes from a blend of public DEMs (predominantly
SRTM v3 ~30 m between 60N and 56S, with GMTED2010, ETOPO1 and regional
sources elsewhere). See docs/methodology/dem_source.md for details.

This module downloads the tiles covering an area of interest, mosaics and
decodes them, clips to the AOI, and reprojects to a local UTM zone so that
horizontal units are metres (required for slope computation).
"""

from __future__ import annotations

import logging
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds

from .validation import ValidationError

log = logging.getLogger(__name__)

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TILE_SIZE = 256
WEB_MERCATOR_ORIGIN = 20037508.342789244  # metres; half the EPSG:3857 world width
NODATA = -32768.0
MAX_TILES = 400  # safety cap on downloads per run
TARGET_RESOLUTION_M = 30.0  # match the native resolution of the SRTM source data
MIN_ZOOM, MAX_ZOOM = 9, 14
DOWNLOAD_RETRIES = 3
RETRY_WAIT_S = 2.0


class AcquisitionError(Exception):
    """Raised when DEM data cannot be obtained from the remote source."""


@dataclass
class AcquiredDem:
    """Provenance record for an automatically acquired DEM."""

    path: str
    source: str
    zoom: int
    n_tiles: int
    n_missing_tiles: int
    native_resolution_m: float
    crs: str


def geographic_bbox(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """Bounding box (west, south, east, north) of a radius around a point.

    Uses the spherical small-distance approximation, which is accurate to well
    under 1% for radii up to tens of kilometres.
    """
    dlat = radius_km / 110.574
    dlon = radius_km / (111.320 * math.cos(math.radians(lat)))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def choose_zoom(lat: float) -> int:
    """Zoom level whose pixel size best matches the source data resolution.

    Terrarium pixel size at zoom z and latitude phi is
    156543.03 * cos(phi) / 2**z metres. Sampling finer than the ~30 m source
    data adds no information; coarser loses it.
    """
    ground_res_z0 = 156543.03392 * math.cos(math.radians(lat))
    zoom = round(math.log2(ground_res_z0 / TARGET_RESOLUTION_M))
    return max(MIN_ZOOM, min(MAX_ZOOM, zoom))


def deg2tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """Slippy-map tile (x, y) containing a geographic point."""
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def decode_terrarium(rgb: np.ndarray) -> np.ndarray:
    """Decode a (3, H, W) Terrarium RGB array to elevation in metres."""
    r = rgb[0].astype("float64")
    g = rgb[1].astype("float64")
    b = rgb[2].astype("float64")
    return r * 256.0 + g + b / 256.0 - 32768.0


def _download_tile(zoom: int, x: int, y: int) -> np.ndarray | None:
    """Fetch and decode one tile. Returns None if the tile does not exist (404)."""
    url = TILE_URL.format(z=zoom, x=x, y=y)
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "microwatershed-dss/0.1"})
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
            import warnings

            with warnings.catch_warnings():
                # PNG tiles carry no georeferencing; we build the transform ourselves.
                warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
                with MemoryFile(payload) as memfile, memfile.open() as png:
                    rgb = png.read()
            if rgb.shape[0] < 3:
                raise AcquisitionError(f"Tile {url} has {rgb.shape[0]} bands; expected RGB.")
            return decode_terrarium(rgb[:3])
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last_error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt < DOWNLOAD_RETRIES:
            time.sleep(RETRY_WAIT_S * attempt)
    raise AcquisitionError(
        f"Failed to download {url} after {DOWNLOAD_RETRIES} attempts: {last_error}. "
        "Check your network connection, or supply a local GeoTIFF with --dem."
    )


def fetch_dem(lat: float, lon: float, radius_km: float, out_path: str) -> AcquiredDem:
    """Download, mosaic, clip and reproject a DEM for the area of interest.

    Writes a GeoTIFF in the local UTM zone (horizontal units: metres) to
    ``out_path`` and returns its provenance record.
    """
    west, south, east, north = geographic_bbox(lat, lon, radius_km)
    zoom = choose_zoom(lat)
    x0, y0 = deg2tile(north, west, zoom)
    x1, y1 = deg2tile(south, east, zoom)
    nx, ny = x1 - x0 + 1, y1 - y0 + 1
    if nx * ny > MAX_TILES:
        raise AcquisitionError(
            f"Area of interest needs {nx * ny} tiles at zoom {zoom} (cap {MAX_TILES}). "
            "Reduce the analysis radius."
        )

    log.info("Downloading %d terrain tiles at zoom %d ...", nx * ny, zoom)
    mosaic = np.full((ny * TILE_SIZE, nx * TILE_SIZE), np.nan, dtype="float64")
    n_missing = 0
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            tile = _download_tile(zoom, tx, ty)
            if tile is None:
                n_missing += 1
                log.warning("Tile %d/%d/%d not available; filling with nodata.", zoom, tx, ty)
                continue
            row, col = (ty - y0) * TILE_SIZE, (tx - x0) * TILE_SIZE
            mosaic[row : row + TILE_SIZE, col : col + TILE_SIZE] = tile

    if n_missing == nx * ny:
        raise AcquisitionError(
            f"No terrain tiles exist for this location at zoom {zoom}; "
            "cannot produce a DEM."
        )

    # Georeference the mosaic in EPSG:3857.
    world = 2.0 * WEB_MERCATOR_ORIGIN
    tile_span = world / (2**zoom)
    resolution = tile_span / TILE_SIZE
    transform = from_origin(
        -WEB_MERCATOR_ORIGIN + x0 * tile_span,
        WEB_MERCATOR_ORIGIN - y0 * tile_span,
        resolution,
        resolution,
    )
    src_crs = rasterio.crs.CRS.from_epsg(3857)

    # Clip the mosaic to the AOI bounding box.
    bounds_3857 = transform_bounds("EPSG:4326", src_crs, west, south, east, north)
    window = rasterio.windows.from_bounds(*bounds_3857, transform=transform)
    window = window.round_offsets().round_lengths()
    row_off, col_off = int(window.row_off), int(window.col_off)
    h, w = int(window.height), int(window.width)
    row_off, col_off = max(row_off, 0), max(col_off, 0)
    h = min(h, mosaic.shape[0] - row_off)
    w = min(w, mosaic.shape[1] - col_off)
    clipped = mosaic[row_off : row_off + h, col_off : col_off + w]
    clip_transform = rasterio.windows.transform(
        rasterio.windows.Window(col_off, row_off, w, h), transform
    )

    # Reproject to the local UTM zone so pixel units are true metres.
    utm = utm_crs(lat, lon)
    dst_transform, dst_w, dst_h = calculate_default_transform(
        src_crs, utm, w, h, *rasterio.windows.bounds(rasterio.windows.Window(col_off, row_off, w, h), transform)
    )
    dst = np.full((dst_h, dst_w), NODATA, dtype="float32")
    source = np.where(np.isnan(clipped), NODATA, clipped).astype("float32")
    reproject(
        source=source,
        destination=dst,
        src_transform=clip_transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=utm,
        src_nodata=NODATA,
        dst_nodata=NODATA,
        resampling=Resampling.bilinear,
    )

    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": dst_w,
        "height": dst_h,
        "count": 1,
        "crs": utm,
        "transform": dst_transform,
        "nodata": NODATA,
        "compress": "deflate",
    }
    with rasterio.open(out_path, "w", **profile) as out:
        out.write(dst, 1)

    native_res = float(abs(dst_transform.a))
    log.info("DEM written to %s (%dx%d px, %.1f m resolution, %s)", out_path, dst_w, dst_h, native_res, utm)
    return AcquiredDem(
        path=out_path,
        source="AWS Terrain Tiles (Mapzen Terrarium), s3://elevation-tiles-prod",
        zoom=zoom,
        n_tiles=nx * ny,
        n_missing_tiles=n_missing,
        native_resolution_m=native_res,
        crs=str(utm),
    )


def utm_crs(lat: float, lon: float) -> rasterio.crs.CRS:
    """EPSG code of the WGS84 UTM zone containing a point."""
    zone = int((lon + 180.0) // 6.0) + 1
    zone = min(max(zone, 1), 60)
    epsg = (32600 if lat >= 0 else 32700) + zone
    return rasterio.crs.CRS.from_epsg(epsg)


def prepare_user_dem(dem_path: str, out_path: str, lat: float | None = None, lon: float | None = None) -> AcquiredDem:
    """Prepare a user-supplied GeoTIFF for analysis.

    If the DEM is in a geographic CRS it is reprojected to the local UTM zone
    (slope needs horizontal units in metres). Projected DEMs are copied as-is.
    """
    try:
        src = rasterio.open(dem_path)
    except rasterio.errors.RasterioIOError as exc:
        raise ValidationError(f"Cannot open DEM '{dem_path}': {exc}") from exc

    with src:
        if src.crs is None:
            raise ValidationError(f"DEM '{dem_path}' has no coordinate reference system.")
        nodata = src.nodata if src.nodata is not None else NODATA
        if src.crs.is_projected:
            data = src.read(1).astype("float32")
            profile = src.profile.copy()
            profile.update(driver="GTiff", dtype="float32", count=1, nodata=nodata, compress="deflate")
            with rasterio.open(out_path, "w", **profile) as out:
                out.write(data, 1)
            res = float(abs(src.transform.a))
            crs = str(src.crs)
        else:
            centre_lon = (src.bounds.left + src.bounds.right) / 2.0
            centre_lat = (src.bounds.top + src.bounds.bottom) / 2.0
            utm = utm_crs(lat if lat is not None else centre_lat, lon if lon is not None else centre_lon)
            dst_transform, dst_w, dst_h = calculate_default_transform(
                src.crs, utm, src.width, src.height, *src.bounds
            )
            dst = np.full((dst_h, dst_w), nodata, dtype="float32")
            reproject(
                source=src.read(1),
                destination=dst,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dst_transform,
                dst_crs=utm,
                src_nodata=src.nodata,
                dst_nodata=nodata,
                resampling=Resampling.bilinear,
            )
            profile = {
                "driver": "GTiff",
                "dtype": "float32",
                "width": dst_w,
                "height": dst_h,
                "count": 1,
                "crs": utm,
                "transform": dst_transform,
                "nodata": nodata,
                "compress": "deflate",
            }
            with rasterio.open(out_path, "w", **profile) as out:
                out.write(dst, 1)
            res = float(abs(dst_transform.a))
            crs = str(utm)

    return AcquiredDem(
        path=out_path,
        source=f"User-supplied GeoTIFF: {dem_path}",
        zoom=-1,
        n_tiles=0,
        n_missing_tiles=0,
        native_resolution_m=res,
        crs=crs,
    )
