"""Command-line entry point for the QGIS project export.

Usage:
    python -m microwatershed.qgis_export
    python -m microwatershed.qgis_export --results-dir outputs/watershed
    python -m microwatershed.qgis_export --project maps/catchment.qgs

Rebuilds the QGIS project for an existing delineation run. ``delineate``
already writes the project, so this is for restyling a previous run (or for
results produced before the QGIS export existed) without redoing the
hydrology: it only reads the outputs and writes the project, the ``.qml``
styles and the GDAL statistics sidecars.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .project import PROJECT_FILENAME, QgisExportError, build_watershed_project

log = logging.getLogger("microwatershed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m microwatershed.qgis_export",
        description="Build a QGIS project from an existing watershed delineation run.",
    )
    parser.add_argument("--results-dir", default="outputs/watershed",
                        help="Directory holding the delineation outputs "
                        "(default: outputs/watershed).")
    parser.add_argument("--project", default=None,
                        help=f"Path of the project file to write (default: "
                        f"<results-dir>/{PROJECT_FILENAME}).")
    parser.add_argument("--no-styles", action="store_true",
                        help="Skip the per-layer .qml style sidecars.")
    parser.add_argument("--no-statistics", action="store_true",
                        help="Do not refresh the GDAL .aux.xml statistics sidecars.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    return parser


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        outputs = build_watershed_project(
            args.results_dir,
            project_path=args.project,
            write_styles=not args.no_styles,
            refresh_statistics=not args.no_statistics,
        )
    except (QgisExportError, ValueError) as exc:
        log.error("%s", exc)
        return 1
    except OSError as exc:
        log.error("Could not write the QGIS project: %s", exc)
        return 1

    print(f"\nQGIS project written: {outputs['qgis_project']}")
    print("Open it in QGIS; all layers are styled, ordered and visible.")
    for key, value in outputs.items():
        if key != "qgis_project":
            print(f"  {key:<26} {value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
