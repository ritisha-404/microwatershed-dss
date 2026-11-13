"""Cartography for the QGIS layers: colour ramps and symbol definitions.

Everything here decides how the Milestone 2 outputs are *drawn*. No value is
recomputed and no file is modified; the styles read the statistics measured by
:mod:`.stats` and turn them into QGIS renderer XML.

Three display decisions carry the readability of the map:

* **Elevation** is stretched between the 2nd and 98th percentile of the DEM
  rather than between its minimum and maximum. A handful of summit or sink
  cells otherwise consume most of the colour range and flatten the terrain
  that matters. Values outside the cut are clamped to the end colours, so the
  whole DEM is still coloured.
* **Flow accumulation** is spread over log-spaced breaks. Accumulation grows
  with upstream area, so a linear ramp paints every hillslope cell the same
  colour and shows only the trunk channel; on log breaks the whole drainage
  network appears. The first breaks are also nearly transparent, so hillslope
  cells (accumulation of a few cells, the overwhelming majority) fade out
  instead of veiling the terrain beneath them.
* **The catchment and the outlets** are drawn in the same visual language as
  the HTML report: a near-black boundary, an orange-red dot for the snapped
  outlet and a black cross for the point as requested, each with a white halo
  so they stay legible over both dark and light terrain.

Symbol properties use the ``<prop k= v=>`` form, which every QGIS 3 release
reads (newer releases write an equivalent ``<Option>`` map but still parse
this one), so the generated project is not tied to the QGIS version that
happens to be installed.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET

from .xmlwriter import el as _el
from .xmlwriter import num as _num
from .stats import CUT_LOWER_PERCENT, CUT_UPPER_PERCENT, BandStats

RGBA = tuple[int, int, int, int]

# Shared with the HTML report so both views of a run look like one product.
OUTLINE_INK = (11, 11, 11, 255)  # catchment divide, requested-outlet cross
SNAPPED_MARKER = (185, 74, 30, 255)  # orange 600: stands out on the blue ramps
HALO_WHITE = (255, 255, 255, 230)

# Land-elevation ramp: valley green through straw and brown to bare rock.
# Positions are fractions of the stretched range, not absolute elevations.
TERRAIN_RAMP: tuple[tuple[float, RGBA], ...] = (
    (0.000, (46, 107, 62, 255)),
    (0.125, (93, 154, 75, 255)),
    (0.250, (146, 185, 92, 255)),
    (0.375, (198, 207, 119, 255)),
    (0.500, (230, 215, 138, 255)),
    (0.625, (214, 178, 116, 255)),
    (0.750, (189, 144, 99, 255)),
    (0.875, (194, 167, 154, 255)),
    (1.000, (239, 239, 239, 255)),
)

# Drainage ramp, in log space: invisible at one cell, saturated blue at the
# trunk channel. The alpha ramp is what keeps hillslopes subdued.
ACCUMULATION_RAMP: tuple[tuple[float, RGBA], ...] = (
    (0.00, (219, 233, 246, 0)),
    (0.15, (198, 219, 239, 25)),
    (0.35, (158, 202, 225, 80)),
    (0.55, (107, 174, 214, 160)),
    (0.75, (49, 130, 189, 225)),
    (0.90, (8, 81, 156, 255)),
    (1.00, (8, 48, 107, 255)),
)

# Breaks offered for the accumulation legend, 1-2-5 per decade.
_LOG_STEPS = (1.0, 2.0, 5.0)


# ---------------------------------------------------------------------------
# Colour ramp sampling.
# ---------------------------------------------------------------------------


def sample_ramp(ramp: tuple[tuple[float, RGBA], ...], position: float) -> RGBA:
    """Linearly interpolate ``ramp`` (positions 0-1) at ``position``."""
    position = min(max(position, 0.0), 1.0)
    previous_stop, previous_colour = ramp[0]
    for stop, colour in ramp:
        if position <= stop:
            if stop == previous_stop:
                return colour
            t = (position - previous_stop) / (stop - previous_stop)
            return tuple(  # type: ignore[return-value]
                int(round(a + (b - a) * t)) for a, b in zip(previous_colour, colour)
            )
        previous_stop, previous_colour = stop, colour
    return ramp[-1][1]


def rgba_text(colour: RGBA) -> str:
    return ",".join(str(component) for component in colour)


def hex_text(colour: RGBA) -> str:
    return "#{:02x}{:02x}{:02x}".format(*colour[:3])


# ---------------------------------------------------------------------------
# Colour ramp breaks per layer.
# ---------------------------------------------------------------------------


def elevation_stops(vmin: float, vmax: float, count: int = 9) -> list[tuple[float, RGBA, str]]:
    """Evenly spaced elevation breaks across the stretched range."""
    span = vmax - vmin
    stops = []
    for index in range(count):
        fraction = index / (count - 1)
        value = vmin + span * fraction
        stops.append((value, sample_ramp(TERRAIN_RAMP, fraction), f"{value:,.0f} m"))
    return stops


def accumulation_stops(vmin: float, vmax: float) -> list[tuple[float, RGBA, str]]:
    """Log-spaced accumulation breaks from ``vmin`` to ``vmax``.

    Breaks follow the 1-2-5 sequence so the legend reads in round numbers, and
    both ends of the data range are always included so no cell falls outside
    the ramp.
    """
    vmin = max(vmin, 1.0)
    vmax = max(vmax, vmin * 10.0)
    values = [vmin]
    decade = 10.0 ** math.floor(math.log10(vmin))
    while decade <= vmax:
        for step in _LOG_STEPS:
            candidate = decade * step
            # Keep breaks clear of the ends so the legend has no near-duplicates.
            if vmin * 1.2 < candidate < vmax / 1.2:
                values.append(candidate)
        decade *= 10.0
    values.append(vmax)

    log_min, log_max = math.log10(vmin), math.log10(vmax)
    stops = []
    for value in values:
        fraction = (math.log10(value) - log_min) / (log_max - log_min)
        stops.append((value, sample_ramp(ACCUMULATION_RAMP, fraction), f"{value:,.0f}"))
    return stops


# ---------------------------------------------------------------------------
# Raster renderers.
# ---------------------------------------------------------------------------


def pseudocolor_pipe(
    parent: ET.Element,
    stops: list[tuple[float, RGBA, str]],
    vmin: float,
    vmax: float,
    opacity: float = 1.0,
    cumulative_cut: bool = True,
    label_precision: int = 0,
) -> ET.Element:
    """A single-band pseudocolour pipe with an interpolated ramp."""
    pipe = _pipe(parent)
    renderer = _el(
        pipe,
        "rasterrenderer",
        type="singlebandpseudocolor",
        band="1",
        alphaBand="-1",
        opacity=_num(opacity),
        classificationMin=_num(vmin),
        classificationMax=_num(vmax),
        nodataColor="",
    )
    _el(renderer, "rasterTransparency")
    _min_max_origin(renderer, cumulative_cut)
    shader = _el(renderer, "rastershader")
    ramp_shader = _el(
        shader,
        "colorrampshader",
        colorRampType="INTERPOLATED",
        classificationMode="1",
        clip="0",
        minimumValue=_num(vmin),
        maximumValue=_num(vmax),
        labelPrecision=str(label_precision),
    )
    for value, colour, label in stops:
        _el(
            ramp_shader,
            "item",
            value=_num(value),
            color=hex_text(colour),
            alpha=str(colour[3]),
            label=label,
        )
    # One legend entry per break rather than a continuous colour bar: the bar
    # is labelled only at its ends, which would hide the elevation steps and
    # imply that the log-spaced accumulation breaks are evenly spaced.
    _el(ramp_shader, "rampLegendSettings", useContinuousLegend="0", direction="0",
        orientation="2", prefix="", suffix="", minimumLabel="", maximumLabel="")
    _pipe_tail(pipe)
    return pipe


def grayscale_pipe(
    parent: ET.Element,
    vmin: float,
    vmax: float,
    opacity: float = 1.0,
) -> ET.Element:
    """A black-to-white pipe stretched between ``vmin`` and ``vmax``."""
    pipe = _pipe(parent)
    renderer = _el(
        pipe,
        "rasterrenderer",
        type="singlebandgray",
        band="1",
        grayBand="1",
        gradient="BlackToWhite",
        alphaBand="-1",
        opacity=_num(opacity),
        nodataColor="",
    )
    _el(renderer, "rasterTransparency")
    _min_max_origin(renderer, cumulative_cut=True)
    enhancement = _el(renderer, "contrastEnhancement")
    _el(enhancement, "minValue").text = _num(vmin)
    _el(enhancement, "maxValue").text = _num(vmax)
    _el(enhancement, "algorithm").text = "StretchToMinimumMaximum"
    _pipe_tail(pipe)
    return pipe


def nodata_element(parent: ET.Element, band: int = 1) -> ET.Element:
    """Make the band's own nodata value render transparent."""
    no_data = _el(parent, "noData")
    _el(no_data, "noDataList", bandNo=str(band), useSrcNoData="1")
    return no_data


