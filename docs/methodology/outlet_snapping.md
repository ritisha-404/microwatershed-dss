# Outlet snapping

**Output:** snapped outlet coordinates (`delineation.snapped_outlet` in the
JSON report, and the `outlet_snapped` feature in `catchment.geojson`).
**Kind: derived.**

## Why it is needed

A user-supplied outlet point is rarely exactly on the modelled drainage line:
GPS error, map-reading error and the D8 model's own displacement of channels
(up to a cell or two) all shift it. Delineating from a point one cell off the
channel yields a tiny hillslope catchment instead of the intended watershed,
so the point must first be moved onto the drainage network.

## Algorithm

**Nearest-drainage-cell snapping** (after Jenson 1991): within the snapping
distance (`--snap-distance`, default 150 m), the outlet moves to the *nearest*
cell whose flow accumulation is at least the stream threshold
(`--stream-threshold`, default 100 cells ≈ 0.09 km² upstream at 30 m).
Distance ties are broken toward the larger accumulation.

If no cell within range reaches the threshold, the highest-accumulation cell
within range is used instead and the report carries an explicit warning that
the point does not appear to lie on a drainage network.

The report always records the snapped coordinates (WGS84 and projected), the
distance moved, the accumulation at the snapped cell, and whether the stream
threshold was met.

### Why nearest-cell rather than maximum accumulation

Snapping to the *maximum* accumulation within a radius (the other common
approach) is biased: accumulation grows monotonically downstream, so the
outlet always slides as far down-channel as the radius allows, silently
enlarging the catchment. Nearest-cell snapping moves the point the minimum
distance necessary and is stable under changes to the snap radius.

## Data source and resolution

Input: the flow accumulation raster (see
[flow_accumulation.md](flow_accumulation.md)). Snapping resolution is one
cell (~30 m); the snapped point is reported at the cell centre.

## Assumptions and limitations

- The stream threshold defines what counts as "the drainage network"; with
  the default 100 cells, streams draining less than ~0.09 km² are invisible
  to snapping. Lower the threshold for very small target catchments.
- If several channels pass within the snap distance (confluences, parallel
  streams), the nearest one wins even if the user intended another; check the
  reported snap distance and the map. The test suite covers this
  multiple-drainage-paths case.
- A snap distance of 0 disables snapping entirely: the catchment is
  delineated from the exact cell containing the input coordinates.
- Snapping is recomputed whenever DEM coverage is automatically expanded
  (grid alignment and data completeness change with the window), so a stream
  sitting near the accumulation threshold can snap differently at different
  radii. The expansion history in the report records the catchment size at
  each radius; a large change between steps signals such a switch and the
  snapped point should be inspected on the map.
