"""Assemble a QGIS project (``.qgs``) for the watershed delineation outputs.

The project is written as plain XML, without any QGIS or PyQGIS dependency, so
the same command that produces the GeoTIFFs can produce a map that opens in
QGIS — on a machine that has no QGIS installed at all. Nothing in it
recomputes or rewrites the analysis: the layers point at the files the
delineation already wrote, in their own CRS, and only the *display* of those
files is defined here.

What the project sets up, bottom to top:

1. ``Hillshade`` — relief shading of the DEM, under everything else.
2. ``Elevation (DEM, m)`` — the DEM on a terrain ramp stretched to the
   2-98% cut, drawn at partial opacity so the hillshade reads through it.
3. ``Flow accumulation`` — log-spaced blue ramp, transparent at the bottom so
   hillslope cells do not veil the terrain and the drainage network stands out.
4. ``Catchment boundary`` — the GeoJSON polygon, unfilled, with a haloed divide.
5. ``Outlets`` — the requested and snapped outlet points, distinct in both
   shape and colour, labelled.

The project CRS is the DEM's (a local UTM zone in metres), the canvas opens on
the DEM extent, every raster declares its nodata value as transparent, and
exact band statistics are refreshed in the GDAL sidecars first so that QGIS
stretches against the real numbers (see :mod:`.stats`).

Layer sources are written relative to the project file, so the whole output
directory can be copied or zipped and still open.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import rasterio

from ..hydrology import terrain
from . import server as server_module
from . import stats as stats_module
from . import styles
from .xmlwriter import el as _el
from .xmlwriter import num as _num
from .xmlwriter import write_document

log = logging.getLogger(__name__)

# Declared project version: new enough that QGIS applies no legacy project
# transforms, old enough that no supported release calls it "from the future".
QGIS_PROJECT_VERSION = "3.16.0"

PROJECT_FILENAME = "watershed.qgs"
DEM_OPACITY = 0.75  # lets the hillshade below show through the terrain ramp
CANVAS_MARGIN = 0.02  # fraction of the DEM extent left around the map

LAYER_ELEVATION = "Elevation (DEM, m)"
LAYER_HILLSHADE = "Hillshade"
LAYER_ACCUMULATION = "Flow accumulation (log scale, upstream cells)"
LAYER_CATCHMENT = "Catchment boundary"
LAYER_OUTLETS = "Outlets (requested / snapped)"


class QgisExportError(Exception):
    """Raised when the QGIS project cannot be built from a results directory."""


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def build_watershed_project(
    results_dir: str | Path,
    project_path: str | Path | None = None,
    write_styles: bool = True,
    refresh_statistics: bool = True,
) -> dict[str, str]:
    """Write a QGIS project for the delineation outputs in ``results_dir``.

    ``results_dir`` must contain the files written by
    ``python -m microwatershed.delineate`` (``dem.tif``,
    ``flow_accumulation.tif``, ``catchment.geojson``). A hillshade is derived
    from the DEM if the directory does not already have one; it is a
    visualization product only and is used by no metric.

    Returns the mapping of output names to paths.
    """
    results_dir = Path(results_dir)
    dem_path = _require(results_dir / "dem.tif")
    accum_path = _require(results_dir / "flow_accumulation.tif")
    geojson_path = _require(results_dir / "catchment.geojson")
    project_path = Path(project_path) if project_path else results_dir / PROJECT_FILENAME

    hillshade_path = results_dir / "hillshade.tif"
    if not hillshade_path.exists():
        log.info("Deriving %s for the QGIS project.", hillshade_path.name)
        terrain.compute_hillshade(str(dem_path), str(hillshade_path))

    with rasterio.open(dem_path) as src:
        project_crs = src.crs
        dem_bounds = tuple(src.bounds)
        dem_resolution = abs(src.transform.a)
    if project_crs is None:
        raise QgisExportError(f"{dem_path} has no CRS; a QGIS project cannot be georeferenced.")

    catchment = _read_catchment(geojson_path)

    band_stats: dict[str, stats_module.BandStats] = {}
    outputs: dict[str, str] = {"hillshade_geotiff": str(hillshade_path)}
    for key, path in (("dem", dem_path), ("flow_accumulation", accum_path),
                      ("hillshade", hillshade_path)):
        band_stats[key] = stats_module.band_statistics(path)
        if refresh_statistics:
            outputs[f"{key}_statistics"] = stats_module.write_pam_statistics(path, band_stats[key])

    layers = [
        _outlets_layer(geojson_path, project_path, catchment),
        _catchment_layer(geojson_path, project_path, catchment),
        _accumulation_layer(accum_path, project_path, band_stats["flow_accumulation"]),
        _elevation_layer(dem_path, project_path, band_stats["dem"]),
        _hillshade_layer(hillshade_path, project_path, band_stats["hillshade"]),
    ]

    root = _project_element(
        layers,
        project_crs=project_crs,
        extent=_padded(dem_bounds),
        title=_project_title(catchment),
        abstract=_project_abstract(catchment, band_stats, dem_resolution),
    )
    write_document(root, project_path)
    outputs["qgis_project"] = str(project_path)

    if write_styles:
        outputs.update(_write_style_files(layers, results_dir))
    return outputs


# ---------------------------------------------------------------------------
# Layer descriptions.
# ---------------------------------------------------------------------------


class _Layer:
    """One map layer: what QGIS needs to load it, and how to draw it."""

    def __init__(self, name: str, source: str, provider: str, layer_type: str,
                 crs: rasterio.crs.CRS, extent: tuple[float, float, float, float],
                 abstract: str, style_filename: str | None,
                 geometry: str | None = None, ows_name: str | None = None) -> None:
        self.name = name
        self.source = source
        self.provider = provider
        self.layer_type = layer_type  # "raster" | "vector"
        self.crs = crs
        self.extent = extent
        self.abstract = abstract
        self.style_filename = style_filename
        self.geometry = geometry
        self.id = _layer_id(name, source)
        # The name this layer is published under by QGIS Server. Stable across
        # runs even when the display name is not (see :mod:`.server`).
        self.short_name = ows_name or server_module.short_name(name)

    def write_style(self, parent: ET.Element) -> None:
        """Append the renderer (and labelling) elements to ``parent``."""
        raise NotImplementedError


class _RasterLayer(_Layer):
    def __init__(self, *, pipe_builder, **kwargs: Any) -> None:
        super().__init__(layer_type="raster", **kwargs)
        self._pipe_builder = pipe_builder

    def write_style(self, parent: ET.Element) -> None:
        styles.nodata_element(parent)
        self._pipe_builder(parent)
        _el(parent, "blendMode", text="0")


class _VectorLayer(_Layer):
    def __init__(self, *, renderer_builder, labeling_builder=None, **kwargs: Any) -> None:
        super().__init__(layer_type="vector", **kwargs)
        self._renderer_builder = renderer_builder
        self._labeling_builder = labeling_builder

    @property
    def labels_enabled(self) -> bool:
        return self._labeling_builder is not None

    def write_style(self, parent: ET.Element) -> None:
        self._renderer_builder(parent)
        if self._labeling_builder is not None:
            self._labeling_builder(parent)
        _el(parent, "blendMode", text="0")
        _el(parent, "featureBlendMode", text="0")
        _el(parent, "layerOpacity", text="1")


def _elevation_layer(path: Path, project_path: Path, stats: stats_module.BandStats) -> _Layer:
    vmin, vmax = stats.stretch_range()
    stops = styles.elevation_stops(vmin, vmax)
    with rasterio.open(path) as src:
        crs, extent = src.crs, tuple(src.bounds)
    return _RasterLayer(
        name=LAYER_ELEVATION,
        ows_name=server_module.SHORT_NAME_ELEVATION,
        source=_relative(path, project_path),
        provider="gdal",
        crs=crs,
        extent=extent,
        style_filename=f"{path.stem}.qml",
        abstract=(
            "Source DEM, unmodified. Terrain colour ramp "
            + styles.describe_stretch(stats, vmin, vmax)
            + f"; drawn at {DEM_OPACITY:.0%} opacity over the hillshade. "
            f"{stats.valid_percent:.2f}% of cells carry data; nodata is transparent."
        ),
        pipe_builder=lambda parent: styles.pseudocolor_pipe(
            parent, stops, vmin, vmax, opacity=DEM_OPACITY, cumulative_cut=True
        ),
    )


def _hillshade_layer(path: Path, project_path: Path, stats: stats_module.BandStats) -> _Layer:
    vmin, vmax = stats.stretch_range()
    with rasterio.open(path) as src:
        crs, extent = src.crs, tuple(src.bounds)
    return _RasterLayer(
        name=LAYER_HILLSHADE,
        ows_name=server_module.SHORT_NAME_HILLSHADE,
        source=_relative(path, project_path),
        provider="gdal",
        crs=crs,
        extent=extent,
        style_filename=f"{path.stem}.qml",
        abstract=(
            "Relief shading of the DEM (light from 315 degrees, 45 degrees above "
            "the horizon), for visualization only - no metric uses it. Grey ramp "
            + styles.describe_stretch(stats, vmin, vmax)
            + "."
        ),
        pipe_builder=lambda parent: styles.grayscale_pipe(parent, vmin, vmax),
    )


def _accumulation_layer(path: Path, project_path: Path, stats: stats_module.BandStats) -> _Layer:
    vmin, vmax = stats.minimum, stats.maximum
    stops = styles.accumulation_stops(vmin, vmax)
    with rasterio.open(path) as src:
        crs, extent = src.crs, tuple(src.bounds)
    return _RasterLayer(
        name=LAYER_ACCUMULATION,
        ows_name=server_module.SHORT_NAME_ACCUMULATION,
        source=_relative(path, project_path),
        provider="gdal",
        crs=crs,
        extent=extent,
        style_filename=f"{path.stem}.qml",
        abstract=(
            "D8 flow accumulation in upstream cells, from "
            f"{stats.minimum:,.0f} to {stats.maximum:,.0f}. Log-spaced colour "
            "breaks; cells with little upstream area fade to transparent so the "
            "drainage network reads against the terrain. Nodata is transparent."
        ),
        pipe_builder=lambda parent: styles.pseudocolor_pipe(
            parent, stops, vmin, vmax, cumulative_cut=False
        ),
    )


def _catchment_layer(path: Path, project_path: Path, catchment: dict[str, Any]) -> _Layer:
    area_ha = catchment["area_ha"]
    name = LAYER_CATCHMENT if area_ha is None else f"{LAYER_CATCHMENT} ({area_ha:,.1f} ha)"
    return _VectorLayer(
        name=name,
        ows_name=server_module.SHORT_NAME_CATCHMENT,
        source=_vector_source(path, project_path, "Polygon"),
        provider="ogr",
        crs=rasterio.crs.CRS.from_epsg(4326),
        extent=catchment["polygon_extent"],
        geometry="Polygon",
        style_filename="catchment_boundary.qml",
        abstract=(
            "Delineated catchment draining through the snapped outlet"
            + (f", {area_ha:,.1f} ha" if area_ha is not None else "")
            + (f" ({catchment['cell_count']:,} cells)" if catchment["cell_count"] else "")
            + ". Drawn unfilled so the terrain and drainage inside it stay visible."
        ),
        renderer_builder=styles.catchment_renderer,
    )


def _outlets_layer(path: Path, project_path: Path, catchment: dict[str, Any]) -> _Layer:
    return _VectorLayer(
        name=LAYER_OUTLETS,
        ows_name=server_module.SHORT_NAME_OUTLETS,
        source=_vector_source(path, project_path, "Point"),
        provider="ogr",
        crs=rasterio.crs.CRS.from_epsg(4326),
        extent=catchment["point_extent"],
        geometry="Point",
        style_filename="outlets.qml",
        abstract=(
            "The outlet as requested (black cross) and after snapping to the "
            "drainage network (orange dot)"
            + (
                f", {catchment['distance_moved_m']:,.0f} m apart"
                if catchment["distance_moved_m"] is not None
                else ""
            )
            + "."
        ),
        renderer_builder=styles.outlet_renderer,
        labeling_builder=styles.outlet_labeling,
    )


# ---------------------------------------------------------------------------
# Project XML.
# ---------------------------------------------------------------------------


def _project_element(
    layers: list[_Layer],
    project_crs: rasterio.crs.CRS,
    extent: tuple[float, float, float, float],
    title: str,
    abstract: str,
) -> ET.Element:
    """Build the whole ``<qgis>`` document. ``layers`` are ordered top first."""
    root = ET.Element("qgis", {"projectname": title, "version": QGIS_PROJECT_VERSION})
    _el(root, "homePath", path="")
    _el(root, "title", text=title)
    _crs_element(_el(root, "projectCrs"), project_crs)

    tree = _el(root, "layer-tree-group")
    _el(tree, "customproperties")
    for layer in layers:
        node = _el(tree, "layer-tree-layer", id=layer.id, name=layer.name,
                   source=layer.source, providerKey=layer.provider,
                   checked="Qt::Checked", expanded="1")
        _el(node, "customproperties")
    custom_order = _el(tree, "custom-order", enabled="0")
    for layer in reversed(layers):
        _el(custom_order, "item", text=layer.id)

    _el(root, "relations")

    # Legacy canvas state: this is what the QGIS desktop restores on open.
    canvas = _el(root, "mapcanvas", name="theMapCanvas", annotationsVisible="1")
    _el(canvas, "units", text="meters")
    _extent_element(canvas, "extent", extent)
    _el(canvas, "rotation", text="0")
    _crs_element(_el(canvas, "destinationsrs"), project_crs)
    _el(canvas, "rendermaptile", text="0")

    project_layers = _el(root, "projectlayers")
    for layer in layers:
        _maplayer_element(project_layers, layer)

    # Drawing order, bottom-most layer first.
    layer_order = _el(root, "layerorder")
    for layer in reversed(layers):
        _el(layer_order, "layer", id=layer.id)

    properties = _el(root, "properties")
    _el(_el(properties, "Paths"), "Absolute", text="false", type="bool")
    measurement = _el(properties, "Measurement")
    _el(measurement, "AreaUnits", text="m2", type="QString")
    _el(measurement, "DistanceUnits", text="meters", type="QString")
    _el(_el(properties, "SpatialRefSys"), "ProjectionsEnabled", text="1", type="int")

    # What QGIS Server is allowed to publish, and under which names. Harmless
    # for the desktop, required for the web client: see :mod:`.server`.
    epsg = project_crs.to_epsg()
    server_module.service_properties(
        properties,
        title=title,
        abstract=abstract,
        project_crs_authid=f"EPSG:{epsg}" if epsg else None,
        wfs_layer_ids=[layer.id for layer in layers if layer.layer_type == "vector"],
    )

    view_settings = _el(root, "ProjectViewSettings", rotation="0", UseProjectScales="0")
    default_extent = _el(view_settings, "DefaultViewExtent",
                         xmin=_num(extent[0]), ymin=_num(extent[1]),
                         xmax=_num(extent[2]), ymax=_num(extent[3]))
    _crs_element(default_extent, project_crs)

    metadata = _el(root, "projectMetadata")
    _el(metadata, "identifier", text="microwatershed-dss watershed delineation")
    _el(metadata, "title", text=title)
    _el(metadata, "abstract", text=abstract)
    _el(metadata, "type", text="dataset")
    _el(metadata, "language", text="EN")
    dates = _el(metadata, "dates")
    _el(dates, "date", type="Created",
        value=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    _crs_element(_el(metadata, "crs"), project_crs)

    _el(root, "Annotations")
    _el(root, "Layouts")
    return root


def _maplayer_element(parent: ET.Element, layer: _Layer) -> ET.Element:
    attrs = {
        "type": layer.layer_type,
        "styleCategories": "AllStyleCategories",
        "hasScaleBasedVisibilityFlag": "0",
        "minScale": "1e+08",
        "maxScale": "0",
        "autoRefreshTime": "0",
        "refreshOnNotifyEnabled": "0",
    }
    if layer.layer_type == "vector":
        attrs.update({
            "geometry": layer.geometry or "Unknown",
            "wkbType": layer.geometry or "Unknown",
            "simplifyDrawingHints": "0",
            "simplifyDrawingTol": "1",
            "simplifyLocal": "1",
            "simplifyMaxScale": "1",
            "simplifyAlgorithm": "0",
            "readOnly": "0",
            "symbologyReferenceScale": "-1",
            "labelsEnabled": "1" if getattr(layer, "labels_enabled", False) else "0",
        })
    element = _el(parent, "maplayer", **attrs)
    _extent_element(element, "extent", layer.extent)
    _el(element, "id", text=layer.id)
    _el(element, "datasource", text=layer.source)
    _el(element, "layername", text=layer.name)
    _crs_element(_el(element, "srs"), layer.crs)
    resource_metadata = _el(element, "resourceMetadata")
    _el(resource_metadata, "identifier")
    _el(resource_metadata, "type", text="dataset")
    _el(resource_metadata, "title", text=layer.name)
    _el(resource_metadata, "abstract", text=layer.abstract)

    # OWS identity: the short name is what web requests use, the title and
    # abstract are the same text the desktop legend shows.
    server_module.layer_server_elements(
        element, layer_name=layer.name, abstract=layer.abstract,
        ows_name=layer.short_name,
    )

    _el(element, "provider", text=layer.provider)
    layer.write_style(element)
    return element


def _write_style_files(layers: list[_Layer], results_dir: Path) -> dict[str, str]:
    """Write a ``.qml`` per layer next to the data.

    QGIS picks up ``<raster>.qml`` automatically when the GeoTIFF is loaded on
    its own, so the styling is not locked inside the project file. The two
    vector styles share one mixed-geometry GeoJSON and so cannot both be the
    file's default style; they are written under their own names, to be loaded
    from Layer Properties > Style > Load Style.
    """
    written = {}
    for layer in layers:
        if not layer.style_filename:
            continue
        root = ET.Element("qgis", {
            "version": QGIS_PROJECT_VERSION,
            "styleCategories": "AllStyleCategories",
            "hasScaleBasedVisibilityFlag": "0",
            "minScale": "1e+08",
            "maxScale": "0",
        })
        layer.write_style(root)
        path = results_dir / layer.style_filename
        write_document(root, path)
        written[f"qgis_style_{Path(layer.style_filename).stem}"] = str(path)
    return written


# ---------------------------------------------------------------------------
# CRS, extents, paths.
# ---------------------------------------------------------------------------


def _crs_element(parent: ET.Element, crs: rasterio.crs.CRS) -> ET.Element:
    """Write a ``<spatialrefsys>`` QGIS can resolve without its own database.

    The authority code identifies the CRS for QGIS; the WKT and proj.4 strings
    keep the project usable for CRSs that have no EPSG code (a custom DEM, for
    instance), and make it readable without a lookup.
    """
    element = _el(parent, "spatialrefsys", nativeFormat="Wkt")
    wkt = crs.to_wkt()
    _el(element, "wkt", text=wkt)
    _el(element, "proj4", text=_proj4(crs))
    epsg = crs.to_epsg()
    _el(element, "srid", text=str(epsg) if epsg else "0")
    _el(element, "authid", text=f"EPSG:{epsg}" if epsg else "")
    _el(element, "description", text=_crs_description(wkt))
    _el(element, "projectionacronym", text=_proj_acronym(crs))
    _el(element, "ellipsoidacronym", text="")
    _el(element, "geographicflag", text="true" if crs.is_geographic else "false")
    return element


def _proj4(crs: rasterio.crs.CRS) -> str:
    """The CRS as a proj.4 string, or empty if it cannot be expressed as one.

    rasterio emits the flagless parameters as ``+no_defs=True``; PROJ tolerates
    that, but the plain flag is what every other tool writes.
    """
    try:
        text = crs.to_proj4() or ""
    except Exception:
        return ""
    return text.replace("+no_defs=True", "+no_defs").strip()


def _crs_description(wkt: str) -> str:
    match = re.match(r'\s*\w+\["([^"]+)"', wkt)
    return match.group(1) if match else ""


def _proj_acronym(crs: rasterio.crs.CRS) -> str:
    match = re.search(r"\+proj=(\w+)", _proj4(crs))
    return match.group(1) if match else ""


def _extent_element(parent: ET.Element, tag: str,
                    extent: tuple[float, float, float, float]) -> ET.Element:
    element = _el(parent, tag)
    for name, value in zip(("xmin", "ymin", "xmax", "ymax"), extent):
        _el(element, name, text=_num(value))
    return element


def _padded(bounds: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """The DEM bounds with a small margin, so nothing sits on the canvas edge."""
    left, bottom, right, top = bounds
    pad_x = (right - left) * CANVAS_MARGIN
    pad_y = (top - bottom) * CANVAS_MARGIN
    return (left - pad_x, bottom - pad_y, right + pad_x, top + pad_y)


def _relative(path: Path, project_path: Path) -> str:
    """Path of ``path`` relative to the project file, in QGIS's ``./`` form."""
    try:
        relative = path.resolve().relative_to(project_path.resolve().parent)
    except ValueError:
        return str(path.resolve())
    return "./" + relative.as_posix()


