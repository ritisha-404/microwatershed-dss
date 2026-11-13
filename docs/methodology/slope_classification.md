# Slope classification and area distribution

**Metric:** `slope_classification` — area (km²) and share (%) of the area of
interest in each slope class. **Kind: derived** — a reclassification of the
derived slope raster.

## What it measures

How the assessed area is distributed across slope-steepness classes. For
water-resource planning this indicates, at a glance, where storage and
recharge interventions are plausible (flat to moderate classes) versus where
runoff is fast and erosion control dominates (steep classes).

## Classes

FAO-style slope classes, bounds in degrees, intervals closed below and open
above (`low <= slope < high`):

| Class | Range (degrees) |
|---|---|
| Flat or almost flat | 0 - 2 |
| Gently sloping | 2 - 5 |
| Moderately sloping | 5 - 10 |
| Strongly sloping | 10 - 15 |
| Steep | 15 - 30 |
| Very steep | >= 30 |

These bounds follow the slope classes commonly used in terrain and watershed
analysis, adapted from the FAO *Guidelines for Soil Description* scheme
(which is defined in percent) to round degree values. They are a reporting
convention, not a physical threshold; users with a local standard (e.g. a
state land-development classification) should reclassify `slope.tif`
accordingly.

## Algorithm

1. Take all valid slope cells inside the area of interest (see
   [slope.md](slope.md)).
2. Count cells per class.
3. `area_km2 = count * cellsize^2 / 10^6` with cellsize in metres;
   `percent_of_area = 100 * count / total_valid_cells`.

## Data source and resolution

See [dem_source.md](dem_source.md) and [slope.md](slope.md). Computed on the
≈30 m UTM analysis grid; the per-cell area is exact in that projection to
within UTM scale distortion (< 0.1% near the zone centre, < 0.4% at zone
edges).

## Assumptions and limitations

- Inherits every limitation of the slope computation, most importantly
  resolution bias: at 30 m, area in the steepest classes is systematically
  understated and the flat classes correspondingly overstated relative to
  what a finer DEM would report.
- Class shares are computed over *valid* cells only; with partial nodata
  coverage the areas do not sum to the geometric AOI area (the report's
  `total_assessed_area_km2` is the valid-cell area).
- Percentages are rounded to 0.01%; rounding can make shares sum to slightly
  more or less than 100%.
