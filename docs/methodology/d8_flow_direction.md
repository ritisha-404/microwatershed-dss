# D8 flow direction

**Output:** flow pointer raster (`flow_direction.tif`). **Kind: derived** —
computed from the conditioned DEM.

## What it represents

For every cell, the single neighbouring cell that water leaving it flows
into. This is the **D8 model** (O'Callaghan & Mark 1984): all flow from a
cell follows exactly one of the 8 compass directions.

## Algorithm

Each cell drains to the neighbour with the steepest downward gradient,

```
gradient = (z_cell - z_neighbour) / distance
```

where `distance` is 1 cell size for cardinal neighbours and √2 cell sizes for
diagonal ones. Directions are stored in the WhiteboxTools power-of-two
encoding:

```
64  128   1
32    0   2        (0 = no downslope neighbour)
16    8   4
```

A value of 0 is legitimate only where flow leaves the valid data (grid edge
or next to nodata); after depression treatment, interior zeros are a
validation failure and force recomputation with the built-in engine.

### Engine

`D8Pointer` in **WhiteboxTools** when available, otherwise the built-in NumPy
implementation of the same rule. Both produce the identical encoding and are
cross-validated in the test suite.

## Data source and resolution

Input: the conditioned DEM (see
[depression_treatment.md](depression_treatment.md)) on the ≈30 m UTM grid.

## Assumptions and limitations

- **Single flow direction:** all flow from a cell goes to exactly one
  neighbour. Divergent flow on ridges, fans and convex hillslopes is not
  represented (multiple-flow-direction models exist but are unsuitable for
  discrete watershed delineation).
- **8 directions only:** flow paths are quantised to 45° increments, which
  biases path lengths and can displace drainage lines by up to one cell per
  step on terrain oblique to the grid.
- On perfectly flat terrain (after conditioning this can only be terrain that
  was flat to begin with), directions follow the epsilon gradient applied by
  conditioning and are essentially arbitrary; the report warns when total DEM
  relief is below 1 m.
- Direction ties (two neighbours with identical steepest gradient) are broken
  by a fixed clockwise-from-north-east preference; real divides through such
  cells are ambiguous at cell scale.