# ---------------------------------------------------------------------------
# Vector renderers.
# ---------------------------------------------------------------------------


def catchment_renderer(parent: ET.Element) -> ET.Element:
    """Hollow catchment polygon with a haloed, near-black divide."""
    renderer = _el(
        parent,
        "renderer-v2",
        type="singleSymbol",
        symbollevels="0",
        forceraster="0",
        enableorderby="0",
        referencescale="-1",
    )
    symbols = _el(renderer, "symbols")
    symbol = _symbol(symbols, "0", "fill")
    # No brush at all: the terrain and the drainage network must stay visible
    # inside the catchment.
    _symbol_layer(symbol, "SimpleFill", {
        "color": "255,255,255,0",
        "style": "no",
        "outline_style": "no",
        "outline_width": "0",
        "joinstyle": "round",
    })
    _symbol_layer(symbol, "SimpleLine", {
        "line_color": rgba_text(HALO_WHITE),
        "line_style": "solid",
        "line_width": "1.6",
        "line_width_unit": "MM",
        "joinstyle": "round",
        "capstyle": "round",
    })
    _symbol_layer(symbol, "SimpleLine", {
        "line_color": rgba_text(OUTLINE_INK),
        "line_style": "solid",
        "line_width": "0.66",
        "line_width_unit": "MM",
        "joinstyle": "round",
        "capstyle": "round",
    })
    return renderer


