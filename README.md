# Microwatershed DSS

A decision-support tool for water-resource planning. Give it a geographic
point and it automatically downloads elevation data — no shapefile, no manual
DEM hunting, no QGIS required.

**Terrain assessment (Milestone 1):**

```
python -m microwatershed.main --lat 17.68 --lon 74.02 --radius 2
```

produces in `outputs/`:

| File | Contents |
|---|---|
| `terrain_assessment.json` | Metrics with units, provenance (measured vs derived) and methodology references |
| `terrain_assessment.html` | Self-contained visual report: elevation / slope / hillshade maps, metric tiles, slope-class distribution |
| `dem.tif` | The analysed DEM (GeoTIFF, local UTM zone, ~30 m) |
| `slope.tif` | Slope in degrees (GeoTIFF) |
| `hillshade.tif` | Hillshade for visualization (GeoTIFF) |

Metrics: minimum / maximum / mean elevation, elevation range, mean slope, and
the area distribution across slope classes. Every metric links to a
methodology document in [`docs/methodology/`](docs/methodology/README.md)
covering what it measures, the algorithm, data source, resolution, and
limitations.

**Watershed delineation (Milestone 2):** give it an outlet point and it
delineates the upstream catchment draining through it, then runs the terrain
assessment over the catchment:

```
python -m microwatershed.delineate --lat 17.68 --lon 74.02
```

produces in `outputs/watershed/`:

| File | Contents |
|---|---|
| `catchment.geojson` | Catchment polygon + snapped/requested outlet points (WGS84), with area properties |
| `catchment.tif` | Catchment raster (1 = catchment) |
| `flow_direction.tif` | D8 flow pointer (Whitebox encoding) |
| `flow_accumulation.tif` | Upstream cell count per cell |
| `dem_conditioned.tif` | Hydrologically conditioned DEM used for flow modelling |
| `watershed_assessment.json` | Snapped outlet, catchment area (ha / km²), validation checks, terrain metrics |
| `watershed_assessment.html` | Visual report: catchment over elevation / flow accumulation / slope, metric tiles |
| `dem.tif`, `slope.tif` | The analysed DEM and its slope |
| `watershed.qgs` | QGIS project: every layer styled, ordered and zoomed to the data |
| `hillshade.tif`, `*.qml` | Relief shading and per-layer QGIS styles used by the project |

The pipeline conditions the DEM (least-cost breaching + filling with
WhiteboxTools, or built-in priority-flood filling), computes the D8 pointer
and flow accumulation, snaps the outlet to the nearest drainage cell within a
configurable distance, traverses the catchment upstream and converts it to a
polygon. Every intermediate output is validated: no unresolved pits or flats,
flow-accumulation mass balance, and catchment/accumulation consistency. DEM
coverage starts at `--radius` km and doubles automatically (up to
`--max-radius`) whenever the catchment reaches the data boundary; a catchment
that still does not fit is reported as an error, never returned truncated.

## How it works

