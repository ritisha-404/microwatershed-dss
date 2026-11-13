"""QGIS Server (OWS) publication settings for the watershed project.

A project that opens perfectly in QGIS Desktop is not automatically servable:
QGIS Server reads a separate set of project properties to decide *what* it is
allowed to publish and *how*. Auditing the desktop project with PyQGIS showed
every one of them unset — ``WMSServiceCapabilities`` false, ``WMSCrsList`` and
``WFSLayers`` empty — which is enough for QGIS Server to answer requests, but
not enough for a web client to be useful:

* without ``WMSServiceCapabilities`` the GetCapabilities document carries no
  title or abstract, and Lizmap refuses to list the project;
* without ``WFSLayers`` no vector layer is published as features, so attribute
  tables and feature selection in the browser have nothing to read;
* without ``WMSAddWktGeometry`` a GetFeatureInfo response has no geometry, so
  a clicked feature cannot be highlighted on the map;
* without ``WMSCrsList`` the service does not advertise the Web Mercator
  (EPSG:3857) that every browser map actually requests.

This module adds those properties, and a WMS/WFS short name per layer. Short
names matter because the layer titles are written for humans - ``Outlets
(requested / snapped)``, ``Flow accumulation (log scale, upstream cells)`` -
and those spaces, parentheses, commas and slashes would otherwise become the
layer identifiers in every OWS request URL. The readable title stays the
layer's title; the short name is what the protocol uses.

Nothing here changes the map's appearance or the analysis outputs. It only
declares the publication contract, so the same project file serves QGIS
Desktop and QGIS Server from one source of truth.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .xmlwriter import el as _el

# CRSs advertised in GetCapabilities. The project's own CRS is added on top of
# these at build time.
#   3857 - what the Lizmap/OpenLayers web map requests tiles in;
#   4326 - what the API and the GeoJSON outputs speak;
#   900913 - the legacy Web Mercator code some clients still send.
BASE_WMS_CRS = ("EPSG:3857", "EPSG:4326", "EPSG:900913")

# Decimal places in GetFeatureInfo coordinates. 8 is ~1 mm at the equator:
# far finer than the ~30 m DEM, so it never limits the reported geometry.
WMS_PRECISION = 8

# Maximum pixels QGIS Server will render for one GetMap, guarding against a
# client asking for an enormous image of a local project.
WMS_MAX_WIDTH = 5000
WMS_MAX_HEIGHT = 5000

ONLINE_RESOURCE_PLACEHOLDER = ""


# The published OWS name of every layer, fixed for the lifetime of the
# service. Display names are written for people and some of them carry run
# specific numbers ("Catchment boundary (17.0 ha)"), but a web client stores
# layer names in its configuration and its URLs - so the name a request uses
# must not change when a new delineation produces a different area. These are
# the identifiers the Lizmap configuration and the browser both refer to.
SHORT_NAME_CATCHMENT = "catchment_boundary"
SHORT_NAME_OUTLETS = "outlets"
SHORT_NAME_ACCUMULATION = "flow_accumulation"
SHORT_NAME_ELEVATION = "elevation"
SHORT_NAME_HILLSHADE = "hillshade"


def short_name(layer_name: str) -> str:
    """Fall back to an OWS-safe short name derived from a display name.

    QGIS Server requires a short name to match ``[A-Za-z][A-Za-z0-9._-]*``:
    it becomes an XML element name in GetCapabilities and a value in request
    URLs. Layers declare a stable name of their own (the constants above);
    this is only for a layer that did not.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", layer_name).strip("_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"layer_{cleaned}" if cleaned else "layer"
    return cleaned


