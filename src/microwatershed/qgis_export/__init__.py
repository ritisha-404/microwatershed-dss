"""QGIS visualization layer for the watershed delineation outputs.

Builds a ready-to-open QGIS project (and matching ``.qml`` styles) from the
files a delineation run already produced. Display only: no hydrological value
is recomputed here, and no analysis output is modified.
"""

from .project import PROJECT_FILENAME, QgisExportError, build_watershed_project

__all__ = ["build_watershed_project", "QgisExportError", "PROJECT_FILENAME"]