1. **Acquire** — downloads the terrain tiles covering the point + radius from
   the [AWS Terrain Tiles open dataset](https://registry.opendata.aws/terrain-tiles/)
   (SRTM-based, ~30 m, global, no API key), decodes and mosaics them, and
   reprojects to the local UTM zone. Alternatively analyse your own GeoTIFF
   with `--dem path/to/dem.tif`.
2. **Validate** — checks georeferencing, data coverage and elevation
   plausibility before any analysis; fails loudly rather than fabricating
   results.
3. **Process** — computes slope (Horn's method, degrees) with WhiteboxTools
   when available, or an equivalent built-in NumPy implementation otherwise;
   the engine used is recorded in the report.
4. **Report** — metrics are computed over the circular area of interest and
   written as JSON and a single-file HTML report.

## Installation

Requires Python 3.11+. The analysis engine is independent of QGIS.

### NixOS / Nix

With flakes:

```bash
cd microwatershed-dss
nix develop          # or: nix-shell
python -m microwatershed.main --lat 17.68 --lon 74.02 --radius 2
```

The dev shell provides Python with numpy/rasterio/matplotlib/pytest plus the
native `whitebox-tools` binary, and puts `src/` on `PYTHONPATH` — no pip step
needed.

### Windows

```powershell
cd microwatershed-dss
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
python -m microwatershed.main --lat 17.68 --lon 74.02 --radius 2
```

Notes:
- `numpy`, `rasterio` and `matplotlib` install as binary wheels; no compiler
  or separate GDAL install is needed.
- The `whitebox` package downloads the WhiteboxTools binary on first run. If
  that fails (offline/locked-down machines), the application automatically
  falls back to its built-in NumPy slope implementation — results are the
  same algorithm (Horn 1981).

### Other Linux / macOS

Same as Windows but `python3.11 -m venv .venv && source .venv/bin/activate`.

## Usage

### Terrain assessment

```
python -m microwatershed.main --lat LAT --lon LON [--radius KM]
                              [--dem FILE.tif] [--output-dir DIR]
                              [--no-whitebox] [-v]
```

- `--lat/--lon` — point of interest, WGS84 decimal degrees.
- `--radius` — analysis radius in km (default 2, range 0.25–50). Metrics are
  computed over the circle; rasters cover its bounding box.
- `--dem` — analyse a local GeoTIFF instead of downloading. With `--lat/--lon`
  the analysis is restricted to the radius; without, the full extent is used.
  Geographic-CRS DEMs are reprojected to UTM automatically.
- `--no-whitebox` — force the built-in slope implementation.

### Watershed delineation

```
python -m microwatershed.delineate --lat LAT --lon LON
                                   [--snap-distance M] [--stream-threshold CELLS]
                                   [--radius KM] [--max-radius KM]
                                   [--dem FILE.tif] [--output-dir DIR]
                                   [--no-whitebox] [-v]
```

- `--lat/--lon` — outlet point, WGS84 decimal degrees (required).
- `--snap-distance` — how far the outlet may be moved onto the drainage
  network, metres (default 150; 0 disables snapping).
- `--stream-threshold` — minimum upstream cells for a cell to count as
  drainage network when snapping (default 100 ≈ 0.09 km² at 30 m).
- `--radius` / `--max-radius` — initial and maximum DEM coverage radius in km
  (defaults 2 and 20). Coverage doubles automatically while the catchment
  reaches the data boundary.
- `--dem` — delineate on a local GeoTIFF; coverage cannot expand, so a
  catchment reaching the DEM edge is an error.
- `--no-whitebox` — force the built-in hydrology engine (priority-flood fill
  instead of least-cost breaching).

Both commands: exit codes `0` success, `1` validation/acquisition/processing
failure, `2` usage error.

### Viewing the results in QGIS

`delineate` writes `outputs/watershed/watershed.qgs` alongside the data.
Open it in QGIS (3.x) and the map is ready: outlets, catchment boundary, flow
accumulation, elevation and hillshade, in that order, in the DEM's UTM zone,
zoomed to the data.

```
python -m microwatershed.qgis_export [--results-dir DIR] [--project FILE.qgs]
                                     [--no-styles] [--no-statistics]
```

rebuilds the project for an existing run — useful after restyling, or for
results produced before this existed. It only reads the outputs: the DEM,
flow direction, flow accumulation and catchment files are never modified.

What the project sets up, and why, is in
[docs/methodology/qgis_visualisation.md](docs/methodology/qgis_visualisation.md):
elevation stretched to the 2–98 % cut over a terrain ramp, flow accumulation
on a logarithmic ramp that fades out hillslope cells, an unfilled catchment
polygon with a haloed boundary, distinct symbols for the requested and
snapped outlets, transparent nodata everywhere, and exact band statistics
refreshed in the GDAL sidecars so QGIS stretches against the real numbers.
Each raster also gets a `.qml` next to it, so loading a single GeoTIFF by
hand picks up the same styling; the two vector styles
(`catchment_boundary.qml`, `outlets.qml`) load from Layer Properties ▸ Style
▸ Load Style. Layer sources are relative, so the whole `outputs/watershed/`
directory can be zipped and opened elsewhere.

Writing the project needs no QGIS installation — it is plain XML, generated
with the standard library.

## Running the tests

```bash
pytest                 # offline tests (synthetic DEMs, no downloads)
pytest -m network      # adds the end-to-end acceptance test (downloads tiles)
```

## Project layout

```
src/microwatershed/
  data/         DEM acquisition (AWS Terrain Tiles) and validation
  hydrology/    terrain processing (slope, hillshade), D8 hydrology engine
                (flow.py) and watershed delineation pipeline (delineation.py)
  metrics/      metric calculations with provenance
  report/       JSON and HTML report generation (terrain + watershed)
  qgis_export/  QGIS project and layer styles for the delineation outputs
  main.py       terrain assessment entry point
  delineate.py  watershed delineation entry point
docs/methodology/   one document per metric/algorithm/data source
tests/              automated tests
outputs/            default output directory
```

QGIS is a consumer of the outputs, never a dependency of the analysis: the
engine runs without it, and `qgis_export/` only writes project and style XML
describing how the existing files should be drawn. Rainfall modelling and
water-balance analysis are out of scope for this milestone.

## Data attribution

Elevation data: AWS Terrain Tiles (Mapzen/Nextzen), containing NASA SRTM,
USGS GMTED2010/3DEP, NOAA ETOPO1 and other public sources. See
[docs/methodology/dem_source.md](docs/methodology/dem_source.md).
