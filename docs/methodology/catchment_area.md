# Catchment area

**Metrics:** `catchment_area_ha` (hectares) and `catchment_area_km2`
(square kilometres). **Kind: derived.**

## What it measures

The horizontal (planimetric) area of the delineated upstream catchment.

## Algorithm

```
area = cell_count x (cell size)^2
```

where `cell_count` is the number of cells in the catchment raster and the
cell size is the analysis-grid resolution (~30 m). The result is reported in
hectares (x 1e-4) and square kilometres (x 1e-6). The same value, rounded,
appears in the GeoJSON polygon's properties.

The computation is performed in the local UTM zone, so cells are true square
metres.

## Data source and resolution

Input: the catchment raster (see
[watershed_delineation.md](watershed_delineation.md)) on the ≈30 m UTM grid.

## Assumptions and limitations

- **Quantisation:** the area is a multiple of one cell (~900 m²); the true
  divide crosses cells, so expect an uncertainty of at least the cell size
  times the perimeter length. For a 1 km² catchment this is typically a few
  percent.
- **Projection distortion:** UTM scale error is at most ~0.1% within a zone
  (up to ~0.3% near zone edges where the acquisition grid extends past the
  zone), far below the quantisation error.
- Planimetric area, not surface area: steep terrain has more actual surface
  per map area.
- All delineation limitations (D8 model, DEM resolution, depression
  treatment) propagate directly into the area value.
