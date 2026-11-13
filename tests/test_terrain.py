import numpy as np
import pytest
import rasterio

from conftest import write_dem
from microwatershed.hydrology import terrain


class TestNumpyHornSlope:
    def test_tilted_plane_recovers_angle(self, tilted_plane_dem, tmp_path):
        out = str(tmp_path / "slope.tif")
        engine = terrain.compute_slope(tilted_plane_dem, out, prefer_whitebox=False)
        assert engine == "numpy-horn"
        with rasterio.open(out) as src:
            slope = src.read(1)
            nodata = src.nodata
        interior = slope[5:-5, 5:-5]
        assert np.all(interior != nodata)
        assert interior == pytest.approx(10.0, abs=0.01)

    def test_flat_plane_is_zero(self, tmp_path):
        dem = write_dem(tmp_path / "flat.tif", np.full((30, 30), 450.0))
        out = str(tmp_path / "slope.tif")
        terrain.compute_slope(dem, out, prefer_whitebox=False)
        with rasterio.open(out) as src:
            interior = src.read(1)[2:-2, 2:-2]
        assert interior == pytest.approx(0.0, abs=1e-6)

    def test_nodata_neighbourhood_masked(self, tmp_path):
        data = np.full((30, 30), 450.0)
        data[14, 14] = -32768.0
        dem = write_dem(tmp_path / "hole.tif", data)
        out = str(tmp_path / "slope.tif")
        terrain.compute_slope(dem, out, prefer_whitebox=False)
        with rasterio.open(out) as src:
            slope = src.read(1)
            nodata = src.nodata
        # The 3x3 block around the hole must be nodata; cells beyond it valid.
        assert np.all(slope[13:16, 13:16] == nodata)
        assert slope[10, 10] != nodata

    def test_geographic_crs_rejected(self, tmp_path):
        dem = write_dem(tmp_path / "geo.tif", np.full((30, 30), 450.0), resolution=0.0003, crs="EPSG:4326")
        with pytest.raises(terrain.TerrainProcessingError, match="projected"):
            terrain._numpy_horn_slope(dem, str(tmp_path / "slope.tif"))


class TestWhiteboxAgreement:
    def test_whitebox_matches_numpy_when_available(self, tilted_plane_dem, tmp_path):
        wbt_out = str(tmp_path / "slope_wbt.tif")
        engine = terrain.compute_slope(tilted_plane_dem, wbt_out, prefer_whitebox=True)
        if engine == "numpy-horn":
            pytest.skip("WhiteboxTools not installed in this environment")
        np_out = str(tmp_path / "slope_np.tif")
        terrain.compute_slope(tilted_plane_dem, np_out, prefer_whitebox=False)
        with rasterio.open(wbt_out) as a, rasterio.open(np_out) as b:
            wbt_slope = a.read(1)[5:-5, 5:-5]
            np_slope = b.read(1)[5:-5, 5:-5]
        assert wbt_slope == pytest.approx(np_slope, abs=0.05)


class TestHillshade:
    def test_output_range(self, tilted_plane_dem, tmp_path):
        out = str(tmp_path / "shade.tif")
        terrain.compute_hillshade(tilted_plane_dem, out)
        with rasterio.open(out) as src:
            shade = src.read(1)
        assert shade.dtype == np.uint8
        assert shade.max() <= 255
