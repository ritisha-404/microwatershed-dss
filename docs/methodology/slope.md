# Slope

**Metrics:** per-cell slope raster (`slope.tif`, degrees) and `mean_slope`
(degrees). **Kind: derived** — computed from measured elevations by the
algorithm below.

## What it measures

The steepness of the terrain surface: the angle between the local terrain
plane and the horizontal, in degrees (0° = flat, 45° = 100% grade, 90° =
vertical). Slope controls runoff velocity, infiltration opportunity time and
erosion risk, making it a core input to water-resource planning.

## Algorithm

**Horn's (1981) third-order finite difference** over each cell's 3x3
neighbourhood — the standard method used by WhiteboxTools, GDAL and ArcGIS.
With the neighbourhood labelled

```
a b c
d e f
g h i
```

and cell sizes `dx`, `dy` in metres:

```
dz/dx = ((c + 2f + i) - (a + 2d + g)) / (8 * dx)
dz/dy = ((g + 2h + i) - (a + 2b + c)) / (8 * dy)
slope = arctan( sqrt( (dz/dx)^2 + (dz/dy)^2 ) )        [converted to degrees]
```

`mean_slope` is the arithmetic mean of the per-cell slope over valid cells in
the area of interest.

### Engine

The computation runs in **WhiteboxTools** (`Slope`, units=degrees) when a
working installation is found — the native `whitebox_tools` binary on PATH or
the `whitebox` pip package. Otherwise a built-in NumPy implementation of the
identical Horn formula is used. The engine actually used is recorded in the
report (`processing.slope_engine`); results are numerically equivalent apart
from floating-point rounding and edge handling.

The DEM is always reprojected to the local UTM zone before this step, so
horizontal and vertical units are both metres.

## Data source and resolution

See [dem_source.md](dem_source.md). Slope is computed on the ≈30 m UTM
analysis grid.

## Assumptions and limitations

- **Resolution bias:** slope from a 30 m grid underestimates true local
  slopes, because each cell averages the surface over 30 m; short steep banks
  (gully walls, bunds, terraces) are smoothed away. Comparisons are only
  valid between slopes computed at the same resolution.
- **Noise sensitivity:** slope differentiates the DEM, amplifying
  high-frequency elevation noise; SRTM speckle can add roughly 1-2° of noise
  on flat terrain.
- Border cells and cells whose 3x3 neighbourhood touches nodata are set to
  nodata and excluded from statistics.
- Mean slope on a skewed distribution (mostly flat with a steep escarpment)
  can misrepresent "typical" terrain — read it together with the
  [slope classification](slope_classification.md) distribution.
