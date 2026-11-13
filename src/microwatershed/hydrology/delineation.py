"""Watershed delineation from an outlet point.

Pipeline (each intermediate output is validated before the next step):

1. Hydrological conditioning of the DEM. Preferred: WhiteboxTools
   BreachDepressionsLeastCost (breaching preserves real flow paths through
   spurious dams such as road embankments in ~30 m DEMs) followed by
   FillDepressions with flat fixing for anything breaching could not resolve.
   Fallback: the built-in priority-flood fill (flow.fill_depressions).
   When both engines are available the effects of pure filling and of
   breach+fill are compared and recorded in the result.
2. D8 flow pointer (WhiteboxTools D8Pointer or flow.d8_pointer).
   Validated: no interior cell may lack a flow direction (unresolved pit or
   flat); if WhiteboxTools output fails this, the built-in engine recomputes
   everything from the original DEM.
3. D8 flow accumulation (WhiteboxTools D8FlowAccumulation, out_type=cells,
   or flow.flow_accumulation). Validated by mass balance: the accumulation
   summed over cells where flow leaves the valid area must equal the number
   of valid cells, i.e. no water is created or lost.
4. Outlet snapping to the nearest drainage cell (Jenson-style: nearest cell
   with at least ``stream_threshold_cells`` upstream cells, so the point
   does not slide downstream the way maximum-accumulation snapping does).
5. Upstream watershed traversal of the validated pointer, catchment area,
   and boundary-contact detection: a catchment touching the raster edge or
   nodata cells is flagged as potentially truncated — callers must either
   expand DEM coverage or report failure, never return it silently.
6. Raster outputs and a GeoJSON catchment polygon in WGS84.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio import features
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom

from . import flow

log = logging.getLogger(__name__)

BREACH_DIST_CELLS = 50  # max breach-channel search distance, in cells
MIN_RELIEF_M = 1.0  # below this the DEM is effectively flat
MIN_SNAP_DISTANCE_M = 0.0
MAX_SNAP_DISTANCE_M = 2000.0
POINTER_NODATA = 255
ACCUM_NODATA = -9999.0
CATCHMENT_NODATA = 255


class DelineationError(Exception):
    """Raised when a catchment cannot be reliably delineated."""


@dataclass
class SnappedOutlet:
    row: int
    col: int
    x: float
    y: float
    lat: float
    lon: float
    distance_moved_m: float
    accumulation_cells: float
    upstream_area_km2: float
    on_stream: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "latitude": round(self.lat, 6),
            "longitude": round(self.lon, 6),
            "projected_x": round(self.x, 2),
            "projected_y": round(self.y, 2),
            "distance_moved_m": round(self.distance_moved_m, 1),
            "flow_accumulation_cells": int(self.accumulation_cells),
            "upstream_area_km2": round(self.upstream_area_km2, 4),
            "on_drainage_network": self.on_stream,
        }


@dataclass
class DelineationResult:
    engine: str  # whitebox-binary | whitebox-pip | numpy
    conditioning: str
    snapped: SnappedOutlet
    catchment_mask: np.ndarray
    valid_mask: np.ndarray
    cell_count: int
    area_km2: float
    area_ha: float
    touches_boundary: bool
    boundary_sides: list[str]
    resolution_m: float
    crs: str
    validation: dict[str, Any]
    depression_comparison: dict[str, Any] | None
    geojson: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    # Raster data carried in memory so that nothing is written to disk until
    # the caller accepts the result (see write_outputs): a truncated catchment
    # must never leave files behind that look like a valid delineation.
    conditioned_dem: np.ndarray = field(default=None, repr=False)
    pointer: np.ndarray = field(default=None, repr=False)
    accumulation: np.ndarray = field(default=None, repr=False)
    transform: Any = field(default=None, repr=False)
    crs_obj: Any = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# WhiteboxTools access (binary on PATH preferred, pip package as fallback).
# ---------------------------------------------------------------------------


class _Whitebox:
    """Thin runner around a WhiteboxTools installation, if one exists."""

    def __init__(self) -> None:
        self.name: str | None = None
        self._binary = shutil.which("whitebox_tools")
        self._wbt = None
        if self._binary:
            self.name = "whitebox-binary"
            return
        try:
            import whitebox  # type: ignore

            self._wbt = whitebox.WhiteboxTools()
            self._wbt.verbose = False
            self.name = "whitebox-pip"
        except Exception:  # ImportError, or the pip package failing to bootstrap
            self.name = None

    @property
    def available(self) -> bool:
        return self.name is not None

    def run(self, tool: str, args: list[str], expect: str) -> bool:
        """Run one tool; True when it succeeds and the expected output exists."""
        try:
            if self._binary:
                result = subprocess.run(
                    [self._binary, f"--run={tool}", *args], capture_output=True, text=True
                )
                ok = result.returncode == 0
                if not ok:
                    log.warning("WhiteboxTools %s failed: %s", tool, result.stderr.strip()[:300])
            else:
                code = self._wbt.run_tool(tool, args)
                ok = code == 0
                if not ok:
                    log.warning("WhiteboxTools %s failed (exit %s).", tool, code)
        except Exception as exc:  # the pip wrapper raises bare Exceptions
            log.warning("WhiteboxTools %s failed (%s).", tool, exc)
            return False
        return ok and Path(expect).exists()


# ---------------------------------------------------------------------------
# Raster helpers.
# ---------------------------------------------------------------------------


def _read_dem(dem_path: str) -> tuple[np.ndarray, rasterio.Affine, rasterio.crs.CRS, float]:
    with rasterio.open(dem_path) as src:
        if src.crs is None or not src.crs.is_projected:
            raise DelineationError("Watershed delineation requires a projected DEM in metre units.")
        z = src.read(1).astype("float64")
        if src.nodata is not None:
            z[z == src.nodata] = np.nan
        z[~np.isfinite(z)] = np.nan
        return z, src.transform, src.crs, float(abs(src.transform.a))


def _write_raster(path: str, data: np.ndarray, transform, crs, dtype: str, nodata) -> None:
    profile = {
        "driver": "GTiff",
        "dtype": dtype,
        "width": data.shape[1],
        "height": data.shape[0],
        "count": 1,
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data.astype(dtype), 1)


def _read_band(path: str) -> np.ndarray:
    with rasterio.open(path) as src:
        data = src.read(1).astype("float64")
        if src.nodata is not None:
            data[data == src.nodata] = np.nan
        return data


def _modification_stats(original: np.ndarray, modified: np.ndarray) -> dict[str, Any]:
    both = np.isfinite(original) & np.isfinite(modified)
    delta = np.where(both, modified - original, 0.0)
    changed = np.abs(delta) > 1e-4
    return {
        "cells_modified": int(changed.sum()),
        "max_raise_m": round(float(delta.max(initial=0.0)), 3),
        "max_lower_m": round(float(-delta.min(initial=0.0)), 3),
    }


# ---------------------------------------------------------------------------
# Hydrological conditioning + flow computation.
# ---------------------------------------------------------------------------


def _numpy_hydrology(z: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    """Condition and compute pointer/accumulation with the built-in engine."""
    conditioned = flow.fill_depressions(z)
    pointer = flow.d8_pointer(conditioned)
    valid = np.isfinite(conditioned)
    unresolved = flow.unresolved_interior_mask(pointer, valid)
    if unresolved.any():  # cannot happen with epsilon filling; defensive
        raise DelineationError(
            f"{int(unresolved.sum())} interior cells have no flow direction after "
            "depression filling; the DEM cannot be conditioned."
        )
    return conditioned, pointer, flow.flow_accumulation(flow.downstream_index(pointer, valid), valid), (
        "priority-flood depression filling (Barnes et al. 2014) with epsilon gradient"
    )


def _whitebox_hydrology(
    z: np.ndarray,
    transform,
    crs,
    wbt: _Whitebox,
    compare_depressions: bool,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, str, dict | None] | None:
    """Condition and compute pointer/accumulation with WhiteboxTools.

    Returns None when any step fails or fails validation, so the caller can
    fall back to the built-in engine. Intermediates are written as float64 so
    small conditioning increments survive the round trip.
    """
    with tempfile.TemporaryDirectory(prefix="microwatershed-wbt-") as tmp:
        tmp = Path(tmp)
        dem64 = str(tmp / "dem64.tif")
        breached = str(tmp / "breached.tif")
        conditioned_path = str(tmp / "conditioned.tif")
        filled_only = str(tmp / "filled_only.tif")
        pointer_path = str(tmp / "pointer.tif")
        accum_path = str(tmp / "accum.tif")

        _write_raster(dem64, np.where(np.isfinite(z), z, -32768.0), transform, crs, "float64", -32768.0)

        ok = wbt.run(
            "BreachDepressionsLeastCost",
            [f"--dem={dem64}", f"--output={breached}", f"--dist={BREACH_DIST_CELLS}", "--fill"],
            expect=breached,
        )
        if not ok:
            return None
        # Fix any remaining flats (breach+fill does not apply a flat gradient).
        ok = wbt.run(
            "FillDepressions",
            [f"--dem={breached}", f"--output={conditioned_path}", "--fix_flats"],
            expect=conditioned_path,
        )
        if not ok:
            return None

        comparison = None
        if compare_depressions:
            if wbt.run(
                "FillDepressions",
                [f"--dem={dem64}", f"--output={filled_only}", "--fix_flats"],
                expect=filled_only,
            ):
                comparison = {
                    "breach_then_fill": _modification_stats(z, _read_band(conditioned_path)),
                    "fill_only": _modification_stats(z, _read_band(filled_only)),
                    "selected": "breach_then_fill",
                    "rationale": (
                        "Breaching cuts through spurious dams instead of raising "
                        "elevations across them, preserving real flow paths; "
                        "remaining depressions and flats are then filled."
                    ),
                }

        ok = wbt.run("D8Pointer", [f"--dem={conditioned_path}", f"--output={pointer_path}"], expect=pointer_path)
        if not ok:
            return None

        conditioned = _read_band(conditioned_path)
        with rasterio.open(pointer_path) as src:
            pointer_raw = src.read(1, masked=True)
        pointer = np.asarray(pointer_raw.filled(0), dtype="float64")
        pointer = np.where(np.isin(pointer, flow.D8_CODES), pointer, 0).astype("uint8")

        valid = np.isfinite(z)
        unresolved = flow.unresolved_interior_mask(pointer, valid)
        if unresolved.any():
            log.warning(
                "WhiteboxTools flow pointer leaves %d interior cells unresolved; "
                "falling back to the built-in engine.",
                int(unresolved.sum()),
            )
            return None

        ok = wbt.run(
            "D8FlowAccumulation",
            [f"--input={pointer_path}", f"--output={accum_path}", "--out_type=cells", "--pntr"],
            expect=accum_path,
        )
        if not ok:
            return None
        accum = _read_band(accum_path)
        accum = np.where(np.isfinite(accum), accum, 0.0)

        return conditioned, pointer, accum, (
            f"least-cost breaching (dist {BREACH_DIST_CELLS} cells) + depression "
            "filling with flat fixing (WhiteboxTools)"
        ), comparison


def _check_mass_balance(ds: np.ndarray, accum: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    terminals = flow.terminal_mask(ds, valid)
    terminal_sum = float(accum[terminals].sum())
    n_valid = int(valid.sum())
    relative_error = abs(terminal_sum - n_valid) / max(n_valid, 1)
    return {
        "terminal_accumulation_sum": terminal_sum,
        "valid_cells": n_valid,
        "relative_error": relative_error,
        "passed": relative_error < 1e-6,
    }


# ---------------------------------------------------------------------------
# GeoJSON output.
# ---------------------------------------------------------------------------


def _catchment_geojson(
    catchment: np.ndarray,
    transform,
    crs,
    snapped: SnappedOutlet,
    requested: tuple[float, float],
    area_km2: float,
    area_ha: float,
    resolution_m: float,
) -> dict[str, Any]:
    shapes = features.shapes(
        catchment.astype("uint8"), mask=catchment, transform=transform, connectivity=8
    )
    polygons = [geom for geom, value in shapes]
    if not polygons:
        raise DelineationError("Catchment raster produced no polygon.")
    if len(polygons) == 1:
        geometry = polygons[0]
    else:
        geometry = {"type": "MultiPolygon", "coordinates": [p["coordinates"] for p in polygons]}
    geometry = transform_geom(crs, "EPSG:4326", geometry, precision=6)

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "name": "catchment",
                    "area_km2": round(area_km2, 4),
                    "area_ha": round(area_ha, 2),
                    "cell_count": int(catchment.sum()),
                    "cell_size_m": resolution_m,
                    "analysis_crs": str(crs),
                },
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(snapped.lon, 6), round(snapped.lat, 6)]},
                "properties": {"name": "outlet_snapped", **snapped.to_dict()},
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(requested[1], 6), round(requested[0], 6)]},
                "properties": {"name": "outlet_requested"},
            },
        ],
    }


# ---------------------------------------------------------------------------
# Main entry point.
# ---------------------------------------------------------------------------


def delineate(
    dem_path: str,
    lat: float,
    lon: float,
    snap_distance_m: float = 150.0,
    stream_threshold_cells: int = 100,
    prefer_whitebox: bool = True,
    compare_depressions: bool = True,
) -> DelineationResult:
    """Delineate the catchment draining through the outlet point.

    Computes everything in memory and returns the full result; nothing is
    written to disk (use :func:`write_outputs` once the result is accepted).
    Raises DelineationError when no reliable catchment can be produced. A
    result with ``touches_boundary`` set means the catchment may be truncated
    by the DEM extent — the caller must expand coverage or report the
    problem, never present the polygon as complete.
    """
    if not MIN_SNAP_DISTANCE_M <= snap_distance_m <= MAX_SNAP_DISTANCE_M:
        raise DelineationError(
            f"Snap distance {snap_distance_m} m is outside the supported range "
            f"[{MIN_SNAP_DISTANCE_M:g}, {MAX_SNAP_DISTANCE_M:g}] m."
        )
    warnings: list[str] = []

    z, transform, crs, resolution_m = _read_dem(dem_path)
    valid = np.isfinite(z)

    # Locate the outlet on the grid.
    xs, ys = warp_transform("EPSG:4326", crs, [lon], [lat])
    row, col = rasterio.transform.rowcol(transform, xs[0], ys[0])
    row, col = int(row), int(col)
    if not (0 <= row < z.shape[0] and 0 <= col < z.shape[1]):
        raise DelineationError("The outlet point lies outside the DEM extent.")
    if not valid[row, col]:
        raise DelineationError("The outlet point lies on a nodata cell of the DEM.")

    relief = float(np.nanmax(z) - np.nanmin(z))
    if relief < MIN_RELIEF_M:
        warnings.append(
            f"The DEM is effectively flat (total relief {relief:.2f} m). D8 flow "
            "directions are arbitrary on flat terrain and the delineated catchment "
            "is not reliable."
        )

    # Conditioning + pointer + accumulation, WhiteboxTools first.
    engine = "numpy"
    comparison: dict | None = None
    wbt_result = None
    if prefer_whitebox:
        wbt = _Whitebox()
        if wbt.available:
            wbt_result = _whitebox_hydrology(z, transform, crs, wbt, compare_depressions)
            if wbt_result is not None:
                conditioned, pointer, accum, conditioning, comparison = wbt_result
                engine = wbt.name
            else:
                warnings.append(
                    "WhiteboxTools output failed validation; hydrology was recomputed "
                    "with the built-in engine."
                )
        else:
            log.info("WhiteboxTools not available; using the built-in hydrology engine.")
    if wbt_result is None:
        conditioned, pointer, accum, conditioning = _numpy_hydrology(z)

    ds = flow.downstream_index(pointer, valid)
    mass_balance = _check_mass_balance(ds, accum, valid)
    if not mass_balance["passed"] and engine != "numpy":
        warnings.append(
            "WhiteboxTools flow accumulation failed the mass-balance check "
            f"(relative error {mass_balance['relative_error']:.2e}); hydrology was "
            "recomputed with the built-in engine."
        )
        conditioned, pointer, accum, conditioning = _numpy_hydrology(z)
        comparison = None
        engine = "numpy"
        ds = flow.downstream_index(pointer, valid)
        mass_balance = _check_mass_balance(ds, accum, valid)
    if not mass_balance["passed"]:
        raise DelineationError(
            "Flow accumulation failed the mass-balance check (terminal accumulation "
            f"{mass_balance['terminal_accumulation_sum']:.0f} vs {mass_balance['valid_cells']} "
            "valid cells); the flow model is inconsistent."
        )

    pits_after = int(flow.interior_pit_mask(conditioned).sum())
    unresolved_after = int(flow.unresolved_interior_mask(pointer, valid).sum())

    # Snap the outlet to the drainage network.
    snap_row, snap_col, on_stream = flow.snap_outlet(
        accum, valid, row, col, snap_distance_m, resolution_m, stream_threshold_cells
    )
    if not on_stream:
        warnings.append(
            f"No drainage cell with at least {stream_threshold_cells} upstream cells was "
            f"found within {snap_distance_m:g} m of the outlet; the highest-accumulation "
            "cell in range was used instead. The point may not lie on a drainage "
            "network, and the catchment may be very small."
        )
    sx, sy = rasterio.transform.xy(transform, snap_row, snap_col)
    lons, lats = warp_transform(crs, "EPSG:4326", [sx], [sy])
    outlet_accum = float(accum[snap_row, snap_col])
    snapped = SnappedOutlet(
        row=snap_row,
        col=snap_col,
        x=float(sx),
        y=float(sy),
        lat=float(lats[0]),
        lon=float(lons[0]),
        distance_moved_m=float(np.hypot(snap_row - row, snap_col - col) * resolution_m),
        accumulation_cells=outlet_accum,
        upstream_area_km2=outlet_accum * resolution_m**2 / 1e6,
        on_stream=on_stream,
    )

    # Delineate and measure.
    catchment = flow.watershed(ds, valid, snap_row, snap_col)
    cell_count = int(catchment.sum())
    if cell_count != int(round(outlet_accum)):
        # The watershed is exactly the set of cells counted by the outlet's
        # accumulation; a mismatch means pointer and accumulation disagree.
        raise DelineationError(
            f"Catchment size ({cell_count} cells) does not match the outlet's flow "
            f"accumulation ({outlet_accum:.0f} cells); flow outputs are inconsistent."
        )
    area_km2 = cell_count * resolution_m**2 / 1e6
    area_ha = area_km2 * 100.0

    touches, sides = flow.touches_boundary(catchment, valid)

    geojson = _catchment_geojson(
        catchment, transform, crs, snapped, (lat, lon), area_km2, area_ha, resolution_m
    )

    return DelineationResult(
        engine=engine,
        conditioning=conditioning,
        snapped=snapped,
        catchment_mask=catchment,
        valid_mask=valid,
        cell_count=cell_count,
        area_km2=area_km2,
        area_ha=area_ha,
        touches_boundary=touches,
        boundary_sides=sides,
        resolution_m=resolution_m,
        crs=str(crs),
        validation={
            "interior_pits_after_conditioning": pits_after,
            "unresolved_interior_cells": unresolved_after,
            "mass_balance": {
                "terminal_accumulation_sum": mass_balance["terminal_accumulation_sum"],
                "valid_cells": mass_balance["valid_cells"],
                "relative_error": mass_balance["relative_error"],
                "passed": mass_balance["passed"],
            },
            "catchment_equals_outlet_accumulation": True,
            "total_relief_m": round(relief, 2),
        },
        depression_comparison=comparison,
        geojson=geojson,
        warnings=warnings,
        conditioned_dem=conditioned,
        pointer=pointer,
        accumulation=accum,
        transform=transform,
        crs_obj=crs,
    )


def write_outputs(result: DelineationResult, out_dir: str | Path) -> dict[str, str]:
    """Write the accepted delineation's rasters and GeoJSON to ``out_dir``.

    Kept separate from :func:`delineate` so that rejected results (e.g. a
    catchment truncated by the DEM boundary) never leave output files behind.
    Returns the mapping of output names to paths.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    valid = result.valid_mask
    transform, crs = result.transform, result.crs_obj
    paths = {
        "dem_conditioned_geotiff": str(out_dir / "dem_conditioned.tif"),
        "flow_direction_geotiff": str(out_dir / "flow_direction.tif"),
        "flow_accumulation_geotiff": str(out_dir / "flow_accumulation.tif"),
        "catchment_geotiff": str(out_dir / "catchment.tif"),
        "catchment_geojson": str(out_dir / "catchment.geojson"),
    }
    _write_raster(
        paths["dem_conditioned_geotiff"],
        np.where(valid, result.conditioned_dem, -32768.0),
        transform, crs, "float32", -32768.0,
    )
    _write_raster(
        paths["flow_direction_geotiff"],
        np.where(valid, result.pointer, POINTER_NODATA),
        transform, crs, "uint8", POINTER_NODATA,
    )
    _write_raster(
        paths["flow_accumulation_geotiff"],
        np.where(valid, result.accumulation, ACCUM_NODATA),
        transform, crs, "float32", ACCUM_NODATA,
    )
    _write_raster(
        paths["catchment_geotiff"],
        np.where(valid, result.catchment_mask.astype("uint8"), CATCHMENT_NODATA),
        transform, crs, "uint8", CATCHMENT_NODATA,
    )
    with open(paths["catchment_geojson"], "w", encoding="utf-8") as fh:
        json.dump(result.geojson, fh, indent=2)
        fh.write("\n")
    return paths
