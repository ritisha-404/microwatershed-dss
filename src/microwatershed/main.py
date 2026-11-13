"""Command-line entry point for the Microwatershed DSS terrain assessment.

Usage:
    python -m microwatershed.main --lat 17.68 --lon 74.02 --radius 2
    python -m microwatershed.main --dem path/to/dem.tif
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .data import acquisition, validation
from .hydrology import terrain
from .metrics import elevation, slope
from .metrics.base import load_raster
from .report import html_report, json_report

log = logging.getLogger("microwatershed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m microwatershed.main",
        description="Generate a terrain assessment for a geographic area. "
        "Elevation data is downloaded automatically; no shapefile or DEM is required.",
    )
    parser.add_argument("--lat", type=float, help="Latitude of the point of interest (WGS84 decimal degrees).")
    parser.add_argument("--lon", type=float, help="Longitude of the point of interest (WGS84 decimal degrees).")
    parser.add_argument(
        "--radius", type=float, default=2.0, help="Analysis radius around the point in km (default: 2)."
    )
    parser.add_argument(
        "--dem",
        type=str,
        default=None,
        help="Optional: path to a GeoTIFF DEM to analyse instead of downloading data. "
        "With --lat/--lon the analysis is restricted to the radius; otherwise the full extent is used.",
    )
    parser.add_argument(
        "--output-dir", type=str, default="outputs", help="Directory for reports and rasters (default: outputs/)."
    )
    parser.add_argument(
        "--no-whitebox",
        action="store_true",
        help="Skip WhiteboxTools and use the built-in NumPy slope implementation.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    return parser


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.dem is None and (args.lat is None or args.lon is None):
        log.error("Provide --lat and --lon (or a local DEM with --dem).")
        return 2

    warnings: list[str] = []
    aoi = None
    try:
        if args.lat is not None and args.lon is not None:
            validation.validate_point(args.lat, args.lon)
            validation.validate_radius(args.radius)
            aoi = (args.lat, args.lon, args.radius)

        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        dem_path = str(out_dir / "dem.tif")
        slope_path = str(out_dir / "slope.tif")
        hillshade_path = str(out_dir / "hillshade.tif")

        # 1. Acquire the DEM.
        if args.dem is not None:
            log.info("Using user-supplied DEM: %s", args.dem)
            provenance = acquisition.prepare_user_dem(args.dem, dem_path, args.lat, args.lon)
        else:
            log.info("Acquiring elevation data for %.4f, %.4f (radius %.1f km) ...", args.lat, args.lon, args.radius)
            provenance = acquisition.fetch_dem(args.lat, args.lon, args.radius, dem_path)
            if provenance.n_missing_tiles:
                warnings.append(
                    f"{provenance.n_missing_tiles} of {provenance.n_tiles} terrain tiles were "
                    "unavailable; affected cells are treated as nodata."
                )

        # 2. Validate before analysis.
        dem_check = validation.validate_dem(dem_path)
        warnings.extend(dem_check.warnings)
        log.info(
            "DEM validated: %dx%d px at %.1f m, %.1f%% valid data.",
            dem_check.width,
            dem_check.height,
            dem_check.resolution_m,
            dem_check.valid_fraction * 100,
        )

        # 3. Terrain processing.
        engine = terrain.compute_slope(dem_path, slope_path, prefer_whitebox=not args.no_whitebox)
        log.info("Slope computed with engine: %s", engine)
        terrain.compute_hillshade(dem_path, hillshade_path)

        # 4. Metrics over the area of interest.
        dem_raster = load_raster(dem_path, aoi=aoi)
        slope_raster = load_raster(slope_path, aoi=aoi)
        metrics = elevation.elevation_metrics(dem_raster)
        metrics += slope.slope_metrics(slope_raster)
        metrics.append(slope.slope_classification(slope_raster))

        # 5. Reports.
        json_path = str(out_dir / "terrain_assessment.json")
        html_path = str(out_dir / "terrain_assessment.html")
        outputs = {
            "dem_geotiff": dem_path,
            "slope_geotiff": slope_path,
            "hillshade_geotiff": hillshade_path,
            "json_report": json_path,
            "html_report": html_path,
        }
        report = json_report.build_report(
            lat=args.lat,
            lon=args.lon,
            radius_km=args.radius if aoi is not None else None,
            dem=provenance,
            validation=dem_check,
            slope_engine=engine,
            metrics=metrics,
            warnings=warnings,
            outputs=outputs,
        )
        json_report.write_report(report, json_path)
        hillshade_raster = load_raster(hillshade_path)
        html_report.write_html_report(report, dem_raster, slope_raster, hillshade_raster, html_path)

    except (validation.ValidationError, acquisition.AcquisitionError, terrain.TerrainProcessingError) as exc:
        log.error("%s", exc)
        return 1
    except ValueError as exc:
        log.error("%s", exc)
        return 1

    for warning in warnings:
        log.warning("%s", warning)
    print("\nTerrain assessment complete.")
    for metric in metrics:
        if metric.name != "slope_classification":
            print(f"  {metric.name:<18} {metric.value:>10,.1f} {metric.unit}  [{metric.kind}]")
    print(f"\nReports written to {out_dir}/:")
    for key, value in outputs.items():
        print(f"  {key:<18} {value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
