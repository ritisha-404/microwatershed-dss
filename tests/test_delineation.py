"""Tests for the watershed delineation pipeline on synthetic DEMs.

The diamond-valley DEM (see conftest) has an analytically known catchment of
exactly 1712 cells, so success is asserted against the correct answer, not
just "a polygon came back". Scenarios covered: outlet on the drainage
network, outlet slightly off it, outlet far from any stream, outlet on the
DEM boundary, nodata inside the catchment, flat terrain, depressions inside
the catchment, multiple candidate drainage paths, and agreement between the
WhiteboxTools and built-in engines.
"""

import json
import shutil

import numpy as np
import pytest
import rasterio

from conftest import DIAMOND_CATCHMENT_CELLS, DIAMOND_OUTLET, cell_latlon, diamond_valley, write_dem
from microwatershed.hydrology import delineation

RES = 30.0
CELL_KM2 = RES * RES / 1e6


def _has_whitebox():
    if shutil.which("whitebox_tools"):
        return True
    try:
        import whitebox  # noqa: F401

        return True
    except ImportError:
        return False


needs_whitebox = pytest.mark.skipif(not _has_whitebox(), reason="WhiteboxTools not installed")


@pytest.fixture
def valley_dem(tmp_path):
    return write_dem(tmp_path / "valley.tif", diamond_valley(), resolution=RES)


def run(dem, row, col, out_dir=None, **kwargs):
    """Delineate; when out_dir is given, also write outputs (as the CLI does
    after accepting a result) and attach the path mapping as ``.written``."""
    lat, lon = cell_latlon(dem, row, col)
    kwargs.setdefault("prefer_whitebox", False)
    result = delineation.delineate(dem, lat, lon, **kwargs)
    result.written = delineation.write_outputs(result, out_dir) if out_dir else None
    return result


class TestKnownCatchment:
    def test_exact_catchment_numpy(self, valley_dem, tmp_path):
        result = run(valley_dem, *DIAMOND_OUTLET, tmp_path / "out")
        assert result.engine == "numpy"
        assert result.cell_count == DIAMOND_CATCHMENT_CELLS
        assert result.area_km2 == pytest.approx(DIAMOND_CATCHMENT_CELLS * CELL_KM2)
        assert result.area_ha == pytest.approx(result.area_km2 * 100.0)
        assert not result.touches_boundary
        assert result.snapped.distance_moved_m == 0.0
        assert result.snapped.on_stream
        assert result.validation["mass_balance"]["passed"]
        assert result.validation["interior_pits_after_conditioning"] == 0
        assert result.validation["unresolved_interior_cells"] == 0

    @needs_whitebox
    def test_whitebox_agrees_with_numpy(self, valley_dem, tmp_path):
        wb = run(valley_dem, *DIAMOND_OUTLET, tmp_path / "wb", prefer_whitebox=True)
        np_ = run(valley_dem, *DIAMOND_OUTLET, tmp_path / "np", prefer_whitebox=False)
        assert wb.engine in ("whitebox-binary", "whitebox-pip")
        assert wb.cell_count == np_.cell_count == DIAMOND_CATCHMENT_CELLS
        np.testing.assert_array_equal(wb.catchment_mask, np_.catchment_mask)
        assert wb.validation["mass_balance"]["passed"]

    @needs_whitebox
    def test_depression_comparison_reported(self, tmp_path):
        z = diamond_valley()
        z[25, 40] -= 5.0  # closed depression inside the basin
        dem = write_dem(tmp_path / "pit.tif", z, resolution=RES)
        result = run(dem, *DIAMOND_OUTLET, tmp_path / "out", prefer_whitebox=True)
        assert result.cell_count == DIAMOND_CATCHMENT_CELLS  # pit must not break the catchment
        comp = result.depression_comparison
        assert comp is not None
        assert comp["fill_only"]["cells_modified"] >= 1
        assert comp["selected"] == "breach_then_fill"

    def test_pit_inside_catchment_filled_numpy(self, tmp_path):
        z = diamond_valley()
        z[25, 40] -= 5.0
        dem = write_dem(tmp_path / "pit.tif", z, resolution=RES)
        result = run(dem, *DIAMOND_OUTLET, tmp_path / "out")
        assert result.cell_count == DIAMOND_CATCHMENT_CELLS


class TestSnapping:
    def test_outlet_slightly_off_network_snaps(self, valley_dem, tmp_path):
        # 3 cells south of the valley axis; nearest stream cell is (30, 69).
        result = run(valley_dem, 33, 69, tmp_path / "out", snap_distance_m=100.0)
        assert (result.snapped.row, result.snapped.col) == (30, 69)
        assert result.snapped.on_stream
        assert result.snapped.distance_moved_m == pytest.approx(90.0)
        # Same analysis as the (30, 70) outlet minus its own cell: hillslope
        # cells of column 69 drain cardinally into (30, 69): 29 x 59 cells.
        assert result.cell_count == 29 * 59

    def test_outlet_far_from_network_warns(self, valley_dem, tmp_path):
        result = run(valley_dem, 20, 40, tmp_path / "out", snap_distance_m=60.0)
        assert not result.snapped.on_stream
        assert any("drainage network" in w for w in result.warnings)
        assert result.cell_count < 20  # hillslope cell: trivially small catchment

    def test_multiple_drainage_paths_prefer_larger_stream(self, tmp_path):
        """Outlet equidistant between two parallel streams: tie broken by size."""
        rows = np.arange(61)[:, None].astype("float64")
        cols = np.arange(81)[None, :].astype("float64")
        # Valley 1 along row 18 (divide at row 26), valley 2 along row 42:
        # valley 2 drains more rows, so its accumulation is larger.
        A = np.where(rows <= 26.0, 3.0 * np.abs(rows - 18.0), 3.0 * np.abs(rows - 42.0))
        B = np.where(
            cols >= 10.0,
            np.where(cols <= 70.0, 100.0 - (cols - 10.0), 40.0 - 6.0 * (cols - 70.0)),
            100.0 - 6.0 * (10.0 - cols),
        )
        dem = write_dem(tmp_path / "two.tif", A + B, resolution=RES)
        # (30, 60) is 12 cells from both valleys; valley 2 must win the tie.
        result = run(dem, 30, 60, tmp_path / "out", snap_distance_m=400.0)
        assert result.snapped.row == 42
        assert result.snapped.on_stream

    def test_invalid_snap_distance(self, valley_dem, tmp_path):
        with pytest.raises(delineation.DelineationError, match="Snap distance"):
            run(valley_dem, *DIAMOND_OUTLET, tmp_path / "out", snap_distance_m=1e9)


