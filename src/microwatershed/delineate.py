"""Command-line entry point for automatic watershed delineation (Milestone 2).

Usage:
    python -m microwatershed.delineate --lat 17.68 --lon 74.02
    python -m microwatershed.delineate --lat 17.68 --lon 74.02 --snap-distance 200
    python -m microwatershed.delineate --dem my_dem.tif --lat 17.68 --lon 74.02

Given an outlet point, the tool downloads elevation coverage around it,
conditions the DEM, computes D8 flow direction and accumulation, snaps the
outlet to the drainage network, delineates the upstream catchment, and runs
the Milestone 1 terrain assessment over the catchment.

DEM coverage starts at ``--radius`` km and is automatically doubled (up to
``--max-radius``) whenever the delineated catchment reaches the edge of the
available data, so arbitrary catchment shapes are supported. If the catchment
still reaches the boundary at the maximum radius, the run fails with a clear
message instead of returning a truncated catchment.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import rasterio
from rasterio.warp import transform as warp_transform

from .data import acquisition, validation
from .hydrology import delineation, terrain
from .metrics import catchment as catchment_metrics
from .metrics import elevation, slope
from .metrics.base import load_raster
from .qgis_export import QgisExportError, build_watershed_project
from .report import watershed_html, watershed_json

log = logging.getLogger("microwatershed")

DEFAULT_INITIAL_RADIUS_KM = 2.0
DEFAULT_MAX_RADIUS_KM = 20.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m microwatershed.delineate",
        description="Delineate the upstream catchment draining through an outlet point. "
        "Elevation data is downloaded automatically and expanded until the catchment fits.",
    )
    parser.add_argument("--lat", type=float, required=True,
                        help="Latitude of the outlet point (WGS84 decimal degrees).")
    parser.add_argument("--lon", type=float, required=True,
                        help="Longitude of the outlet point (WGS84 decimal degrees).")
    parser.add_argument("--snap-distance", type=float, default=150.0,
                        help="Maximum distance in metres to move the outlet onto the "
                        "drainage network (default: 150).")
    parser.add_argument("--stream-threshold", type=int, default=100,
                        help="Minimum upstream cells for a cell to count as drainage "
                        "network when snapping (default: 100, i.e. 0.09 km2 at 30 m).")
    parser.add_argument("--radius", type=float, default=DEFAULT_INITIAL_RADIUS_KM,
                        help=f"Initial DEM radius around the outlet in km (default: "
                        f"{DEFAULT_INITIAL_RADIUS_KM:g}).")
    parser.add_argument("--max-radius", type=float, default=DEFAULT_MAX_RADIUS_KM,
                        help="Maximum DEM radius in km before giving up on catchments that "
                        f"reach the data boundary (default: {DEFAULT_MAX_RADIUS_KM:g}, "
                        f"limit {validation.MAX_RADIUS_KM:g}).")
    parser.add_argument("--dem", type=str, default=None,
                        help="Optional: delineate on a local GeoTIFF DEM instead of "
                        "downloading data. Coverage cannot be expanded automatically; if "
                        "the catchment reaches the DEM edge the run fails.")
    parser.add_argument("--output-dir", type=str, default="outputs/watershed",
                        help="Directory for results (default: outputs/watershed/).")
    parser.add_argument("--no-whitebox", action="store_true",
                        help="Skip WhiteboxTools and use the built-in hydrology engine.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    return parser


def _truncation_message(result: delineation.DelineationResult, user_dem: bool, radius_km: float) -> str:
    sides = ", ".join(result.boundary_sides)
    if user_dem:
        return (
            f"The catchment reaches the boundary of the supplied DEM ({sides}). The true "
            "catchment extends beyond the available elevation data, so a reliable boundary "
            "cannot be delineated. Supply a DEM with wider coverage around the outlet."
        )
    return (
        f"The catchment still reaches the edge of the elevation data ({sides}) at the "
        f"maximum DEM radius of {radius_km:g} km. The upstream area is larger than this "
        "tool's configured coverage; increase --max-radius (up to "
        f"{validation.MAX_RADIUS_KM:g} km) or verify the outlet location. No catchment "
        "boundary was produced because it would be truncated."
    )


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    out_dir = Path(args.output_dir)
    warnings: list[str] = []
    expansion_history: list[dict] = []

    try:
        validation.validate_point(args.lat, args.lon)
        validation.validate_radius(args.radius)
        validation.validate_radius(args.max_radius)
        if args.max_radius < args.radius:
            log.error("--max-radius (%g) must be at least --radius (%g).", args.max_radius, args.radius)
            return 2

        out_dir.mkdir(parents=True, exist_ok=True)
        dem_path = str(out_dir / "dem.tif")

        # --- Acquire + delineate, expanding DEM coverage as needed. ---------
        radius = args.radius
        user_dem = args.dem is not None
        while True:
            if user_dem:
                log.info("Using user-supplied DEM: %s", args.dem)
                provenance = acquisition.prepare_user_dem(args.dem, dem_path, args.lat, args.lon)
            else:
                log.info("Acquiring elevation data around %.4f, %.4f (radius %.1f km) ...",
                         args.lat, args.lon, radius)
                provenance = acquisition.fetch_dem(args.lat, args.lon, radius, dem_path)
                if provenance.n_missing_tiles:
                    warnings.append(
                        f"{provenance.n_missing_tiles} of {provenance.n_tiles} terrain tiles "
                        "were unavailable; affected cells are treated as nodata."
                    )

            dem_check = validation.validate_dem(dem_path)
            log.info("DEM validated: %dx%d px at %.1f m, %.1f%% valid data.",
                     dem_check.width, dem_check.height, dem_check.resolution_m,
                     dem_check.valid_fraction * 100)

            result = delineation.delineate(
                dem_path, args.lat, args.lon,
                snap_distance_m=args.snap_distance,
                stream_threshold_cells=args.stream_threshold,
                prefer_whitebox=not args.no_whitebox,
            )
            expansion_history.append({
                "radius_km": radius if not user_dem else None,
                "touches_boundary": result.touches_boundary,
                "boundary_sides": result.boundary_sides,
                "catchment_cells": result.cell_count,
            })

            if not result.touches_boundary:
                break
            if user_dem or radius >= args.max_radius:
                log.error("%s", _truncation_message(result, user_dem, radius))
                return 1
            radius = min(radius * 2.0, args.max_radius)
            log.info("Catchment reaches the DEM boundary (%s); expanding coverage to %.1f km.",
                     ", ".join(result.boundary_sides), radius)

        warnings.extend(dem_check.warnings)
        warnings.extend(result.warnings)

        # The catchment is accepted: write the delineation outputs.
        delineation_paths = delineation.write_outputs(result, out_dir)

        # --- Terrain assessment over the catchment (Milestone 1 engine). ----
        slope_path = str(out_dir / "slope.tif")
        engine = terrain.compute_slope(dem_path, slope_path, prefer_whitebox=not args.no_whitebox)
        log.info("Slope computed with engine: %s", engine)

        dem_raster = load_raster(dem_path)
        slope_raster = load_raster(slope_path)
        accum_raster = load_raster(delineation_paths["flow_accumulation_geotiff"])
        dem_raster.aoi_mask = result.catchment_mask
        slope_raster.aoi_mask = result.catchment_mask

        metrics = catchment_metrics.catchment_area_metrics(result.cell_count, result.resolution_m)
        metrics += elevation.elevation_metrics(dem_raster)
        metrics += slope.slope_metrics(slope_raster)
        metrics.append(slope.slope_classification(slope_raster))

        # --- QGIS project over the accepted outputs (display only). ---------
        qgis_outputs = build_watershed_project(out_dir)
        log.info("QGIS project written: %s", qgis_outputs["qgis_project"])

        # --- Reports. --------------------------------------------------------
        json_path = str(out_dir / "watershed_assessment.json")
        html_path = str(out_dir / "watershed_assessment.html")
        outputs = {
            "dem_geotiff": dem_path,
            "slope_geotiff": slope_path,
            **delineation_paths,
            **qgis_outputs,
            "json_report": json_path,
            "html_report": html_path,
        }
        report = watershed_json.build_watershed_report(
            lat=args.lat,
            lon=args.lon,
            snap_distance_m=args.snap_distance,
            stream_threshold_cells=args.stream_threshold,
            dem=provenance,
            validation=dem_check,
            delineation=result,
            slope_engine=engine,
            metrics=metrics,
            warnings=warnings,
            outputs=outputs,
            expansion_history=expansion_history,
        )
        watershed_json.write_report(report, json_path)

        with rasterio.open(dem_path) as src:
            xs, ys = warp_transform("EPSG:4326", src.crs, [args.lon], [args.lat])
            req_row, req_col = rasterio.transform.rowcol(src.transform, xs[0], ys[0])
        watershed_html.write_watershed_html_report(
            report, dem_raster, slope_raster, accum_raster,
            result.catchment_mask,
            (result.snapped.row, result.snapped.col),
            (int(req_row), int(req_col)),
            html_path,
        )

    except (validation.ValidationError, acquisition.AcquisitionError,
            delineation.DelineationError, terrain.TerrainProcessingError,
            QgisExportError) as exc:
        log.error("%s", exc)
        return 1
    except ValueError as exc:
        log.error("%s", exc)
        return 1

    for warning in warnings:
        log.warning("%s", warning)

    snapped = result.snapped
    print("\nWatershed delineation complete.")
    print(f"  Snapped outlet     {snapped.lat:.6f}, {snapped.lon:.6f} "
          f"(moved {snapped.distance_moved_m:.0f} m)")
    print(f"  Catchment area     {result.area_ha:,.1f} ha ({result.area_km2:,.4f} km2, "
          f"{result.cell_count:,} cells)")
    print(f"  Hydrology engine   {result.engine}")
    for metric in metrics:
        if metric.name not in ("slope_classification", "catchment_area_ha", "catchment_area_km2"):
            print(f"  {metric.name:<18} {metric.value:>10,.1f} {metric.unit}  [{metric.kind}]")
    print(f"\nResults written to {out_dir}/:")
    for key, value in outputs.items():
        print(f"  {key:<26} {value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
