# Methodology documentation

Every metric in a terrain assessment or watershed delineation points at one
of the documents in this directory. Each document states what the metric or
output represents, the formula or algorithm used, the input data, the spatial
resolution, and the assumptions and limitations that apply.

## Measured vs derived values

The JSON report tags every metric with a `kind`:

- **measured** — the value is read directly from the cells of the source DEM
  (e.g. minimum elevation). No algorithm beyond selecting and aggregating
  cells is applied by this software. These values still inherit the accuracy
  of the source data: the DEM itself is a *measurement product* built from
  radar interferometry and similar remote sensing, not a ground survey.
- **derived** — the value is computed from measured values by a documented
  algorithm (e.g. slope from elevation differences between neighbouring
  cells). Derived values add the algorithm's assumptions on top of the source
  data's accuracy.

The application produces no modelled estimates or predictions of unmeasured
quantities (such as rainfall or runoff volumes); if a future version adds
them, they will carry a third kind (`estimated`) and their own methodology
documents. The watershed boundary and derived rasters are deterministic
algorithm outputs over the measured DEM and are tagged `derived`.

## Documents

### Terrain assessment (Milestone 1)

| Document | Covers |
|---|---|
| [dem_source.md](dem_source.md) | The elevation data source, acquisition and validation |
| [elevation_statistics.md](elevation_statistics.md) | Min / max / mean elevation and elevation range |
| [slope.md](slope.md) | Per-cell slope and mean slope |
| [slope_classification.md](slope_classification.md) | Slope classes and area distribution |

### Watershed delineation (Milestone 2)

The delineation pipeline, in processing order:

| Document | Covers |
|---|---|
| [depression_treatment.md](depression_treatment.md) | Depression filling vs breaching, conditioning validation |
| [d8_flow_direction.md](d8_flow_direction.md) | The D8 flow pointer |
| [flow_accumulation.md](flow_accumulation.md) | Upstream cell counts and the mass-balance check |
| [outlet_snapping.md](outlet_snapping.md) | Moving the outlet onto the drainage network |
| [watershed_delineation.md](watershed_delineation.md) | Upstream traversal, boundary checks, polygon output |
| [catchment_area.md](catchment_area.md) | Catchment area in ha and km² |
| [qgis_visualisation.md](qgis_visualisation.md) | How the outputs are styled for QGIS (display only) |