def service_properties(
    parent: ET.Element,
    *,
    title: str,
    abstract: str,
    project_crs_authid: str | None,
    wfs_layer_ids: list[str],
    keywords: tuple[str, ...] = ("watershed", "catchment", "hydrology", "DEM", "terrain"),
) -> None:
    """Append the OWS service declaration to a project's ``<properties>``.

    ``wfs_layer_ids`` are the ids of the layers to publish as features; the
    rasters are deliberately left out of WFS (they are published as WMS images
    and have no attributes to serve).
    """
    _el(parent, "WMSServiceCapabilities", text="true", type="bool")
    _el(parent, "WMSServiceTitle", text=title, type="QString")
    _el(parent, "WMSServiceAbstract", text=abstract, type="QString")
    _el(parent, "WMSOnlineResource", text=ONLINE_RESOURCE_PLACEHOLDER, type="QString")

    keyword_list = _el(parent, "WMSKeywordList", type="QStringList")
    for keyword in keywords:
        _el(keyword_list, "value", text=keyword)

    # The name of the root layer in GetCapabilities. Without it QGIS Server
    # uses the project title, which contains spaces.
    _el(parent, "WMSRootName", text="microwatershed", type="QString")

    # Advertised CRSs: the browser map's Web Mercator, WGS84, and the DEM's
    # own UTM zone so a client can request unreprojected pixels.
    crs_list = _el(parent, "WMSCrsList", type="QStringList")
    advertised = list(BASE_WMS_CRS)
    if project_crs_authid and project_crs_authid not in advertised:
        advertised.append(project_crs_authid)
    for authid in advertised:
        _el(crs_list, "value", text=authid)

    # GetFeatureInfo: return the feature's geometry as WKT so the client can
    # highlight what was clicked, and report coordinates at full precision.
    _el(parent, "WMSAddWktGeometry", text="true", type="bool")
    _el(parent, "WMSPrecision", text=str(WMS_PRECISION), type="QString")

    # Render limits and segmentation of curved geometries.
    _el(parent, "WMSMaxWidth", text=str(WMS_MAX_WIDTH), type="int")
    _el(parent, "WMSMaxHeight", text=str(WMS_MAX_HEIGHT), type="int")
    _el(parent, "WMSImageQuality", text="90", type="int")

    # Nothing is withheld: every layer in the project is meant to be seen.
    _el(parent, "WMSRestrictedLayers", type="QStringList")

    # Publish layer *names* rather than ids in OWS requests. The short names
    # written per layer are the stable identifiers; ids embed a hash.
    _el(parent, "WMSUseLayerIDs", text="false", type="bool")

    # WFS: the vector layers only, read-only. Precision 8 decimal places
    # matches WMSPrecision so the two services agree on coordinates.
    wfs = _el(parent, "WFSLayers", type="QStringList")
    for layer_id in wfs_layer_ids:
        _el(wfs, "value", text=layer_id)
    precision = _el(parent, "WFSLayersPrecision")
    for layer_id in wfs_layer_ids:
        _el(precision, layer_id, text=str(WMS_PRECISION), type="int")

    # Explicitly deny every WFS-T capability: the browser may read the
    # catchment, never edit it. The analysis engine owns these files. All three
    # operations are children of a single element - QGIS reads the first
    # <WFSTLayers> it finds and ignores any repeat.
    transactional = _el(parent, "WFSTLayers")
    for operation in ("Insert", "Update", "Delete"):
        _el(transactional, operation, type="QStringList")


def layer_server_elements(
    parent: ET.Element,
    *,
    layer_name: str,
    abstract: str,
    ows_name: str | None = None,
    keywords: tuple[str, ...] = (),
) -> str:
    """Append the per-layer OWS metadata to a ``<maplayer>``. Returns the short name.

    The ``<shortname>`` is the OWS identifier, ``<title>`` the human label and
    ``<abstract>`` the methodology note already written for the desktop
    project - so the browser's layer tooltip says the same thing as QGIS.
    """
    name = ows_name or short_name(layer_name)
    _el(parent, "shortname", text=name)
    _el(parent, "title", text=layer_name)
    _el(parent, "abstract", text=abstract)
    if keywords:
        keyword_list = _el(parent, "keywordList")
        for keyword in keywords:
            _el(keyword_list, "value", text=keyword)
    return name