class TestBoundaryAndCoverage:
    def test_outlet_on_dem_boundary_flagged(self, valley_dem, tmp_path):
        # The channel exits the east edge at (30, 80); its catchment includes
        # boundary cells, so the result must be flagged as possibly truncated.
        result = run(valley_dem, 30, 80, tmp_path / "out", snap_distance_m=0.0,
                     stream_threshold_cells=1)
        assert result.touches_boundary
        assert "east" in result.boundary_sides

    def test_nodata_inside_catchment_flagged(self, tmp_path):
        z = diamond_valley()
        z[25:28, 40:43] = np.nan  # elevation hole upstream of the outlet
        dem = write_dem(tmp_path / "hole.tif", z, resolution=RES)
        result = run(dem, *DIAMOND_OUTLET, tmp_path / "out")
        assert result.touches_boundary
        assert "nodata" in result.boundary_sides

    def test_outlet_outside_dem(self, valley_dem):
        with pytest.raises(delineation.DelineationError, match="outside the DEM"):
            delineation.delineate(valley_dem, 0.0, 0.0, prefer_whitebox=False)

    def test_outlet_on_nodata_cell(self, tmp_path):
        z = diamond_valley()
        z[28:33, 68:72] = np.nan
        dem = write_dem(tmp_path / "hole.tif", z, resolution=RES)
        lat, lon = cell_latlon(dem, *DIAMOND_OUTLET)
        with pytest.raises(delineation.DelineationError, match="nodata"):
            delineation.delineate(dem, lat, lon, prefer_whitebox=False)


class TestFlatTerrain:
    def test_flat_dem_warns_and_does_not_crash(self, tmp_path):
        dem = write_dem(tmp_path / "flat.tif", np.full((50, 50), 500.0), resolution=RES)
        result = run(dem, 25, 25, tmp_path / "out", stream_threshold_cells=1)
        assert any("flat" in w.lower() for w in result.warnings)
        assert result.validation["total_relief_m"] == 0.0
        assert result.cell_count >= 1


class TestOutputs:
    @pytest.fixture
    def result(self, valley_dem, tmp_path):
        return run(valley_dem, *DIAMOND_OUTLET, tmp_path / "out"), tmp_path / "out"

    def test_rasters_written_and_consistent(self, result):
        res, out = result
        from pathlib import Path

        for key in ("dem_conditioned_geotiff", "flow_direction_geotiff",
                    "flow_accumulation_geotiff", "catchment_geotiff", "catchment_geojson"):
            assert Path(res.written[key]).exists(), key
        with rasterio.open(res.written["catchment_geotiff"]) as src:
            catch = src.read(1)
            assert set(np.unique(catch)) <= {0, 1, 255}
            assert (catch == 1).sum() == DIAMOND_CATCHMENT_CELLS
        with rasterio.open(res.written["flow_accumulation_geotiff"]) as src:
            accum = src.read(1, masked=True)
            assert accum.max() >= DIAMOND_CATCHMENT_CELLS  # channel exits east of outlet
        with rasterio.open(res.written["flow_direction_geotiff"]) as src:
            pointer = src.read(1)
            assert set(np.unique(pointer)) <= {0, 1, 2, 4, 8, 16, 32, 64, 128, 255}

    def test_no_files_written_without_acceptance(self, valley_dem, tmp_path):
        run(valley_dem, *DIAMOND_OUTLET)  # no out_dir: nothing on disk
        assert not (tmp_path / "out").exists()

    def test_geojson_structure(self, result):
        res, out = result
        geo = json.loads((out / "catchment.geojson").read_text())
        assert geo["type"] == "FeatureCollection"
        names = [f["properties"]["name"] for f in geo["features"]]
        assert names == ["catchment", "outlet_snapped", "outlet_requested"]
        catchment = geo["features"][0]
        assert catchment["geometry"]["type"] in ("Polygon", "MultiPolygon")
        assert catchment["properties"]["area_km2"] == pytest.approx(res.area_km2, abs=1e-3)
        assert catchment["properties"]["cell_count"] == DIAMOND_CATCHMENT_CELLS
        # Coordinates must be WGS84 (the test DEM sits near 17.7 N, 75.0 E).
        ring = catchment["geometry"]["coordinates"][0]
        lons = [pt[0] for pt in ring]
        lats = [pt[1] for pt in ring]
        assert all(74.5 < x < 75.5 for x in lons)
        assert all(17.0 < y < 18.5 for y in lats)
        # The snapped outlet must sit on the catchment boundary region.
        outlet = geo["features"][1]
        assert outlet["properties"]["on_drainage_network"] is True
        assert outlet["properties"]["flow_accumulation_cells"] == DIAMOND_CATCHMENT_CELLS
