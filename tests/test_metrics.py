import numpy as np
import pytest

from conftest import write_dem
from microwatershed.metrics import elevation, slope
from microwatershed.metrics.base import DERIVED, MEASURED, RasterData, load_raster


def make_raster(values, resolution=30.0, aoi_mask=None):
    return RasterData(
        values=np.asarray(values, dtype="float64"),
        resolution_m=resolution,
        transform=None,
        crs=None,
        aoi_mask=aoi_mask,
    )


class TestElevationMetrics:
    def test_basic_stats(self):
        dem = make_raster([[100.0, 200.0], [300.0, 400.0]])
        by_name = {m.name: m for m in elevation.elevation_metrics(dem)}
        assert by_name["min_elevation"].value == 100.0
        assert by_name["max_elevation"].value == 400.0
        assert by_name["mean_elevation"].value == 250.0
        assert by_name["elevation_range"].value == 300.0
        assert all(m.kind == MEASURED for m in by_name.values())
        assert all(m.unit == "m" for m in by_name.values())
        assert all(m.methodology for m in by_name.values())

    def test_nan_cells_excluded(self):
        dem = make_raster([[100.0, np.nan], [300.0, np.nan]])
        by_name = {m.name: m for m in elevation.elevation_metrics(dem)}
        assert by_name["mean_elevation"].value == 200.0

    def test_aoi_mask_applied(self):
        mask = np.array([[True, False], [True, False]])
        dem = make_raster([[100.0, 9000.0], [300.0, 9000.0]], aoi_mask=mask)
        by_name = {m.name: m for m in elevation.elevation_metrics(dem)}
        assert by_name["max_elevation"].value == 300.0

    def test_empty_raises(self):
        dem = make_raster([[np.nan, np.nan]])
        with pytest.raises(ValueError, match="No valid elevation"):
            elevation.elevation_metrics(dem)


class TestSlopeMetrics:
    def test_mean_slope(self):
        s = make_raster([[0.0, 10.0], [20.0, 30.0]])
        (metric,) = slope.slope_metrics(s)
        assert metric.value == 15.0
        assert metric.kind == DERIVED
        assert metric.unit == "degrees"

    def test_classification_areas(self):
        # 4 cells of 30 m: one per class boundary case.
        s = make_raster([[0.0, 2.0], [5.0, 40.0]], resolution=30.0)
        metric = slope.slope_classification(s)
        classes = {c["class"]: c for c in metric.value["classes"]}
        cell_km2 = 0.0009
        assert classes["flat_or_almost_flat"]["area_km2"] == pytest.approx(cell_km2)
        assert classes["gently_sloping"]["area_km2"] == pytest.approx(cell_km2)  # 2.0 goes up a class
        assert classes["moderately_sloping"]["area_km2"] == pytest.approx(cell_km2)
        assert classes["very_steep"]["area_km2"] == pytest.approx(cell_km2)
        assert metric.value["total_assessed_area_km2"] == pytest.approx(4 * cell_km2)
        assert sum(c["percent_of_area"] for c in metric.value["classes"]) == pytest.approx(100.0)

    def test_steepest_class_inclusive_of_90(self):
        s = make_raster([[90.0]])
        metric = slope.slope_classification(s)
        very_steep = next(c for c in metric.value["classes"] if c["class"] == "very_steep")
        assert very_steep["percent_of_area"] == 100.0


class TestLoadRaster:
    def test_nodata_to_nan_and_aoi_circle(self, tmp_path):
        data = np.full((60, 60), 500.0)
        data[0, 0] = -32768.0
        path = write_dem(tmp_path / "dem.tif", data, resolution=30.0, crs="EPSG:32643")
        # Centre of this synthetic grid in EPSG:32643 is at (500900, 1959100);
        # inverse of that is roughly lat 17.72, lon 74.009 (UTM zone 43N).
        raster = load_raster(path)
        assert np.isnan(raster.values[0, 0])
        assert raster.resolution_m == 30.0

        from rasterio.warp import transform as warp_transform

        lons, lats = warp_transform("EPSG:32643", "EPSG:4326", [500_900.0], [1_959_100.0])
        aoi_raster = load_raster(path, aoi=(lats[0], lons[0], 0.5))
        assert aoi_raster.aoi_mask is not None
        assert 0 < aoi_raster.aoi_mask.sum() < aoi_raster.values.size
        # ~0.5 km radius circle at 30 m cells: area within 10% of pi*r^2.
        area = aoi_raster.aoi_mask.sum() * raster.cell_area_km2
        assert area == pytest.approx(np.pi * 0.25, rel=0.1)