def outlet_renderer(parent: ET.Element, attribute: str = "name") -> ET.Element:
    """Distinct symbols for the requested and the snapped outlet.

    Categorised on the feature's ``name`` property, which the delineation
    writes as ``outlet_requested`` / ``outlet_snapped``. The two outlets sit
    within a snap distance of each other, so they differ in shape as well as
    colour: a hollow cross marks the point as given, a filled dot the point
    actually used for the delineation.
    """
    renderer = _el(
        parent,
        "renderer-v2",
        type="categorizedSymbol",
        attr=attribute,
        symbollevels="0",
        forceraster="0",
        enableorderby="0",
        referencescale="-1",
    )
    categories = _el(renderer, "categories")
    _el(categories, "category", value="outlet_snapped", symbol="0", render="true",
        label="Snapped outlet (used for delineation)", type="string")
    _el(categories, "category", value="outlet_requested", symbol="1", render="true",
        label="Requested outlet (as given)", type="string")

    symbols = _el(renderer, "symbols")
    snapped = _symbol(symbols, "0", "marker")
    _symbol_layer(snapped, "SimpleMarker", {
        "name": "circle",
        "color": rgba_text(SNAPPED_MARKER),
        "outline_color": "255,255,255,255",
        "outline_style": "solid",
        "outline_width": "0.5",
        "outline_width_unit": "MM",
        "size": "3.4",
        "size_unit": "MM",
        "angle": "0",
        "offset": "0,0",
    })
    requested = _symbol(symbols, "1", "marker")
    # Halo first, then the cross on top, so the mark survives a dark hillside.
    _symbol_layer(requested, "SimpleMarker", {
        "name": "cross2",
        "color": rgba_text(HALO_WHITE),
        "outline_color": rgba_text(HALO_WHITE),
        "outline_style": "solid",
        "outline_width": "1.3",
        "outline_width_unit": "MM",
        "size": "3.6",
        "size_unit": "MM",
        "angle": "0",
        "offset": "0,0",
    })
    _symbol_layer(requested, "SimpleMarker", {
        "name": "cross2",
        "color": rgba_text(OUTLINE_INK),
        "outline_color": rgba_text(OUTLINE_INK),
        "outline_style": "solid",
        "outline_width": "0.55",
        "outline_width_unit": "MM",
        "size": "3.6",
        "size_unit": "MM",
        "angle": "0",
        "offset": "0,0",
    })
    _el(renderer, "rotation")
    _el(renderer, "sizescale")
    return renderer


OUTLET_LABEL_EXPRESSION = (
    "CASE WHEN \"name\" = 'outlet_snapped' THEN 'Snapped outlet' "
    "ELSE 'Requested outlet' END"
)

# The two outlets sit within the snap distance of each other, so one quadrant
# for both labels would make them collide and QGIS would drop one. The snapped
# label goes above-right (quadrant 2), the requested one below-left (6).
OUTLET_LABEL_QUADRANT = (
    "CASE WHEN \"name\" = 'outlet_snapped' THEN 2 ELSE 6 END"
)


