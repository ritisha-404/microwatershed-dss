# Elevation statistics

**Metrics:** `min_elevation`, `max_elevation`, `mean_elevation`,
`elevation_range` — all in metres. **Kind: measured** (range is an arithmetic
combination of two measured values; no model is applied).

## What they measure

Summary statistics of the terrain surface height within the area of interest
(AOI): the circle of the requested radius around the requested point,
evaluated in the projected (UTM) analysis grid. For a user-supplied DEM
without a point/radius, the AOI is the full raster extent.

- **Minimum / maximum elevation** — lowest / highest valid cell in the AOI.
- **Mean elevation** — arithmetic mean of all valid cells.
- **Elevation range (local relief)** — `max_elevation - min_elevation`; a
  first-order indicator of how much vertical energy is available to drive
  runoff in the area.

## Formula

For the set *Z* of valid (non-nodata, finite) cell values inside the AOI:

```
min  = min(Z)
max  = max(Z)
mean = (1/n) * sum(Z)
range = max - min
```

Cells are **not** area-weighted: in the UTM analysis grid all cells have equal
area, so the arithmetic mean is the area-weighted mean.

## Data source and resolution

See [dem_source.md](dem_source.md). Analysis grid ≈ 30 m; values in metres
above the EGM96 geoid.

## Assumptions and limitations

- Nodata cells (missing tiles, outside data extent) are excluded; the report
  warns when coverage is below 100% and aborts below 50%.
- Min and max are single-cell values and therefore the statistics most
  sensitive to source noise and voids; the ±16 m (90%) SRTM vertical accuracy
  applies to each cell, and `elevation_range` combines the errors of two
  cells (worst case roughly ±23 m at 90% if errors are independent).
- The AOI circle is rasterised: a cell is included if its centre lies within
  the radius, so the assessed area differs from the ideal circle by up to
  about one cell-width ring.
- In vegetated or built-up terrain, values represent the radar surface
  (canopy/rooftops), not bare ground.
