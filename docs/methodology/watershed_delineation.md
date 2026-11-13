# Watershed delineation

**Outputs:** catchment raster (`catchment.tif`, 1 = catchment) and catchment
polygon (`catchment.geojson`, WGS84). **Kind: derived.**

## What it represents

The **upstream catchment** of the snapped outlet: every cell whose surface
flow, as modelled by the D8 pointer, passes through the outlet cell. The
outlet cell itself is included.

## Algorithm

1. Starting from the snapped outlet cell, the D8 pointer graph is traversed
   upstream: a cell belongs to the catchment exactly when its downstream cell
   does. Because each cell has a single downstream cell, the traversal visits
   every catchment cell exactly once.
2. **Consistency check:** the number of catchment cells must equal the flow
   accumulation value at the outlet (computed independently); a mismatch
   aborts the run.
3. **Boundary-contact check:** if any catchment cell lies on the raster edge
   or is 8-adjacent to a nodata cell, the true catchment may extend beyond
   the available elevation data. The result is then *not* accepted: with
   automatically acquired data the DEM radius is doubled (up to
   `--max-radius`) and the whole pipeline re-runs; with a user DEM, or at the
   maximum radius, the run fails with an explanation instead of returning a
   truncated boundary.
4. The accepted catchment raster is converted to a polygon
   (8-connectivity, `rasterio.features.shapes`) and reprojected from the UTM
   analysis grid to WGS84 for the GeoJSON output.

The upstream traversal always runs in the built-in engine on the validated
pointer raster, so it is identical regardless of which engine produced the
pointer.

## Data source and resolution

Inputs: D8 flow pointer and flow accumulation
([d8_flow_direction.md](d8_flow_direction.md),
[flow_accumulation.md](flow_accumulation.md)) on the ≈30 m UTM grid.

## Assumptions and limitations

- The boundary is a **model result at 30 m resolution**, not a surveyed
  divide: its edges follow cell boundaries, and the divide can be misplaced
  by one or more cells wherever the DEM smooths low ridges, or where D8's
  8-direction quantisation displaces flow paths.
- Subsurface flow, culverts, canals, pumping and karst are invisible to a
  surface DEM; real contributing areas can differ, especially in flat,
  urbanised or irrigated terrain.
- Depression treatment forces closed basins inside the catchment to drain
  (see [depression_treatment.md](depression_treatment.md)), which may include
  areas that in reality only contribute after their storage fills.
- In very flat terrain (relief < 1 m, warned in the report) the delineation
  is essentially arbitrary.