def outlet_labeling(parent: ET.Element) -> ET.Element:
    """Buffered labels, placed on opposite sides of their markers."""
    labeling = _el(parent, "labeling", type="simple")
    settings = _el(labeling, "settings")
    text_style = _el(
        settings,
        "text-style",
        fieldName=OUTLET_LABEL_EXPRESSION,
        isExpression="1",
        fontFamily="Sans Serif",
        fontSize="9",
        fontSizeUnit="Point",
        fontWeight="75",
        textColor=rgba_text(OUTLINE_INK),
        textOpacity="1",
        blendMode="0",
        previewBkgrdColor="255,255,255,255",
    )
    _el(text_style, "text-buffer", bufferDraw="1", bufferSize="1", bufferSizeUnits="MM",
        bufferColor="255,255,255,255", bufferOpacity="1", bufferNoFill="1",
        bufferJoinStyle="128", bufferBlendMode="0")
    _el(text_style, "background", shapeDraw="0")
    _el(text_style, "shadow", shadowDraw="0")
    _el(settings, "text-format")
    # placement 1 = offset from point; the quadrant is set per feature below.
    _el(settings, "placement", placement="1", quadOffset="2", dist="3", distUnits="MM",
        priority="5", xOffset="0", yOffset="0", offsetType="0", rotationAngle="0",
        overlapHandling="AllowOverlapIfRequired")
    # displayAll is how releases before the overlapHandling setting expressed
    # "label every feature, colliding or not"; both are written so neither
    # outlet goes unlabelled whatever QGIS opens the project.
    _el(settings, "rendering", drawLabels="1", displayAll="1", scaleVisibility="0",
        obstacle="0", fontLimitPixelSize="0", minFeatureSize="0", labelPerPart="0",
        mergeLines="0", upsidedownLabels="0", maxNumLabels="2000", limitNumLabels="0")
    _quadrant_property(settings, OUTLET_LABEL_QUADRANT)
    return labeling


def _quadrant_property(settings: ET.Element, expression: str) -> ET.Element:
    """Data-defined label quadrant, so each outlet labels to its own side."""
    dd_properties = _el(settings, "dd_properties")
    collection = _el(dd_properties, "Option", type="Map")
    _el(collection, "Option", name="name", type="QString", value="")
    properties = _el(collection, "Option", name="properties", type="Map")
    quadrant = _el(properties, "Option", name="OffsetQuad", type="Map")
    _el(quadrant, "Option", name="active", type="bool", value="true")
    _el(quadrant, "Option", name="expression", type="QString", value=expression)
    _el(quadrant, "Option", name="type", type="int", value="3")
    _el(collection, "Option", name="type", type="QString", value="collection")
    return dd_properties


# ---------------------------------------------------------------------------
# XML plumbing.
# ---------------------------------------------------------------------------


def _pipe(parent: ET.Element) -> ET.Element:
    pipe = _el(parent, "pipe")
    provider = _el(pipe, "provider")
    _el(provider, "resampling", enabled="false", maxOversampling="2",
        zoomedInResamplingMethod="nearestNeighbour",
        zoomedOutResamplingMethod="nearestNeighbour")
    return pipe


def _pipe_tail(pipe: ET.Element) -> None:
    _el(pipe, "brightnesscontrast", brightness="0", contrast="0", gamma="1")
    _el(pipe, "huesaturation", saturation="0", grayscaleMode="0", invertColors="0",
        colorizeOn="0", colorizeRed="255", colorizeGreen="128", colorizeBlue="128",
        colorizeStrength="100")
    _el(pipe, "rasterresampler", maxOversampling="2")
    _el(pipe, "resamplingStage", text="resamplingFilter")


def _min_max_origin(renderer: ET.Element, cumulative_cut: bool) -> ET.Element:
    """Record how the display range was derived, as QGIS's own stretch would.

    QGIS shows this in the layer's Min/Max Value Settings, so the stretch can
    be reproduced or widened from the GUI instead of being an opaque pair of
    numbers.
    """
    origin = _el(renderer, "minMaxOrigin")
    _el(origin, "limits", text="CumulativeCut" if cumulative_cut else "MinMax")
    _el(origin, "extent", text="WholeRaster")
    _el(origin, "statAccuracy", text="Exact")
    _el(origin, "cumulativeCutLower", text=_num(CUT_LOWER_PERCENT / 100.0))
    _el(origin, "cumulativeCutUpper", text=_num(CUT_UPPER_PERCENT / 100.0))
    _el(origin, "stdDevFactor", text="2")
    return origin


def _symbol(parent: ET.Element, name: str, symbol_type: str) -> ET.Element:
    return _el(parent, "symbol", name=name, type=symbol_type, alpha="1",
               clip_to_extent="1", force_rhr="0")


def _symbol_layer(symbol: ET.Element, layer_class: str, props: dict[str, str]) -> ET.Element:
    layer = _el(symbol, "layer", **{"class": layer_class, "pass": "0", "enabled": "1",
                                    "locked": "0"})
    for key in sorted(props):
        _el(layer, "prop", k=key, v=props[key])
    return layer


def describe_stretch(stats: BandStats, vmin: float, vmax: float) -> str:
    """One-line human description of a layer's display range, for metadata."""
    if (vmin, vmax) == (stats.minimum, stats.maximum):
        return f"displayed over the full data range {vmin:,.2f} to {vmax:,.2f}"
    return (
        f"displayed over the {CUT_LOWER_PERCENT:g}-{CUT_UPPER_PERCENT:g}% cut "
        f"{vmin:,.2f} to {vmax:,.2f} (data range {stats.minimum:,.2f} to "
        f"{stats.maximum:,.2f})"
    )
