# Elevation data source and acquisition

## What it provides

A digital elevation model (DEM): a raster grid whose cells hold the elevation
of the terrain surface in metres above sea level. Every metric in the terrain
assessment is computed from this grid.

## Data source

**AWS Terrain Tiles** (`s3://elevation-tiles-prod`, part of the AWS Open Data
program; originally produced by Mapzen, continued as Nextzen). The tiles are
256x256 px PNG images in web-mercator (EPSG:3857) using the **Terrarium**
encoding, decoded as:

```
elevation_m = (R * 256 + G + B / 256) - 32768
```

The tileset is a composite of public elevation sources. For most of the world
between 60°N and 56°S the source is **NASA SRTM v3 (~30 m)**; higher latitudes
use GMTED2010, oceans use ETOPO1 bathymetry, and some regions use
higher-resolution national datasets (e.g. 3DEP in the United States,
regional lidar). The exact source for a given tile is not reported by the
service, so this application conservatively describes accuracy in SRTM terms.

No API key or registration is required. Source registry entry:
<https://registry.opendata.aws/terrain-tiles/>

Alternatively, the user may supply their own GeoTIFF DEM with `--dem`; in that
case this document's accuracy statements do not apply and the user is
responsible for knowing their data's provenance.

## Acquisition algorithm

1. Compute the geographic bounding box of the requested circle (point +
   radius) using the small-distance spherical approximation
   (1° latitude ≈ 110.574 km; 1° longitude ≈ 111.320 km x cos(latitude)).
2. Choose the tile zoom level whose pixel size best matches ~30 m — the
   native resolution of the dominant source data — via
   `zoom = round(log2(156543.03 * cos(lat) / 30))`, clamped to [9, 14].
3. Download all tiles intersecting the bounding box (with retries on network
   errors; missing tiles become nodata), decode Terrarium RGB to metres, and
   mosaic.
4. Clip the mosaic to the bounding box and **reproject to the local WGS84 UTM
   zone** (bilinear resampling) so that horizontal units are true metres —
   required for slope computation. Web-mercator pixels are not true metres
   (scale error grows with latitude), which is why analysis is not done in
   the tile CRS.

## Validation

Before analysis the DEM must pass these checks (failures abort the run):

- the file opens and has a coordinate reference system;
- raster is at least 10x10 pixels;
- at least 50% of cells contain valid data;
- elevations intersect the plausible land-surface range [-500, 9000] m.

Partial nodata coverage and out-of-range extremes produce warnings that are
included in the report.

## Spatial resolution

The analysis grid is approximately **30 m** (the exact value depends on
latitude and is recorded in the report as `native_resolution_m`). Features
smaller than ~2 pixels (60 m) are not resolved.

## Assumptions and limitations

- **Vertical accuracy:** SRTM absolute vertical accuracy is approximately
  ±16 m (90% confidence) globally, typically better (~±6-10 m) in low-relief
  areas. The Terrarium encoding quantises elevations to 1/256 m, which is far
  finer than the source accuracy — do not read precision into the decimals.
- **Surface vs terrain:** SRTM is a radar *surface* model; in forests and
  built-up areas elevations are biased upward toward canopy/rooftop height.
- **Vertical datum:** EGM96 geoid (orthometric heights), per the SRTM source.
- **Acquisition date:** SRTM data was collected in February 2000; terrain
  altered since (quarries, dams, major earthworks) is not reflected.
- **Resampling:** bilinear reprojection to UTM slightly smooths the surface;
  the effect on area-aggregated statistics is negligible at this resolution.
- **Mixed sources:** near coastlines, cells may come from bathymetry
  (negative values); above 60°N/below 56°S the source is coarser GMTED2010
  resampled to the requested zoom, so real information content is lower than
  the grid spacing suggests.
