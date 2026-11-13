# Flow accumulation

**Output:** flow accumulation raster (`flow_accumulation.tif`, upstream cell
count). **Kind: derived** — computed from the D8 flow pointer.

## What it represents

For every cell, the number of cells whose flow eventually passes through it,
**including the cell itself** (so the minimum value is 1). Multiplying by the
cell area (900 m² at 30 m) converts it to upstream contributing area. High
values trace the drainage network; the value at a catchment outlet equals the
catchment's cell count.

## Algorithm

Cells are processed in topological order of the D8 pointer graph: each cell
starts with a load of 1 and passes its accumulated load to its single
downstream cell. The built-in engine implements this with Kahn's algorithm in
vectorised waves; WhiteboxTools' `D8FlowAccumulation` (run on the pointer
raster, `out_type=cells`) is used when available.

### Validation: mass balance

Because every cell contributes exactly 1 and all flow must eventually leave
the valid data, the accumulation summed over *terminal* cells (cells whose
flow exits the grid or enters nodata) must equal the total number of valid
cells — no water may be created or lost. Every run checks this invariant; a
violation (e.g. flow swallowed by an unresolved pit) discards the result and
recomputes with the built-in engine, or aborts. This directly guards against
the "suspiciously low flow accumulation" failure mode of unconditioned DEMs.

The delineated catchment is additionally required to contain exactly as many
cells as the outlet's accumulation value — the two are computed by different
code paths and must agree.

## Data source and resolution

Input: the D8 flow pointer (see
[d8_flow_direction.md](d8_flow_direction.md)) on the ≈30 m UTM grid.

## Assumptions and limitations

- Inherits all D8 assumptions: convergent-only flow means accumulation
  concentrates into single-cell-wide channels faster than real overland flow
  does; values on hillslopes are underestimates of true contributing area.
- Cells outside the DEM contribute nothing: accumulation near the data
  boundary is only valid if no real drainage enters from outside the raster
  (the boundary-contact check on the catchment guards the delineation
  against this).
- Accumulation is a cell count, not discharge: it weights every cell equally
  regardless of rainfall, infiltration or land cover.
