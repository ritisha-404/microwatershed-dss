import numpy as np
import pytest

from conftest import write_dem
from microwatershed.data import validation


class TestValidatePoint:
    def test_valid_point(self):
        validation.validate_point(17.68, 74.02)

    @pytest.mark.parametrize("lat,lon", [(91, 0), (-91, 0), (0, 181), (0, -181), (float("nan"), 0)])
    def test_out_of_range(self, lat, lon):
        with pytest.raises(validation.ValidationError):
            validation.validate_point(lat, lon)

    def test_polar_latitude_rejected(self):
        with pytest.raises(validation.ValidationError, match="coverage"):
            validation.validate_point(88.0, 10.0)


class TestValidateRadius:
    def test_valid(self):
        validation.validate_radius(2.0)

    @pytest.mark.parametrize("radius", [0.0, -1.0, 100.0, float("inf")])
    def test_invalid(self, radius):
        with pytest.raises(validation.ValidationError):
            validation.validate_radius(radius)


class TestValidateDem:
    def test_good_dem(self, tmp_path):
        data = np.random.default_rng(1).uniform(600, 700, (40, 40))
        path = write_dem(tmp_path / "ok.tif", data)
        result = validation.validate_dem(path)
        assert result.valid_fraction == 1.0
        assert 600 <= result.min_elevation <= result.max_elevation <= 700
        assert result.resolution_m == 30.0
        assert not result.warnings

    def test_missing_file(self, tmp_path):
        with pytest.raises(validation.ValidationError, match="Cannot open"):
            validation.validate_dem(str(tmp_path / "nope.tif"))

    def test_all_nodata(self, tmp_path):
        data = np.full((40, 40), -32768.0)
        path = write_dem(tmp_path / "void.tif", data)
        with pytest.raises(validation.ValidationError, match="no valid elevation"):
            validation.validate_dem(path)

    def test_low_coverage_rejected(self, tmp_path):
        data = np.full((40, 40), -32768.0)
        data[:10, :10] = 500.0  # only ~6% valid
        path = write_dem(tmp_path / "sparse.tif", data)
        with pytest.raises(validation.ValidationError, match="covers only"):
            validation.validate_dem(path)

    def test_partial_coverage_warns(self, tmp_path):
        data = np.full((40, 40), 500.0)
        data[:8, :] = -32768.0  # 20% nodata
        path = write_dem(tmp_path / "partial.tif", data)
        result = validation.validate_dem(path)
        assert result.valid_fraction == pytest.approx(0.8)
        assert any("no elevation data" in w for w in result.warnings)

    def test_ocean_rejected(self, tmp_path):
        data = np.full((40, 40), -4000.0)
        path = write_dem(tmp_path / "ocean.tif", data)
        with pytest.raises(validation.ValidationError, match="plausible"):
            validation.validate_dem(path)

    def test_too_small(self, tmp_path):
        data = np.full((5, 5), 500.0)
        path = write_dem(tmp_path / "tiny.tif", data)
        with pytest.raises(validation.ValidationError, match="too small"):
            validation.validate_dem(path)