def _vector_source(path: Path, project_path: Path, geometry_type: str) -> str:
    """OGR URI selecting one geometry type out of the mixed GeoJSON.

    ``catchment.geojson`` holds the catchment polygon and both outlet points in
    a single collection. Rather than duplicating that file, each layer reads it
    through the OGR provider's geometry-type filter, so the canonical output
    stays the single file the delineation wrote.
    """
    return f"{_relative(path, project_path)}|layername={path.stem}|geometrytype={geometry_type}"


def _layer_id(name: str, source: str) -> str:
    """A stable, unique layer id: regenerating the project yields the same ids."""
    slug = re.sub(r"\W+", "_", name).strip("_")
    digest = hashlib.sha1(f"{name}|{source}".encode("utf-8")).hexdigest()[:12]
    return f"{slug}_{digest}"


# ---------------------------------------------------------------------------
# Reading what the delineation wrote.
# ---------------------------------------------------------------------------


def _require(path: Path) -> Path:
    if not path.exists():
        raise QgisExportError(
            f"{path} is missing; run `python -m microwatershed.delineate` first "
            "(or point --results-dir at a completed run)."
        )
    return path


def _read_catchment(geojson_path: Path) -> dict[str, Any]:
    """Pull the facts the map needs out of the catchment GeoJSON."""
    try:
        with open(geojson_path, encoding="utf-8") as handle:
            collection = json.load(handle)
        features = collection["features"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise QgisExportError(f"{geojson_path} is not a readable GeoJSON FeatureCollection: {exc}")

    polygons = [f for f in features if _geometry_type(f).endswith("Polygon")]
    points = [f for f in features if _geometry_type(f) == "Point"]
    if not polygons:
        raise QgisExportError(f"{geojson_path} contains no catchment polygon.")

    properties = polygons[0].get("properties") or {}
    snapped = next((f.get("properties") or {} for f in points
                    if (f.get("properties") or {}).get("name") == "outlet_snapped"), {})
    return {
        "area_ha": _as_float(properties.get("area_ha")),
        "cell_count": properties.get("cell_count"),
        "distance_moved_m": _as_float(snapped.get("distance_moved_m")),
        "polygon_extent": _coords_extent(polygons),
        "point_extent": _coords_extent(points) if points else _coords_extent(polygons),
    }


def _geometry_type(feature: dict[str, Any]) -> str:
    return ((feature or {}).get("geometry") or {}).get("type", "")


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coords_extent(features: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []

    def walk(coordinates: Any) -> None:
        if isinstance(coordinates, (list, tuple)):
            if coordinates and isinstance(coordinates[0], (int, float)):
                xs.append(float(coordinates[0]))
                ys.append(float(coordinates[1]))
            else:
                for item in coordinates:
                    walk(item)

    for feature in features:
        walk(((feature or {}).get("geometry") or {}).get("coordinates"))
    if not xs:
        raise QgisExportError("A GeoJSON feature has no coordinates to take an extent from.")
    return (min(xs), min(ys), max(xs), max(ys))


def _project_title(catchment: dict[str, Any]) -> str:
    area_ha = catchment["area_ha"]
    if area_ha is None:
        return "Microwatershed delineation"
    return f"Microwatershed delineation - {area_ha:,.1f} ha catchment"


def _project_abstract(
    catchment: dict[str, Any],
    band_stats: dict[str, stats_module.BandStats],
    resolution_m: float,
) -> str:
    dem = band_stats["dem"]
    accumulation = band_stats["flow_accumulation"]
    return (
        "Watershed delineation outputs from microwatershed-dss, styled for "
        f"inspection. DEM cell size {resolution_m:.1f} m, elevation "
        f"{dem.minimum:,.0f}-{dem.maximum:,.0f} m, flow accumulation up to "
        f"{accumulation.maximum:,.0f} upstream cells. The layers display the "
        "delineation outputs unchanged; only their symbology is defined by "
        "this project."
    )
