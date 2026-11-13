# QGIS visualisation

**Output:** QGIS project (`watershed.qgs`), per-layer styles (`*.qml`),
hillshade (`hillshade.tif`) and refreshed GDAL statistics sidecars
(`*.tif.aux.xml`). **Kind: display only** — no value in any report comes from
this step, and no analysis output is modified by it.

## What it represents

A map of the delineation that opens directly in QGIS: the DEM, the drainage
network, the catchment boundary and both outlet points, already styled,
ordered and zoomed to the data. It exists because the raw GeoTIFFs are not
readable as delivered — QGIS loads a DEM stretched grey over its full range,
flow accumulation as a near-black field with one bright line, and a
mixed-geometry GeoJSON as one undifferentiated layer.

The project is written as plain XML by `microwatershed.qgis_export`, so
producing it requires neither QGIS nor PyQGIS; only opening it does.

## Layers, top to bottom

| Layer | Source | Rendering |
|---|---|---|
| Outlets (requested / snapped) | `catchment.geojson`, points | Categorised on `name`: black cross for the requested point, orange-red dot for the snapped one, both haloed and labelled |
| Catchment boundary (*area* ha) | `catchment.geojson`, polygon | No fill; white halo line under a near-black divide |
| Flow accumulation (log scale) | `flow_accumulation.tif` | Log-spaced blue ramp, transparent at the low end |
| Elevation (DEM, m) | `dem.tif` | Terrain ramp on the 2–98 % cut, 75 % opacity |
| Hillshade | `hillshade.tif` | Grey ramp on the 2–98 % cut |

Both vector layers read the single `catchment.geojson` the delineation wrote,
through the OGR provider's `geometrytype=` filter, so the map and the data
cannot drift apart. All sources are stored relative to the project file.

## Display decisions

### Elevation: percentile stretch, not min–max

A DEM's extremes are usually a handful of cells. Stretching a colour ramp
between them spends most of the ramp on terrain that occupies almost no area
and flattens everything else into one or two colours. The elevation layer is
therefore stretched between the 2nd and 98th percentile of the valid cells,
with clipping off, so values outside the cut keep the end colours instead of
disappearing. The 9 ramp breaks run from valley green through straw and
brown to near-white.

The layer is drawn at 75 % opacity over the hillshade: colour carries the
elevation, shading carries the shape of the land.

### Flow accumulation: logarithmic contrast

Flow accumulation grows with upstream area, so its distribution is extremely
skewed — in a typical run the median cell has 2 upstream cells and the trunk
channel has thousands. On a linear ramp only the main channel is visible. The
breaks therefore follow a 1–2–5 sequence per decade between the raster's
actual minimum and maximum, which makes the ramp logarithmic in value, and
colour is interpolated over equal steps in log space.

Transparency carries the same information a second time: alpha runs from 0 at
one upstream cell to opaque at the top of the range, so hillslope cells (the
overwhelming majority) fade out and the drainage network reads against the
terrain beneath it.

### Catchment and outlets

The catchment polygon is drawn with no fill, so terrain and drainage inside
the catchment stay visible, and with a white halo under a near-black divide
so the boundary survives both dark and light backgrounds. The two outlets can
be only a snap distance apart, so they differ in shape as well as colour, and
their labels are placed in opposite quadrants.

The colours match the HTML report (`watershed_assessment.html`), so both views
of the same run read as one product.

## NoData and raster statistics

Every raster layer declares its source nodata value transparent
(`useSrcNoData`). This matters in practice: a DEM reprojected from tiles into
a UTM grid has nodata wedges in its corners, and conditioning, flow and
catchment rasters inherit them.

Before the styles are written, exact statistics (minimum, maximum, mean,
standard deviation, valid percentage) are computed over the valid cells of
each displayed raster and written to its GDAL `.aux.xml` sidecar, replacing
any stale or approximate values left by other tools. Both the sidecars and
the stretches baked into the project then come from the same computation,
and the numbers QGIS reports in Layer Properties are the real ones rather
than an estimate from a subsample.

## Assumptions and limitations

- The hillshade is illuminated from the north-west (azimuth 315°, altitude
  45°) with no vertical exaggeration. Like any single-azimuth shading it can
  invert the apparent relief of features aligned with the light.
- Percentile cuts are computed over the whole raster, not the catchment. A
  DEM extended to fit a large catchment therefore stretches against its full
  coverage, which is the right default for inspecting the delineation in its
  terrain context but not for mapping the catchment alone.
- Colours are chosen for screen use in QGIS's default light theme; they are
  not a cartographic standard, and nothing downstream should infer values
  from them. The legend and the `identify` tool report the real numbers.
- Layer styling, not data: re-running the export after editing the rasters
  restyles them, it never reconciles them. The hydrological outputs remain
  the single source of truth.
