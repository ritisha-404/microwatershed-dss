"""End-to-end tests.

The network-marked test exercises the full acceptance path, including the
real tile download. Run with:  pytest -m network
The offline test exercises the same pipeline through main() with a local DEM.
"""

import json

import numpy as np
import pytest

from conftest import write_dem
from microwatershed import main as cli


def _check_outputs(out_dir):
    report_path = out_dir / "terrain_assessment.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text())
    names = {m["name"] for m in report["metrics"]}
    assert {"min_elevation", "max_elevation", "mean_elevation", "elevation_range",
            "mean_slope", "slope_classification"} <= names
    for metric in report["metrics"]:
        assert metric["kind"] in ("measured", "derived")
        assert metric["methodology"]
    assert (out_dir / "terrain_assessment.html").exists()
    assert (out_dir / "dem.tif").exists()
    assert (out_dir / "slope.tif").exists()
    return report


class TestOfflinePipeline:
    def test_user_dem_end_to_end(self, tmp_path):
        rng = np.random.default_rng(7)
        z = np.cumsum(rng.normal(0, 1.0, (80, 80)), axis=1) + 600.0
        dem = write_dem(tmp_path / "input.tif", z)
        out_dir = tmp_path / "out"
        code = cli.main(["--dem", dem, "--output-dir", str(out_dir), "--no-whitebox"])
        assert code == 0
        report = _check_outputs(out_dir)
        assert report["request"]["latitude"] is None
        assert "User-supplied" in report["data_source"]["description"]

    def test_invalid_coordinates_fail_cleanly(self, tmp_path):
        code = cli.main(["--lat", "95", "--lon", "74", "--output-dir", str(tmp_path / "out")])
        assert code == 1

    def test_missing_arguments(self, tmp_path):
        code = cli.main(["--output-dir", str(tmp_path / "out")])
        assert code == 2

    def test_invalid_radius(self, tmp_path):
        code = cli.main(["--lat", "17.68", "--lon", "74.02", "--radius", "500",
                         "--output-dir", str(tmp_path / "out")])
        assert code == 1


@pytest.mark.network
class TestAcceptance:
    def test_acceptance_command(self, tmp_path):
        """python -m microwatershed.main --lat 17.68 --lon 74.02 --radius 2"""
        out_dir = tmp_path / "out"
        code = cli.main(["--lat", "17.68", "--lon", "74.02", "--radius", "2",
                         "--output-dir", str(out_dir)])
        assert code == 0
        report = _check_outputs(out_dir)
        by_name = {m["name"]: m for m in report["metrics"]}
        # The Deccan plateau around Satara sits between ~550 and ~1500 m.
        assert 400 < by_name["min_elevation"]["value"] < 1600
        assert by_name["elevation_range"]["value"] > 0
        assert 0 <= by_name["mean_slope"]["value"] < 45
        classification = by_name["slope_classification"]["value"]
        # ~12.6 km2 circle; allow for raster edge effects.
        assert classification["total_assessed_area_km2"] == pytest.approx(np.pi * 4.0, rel=0.1)
