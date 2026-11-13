"""End-to-end tests for the watershed delineation CLI.

Offline tests run against synthetic DEMs, including a mocked tile-fetcher
that serves windows of a master DEM so the automatic coverage-expansion loop
is exercised without network access. The network-marked test runs the real
acceptance command against downloaded elevation data.
"""

import json

import numpy as np
import pytest
import rasterio

from conftest import DIAMOND_CATCHMENT_CELLS, DIAMOND_OUTLET, cell_latlon, diamond_valley, write_dem
from microwatershed import delineate as cli
from microwatershed.data import acquisition

RES = 30.0


def _check_outputs(out_dir, expect_cells=None):
    report = json.loads((out_dir / "watershed_assessment.json").read_text())
    names = {m["name"] for m in report["metrics"]}
    assert {"catchment_area_ha", "catchment_area_km2", "min_elevation", "max_elevation",
            "mean_elevation", "elevation_range", "mean_slope", "slope_classification"} <= names
    for metric in report["metrics"]:
        assert metric["kind"] in ("measured", "derived")
        assert metric["methodology"]
    for name in ("watershed_assessment.html", "catchment.geojson", "catchment.tif",
                 "flow_accumulation.tif", "flow_direction.tif", "dem_conditioned.tif",
                 "dem.tif", "slope.tif", "hillshade.tif", "watershed.qgs", "dem.qml"):
        assert (out_dir / name).exists(), name
    assert report["outputs"]["qgis_project"].endswith("watershed.qgs")
    deli = report["delineation"]
    assert deli["boundary"]["touches_data_boundary"] is False
    assert deli["validation_checks"]["mass_balance"]["passed"] is True
    assert deli["snapped_outlet"]["latitude"] is not None
    if expect_cells is not None:
        assert deli["catchment"]["cell_count"] == expect_cells
        by_name = {m["name"]: m for m in report["metrics"]}
        assert by_name["catchment_area_ha"]["value"] == pytest.approx(
            expect_cells * RES * RES / 1e4, rel=1e-6
        )
        assert by_name["catchment_area_km2"]["value"] == pytest.approx(
            expect_cells * RES * RES / 1e6, rel=1e-4
        )
    return report


class TestUserDemPipeline:
    def test_end_to_end_known_catchment(self, tmp_path):
        dem = write_dem(tmp_path / "valley.tif", diamond_valley(), resolution=RES)
        lat, lon = cell_latlon(dem, *DIAMOND_OUTLET)
        out = tmp_path / "out"
        code = cli.main(["--dem", dem, "--lat", f"{lat}", "--lon", f"{lon}",
                         "--output-dir", str(out), "--no-whitebox"])
        assert code == 0
        report = _check_outputs(out, expect_cells=DIAMOND_CATCHMENT_CELLS)
        assert report["processing"]["hydrology_engine"] == "numpy"
        geo = json.loads((out / "catchment.geojson").read_text())
        assert geo["features"][0]["properties"]["cell_count"] == DIAMOND_CATCHMENT_CELLS

    def test_catchment_reaching_dem_edge_fails(self, tmp_path):
        # A plane dipping east: every catchment stretches to the west edge.
        cols = np.arange(60, 0.0, -1.0) * 2.0
        dem = write_dem(tmp_path / "plane.tif", np.tile(cols, (40, 1)), resolution=RES)
        lat, lon = cell_latlon(dem, 20, 40)
        code = cli.main(["--dem", dem, "--lat", f"{lat}", "--lon", f"{lon}",
                         "--output-dir", str(tmp_path / "out"), "--no-whitebox",
                         "--stream-threshold", "5"])
        assert code == 1

    def test_invalid_coordinates(self, tmp_path):
        code = cli.main(["--lat", "95", "--lon", "74", "--output-dir", str(tmp_path / "out")])
        assert code == 1

    def test_missing_arguments(self):
        with pytest.raises(SystemExit) as excinfo:
            cli.main([])
        assert excinfo.value.code == 2

    def test_invalid_snap_distance(self, tmp_path):
        dem = write_dem(tmp_path / "valley.tif", diamond_valley(), resolution=RES)
        lat, lon = cell_latlon(dem, *DIAMOND_OUTLET)
        code = cli.main(["--dem", dem, "--lat", f"{lat}", "--lon", f"{lon}",
                         "--snap-distance", "99999",
                         "--output-dir", str(tmp_path / "out"), "--no-whitebox"])
        assert code == 1


class TestAutomaticExpansion:
    @pytest.fixture
    def mock_tiles(self, tmp_path, monkeypatch):
        """Replace the tile fetcher with windows cropped from a master DEM."""
        master_path = write_dem(tmp_path / "master.tif", diamond_valley(), resolution=RES)
        fetch_radii = []

        def fake_fetch(lat, lon, radius_km, out_path):
            fetch_radii.append(radius_km)
            with rasterio.open(master_path) as src:
                from rasterio.warp import transform as warp_transform

                xs, ys = warp_transform("EPSG:4326", src.crs, [lon], [lat])
                row, col = src.index(xs[0], ys[0])
                half = int(round(radius_km * 1000.0 / RES))
                r0, r1 = max(row - half, 0), min(row + half + 1, src.height)
                c0, c1 = max(col - half, 0), min(col + half + 1, src.width)
                window = rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0)
                data = src.read(1, window=window)
                profile = src.profile.copy()
                profile.update(width=c1 - c0, height=r1 - r0,
                               transform=src.window_transform(window))
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(data, 1)
            return acquisition.AcquiredDem(
                path=out_path, source="mock tiles", zoom=12, n_tiles=1,
                n_missing_tiles=0, native_resolution_m=RES, crs=str(profile["crs"]),
            )

        monkeypatch.setattr(acquisition, "fetch_dem", fake_fetch)
        return master_path, fetch_radii

    def test_expands_until_catchment_fits(self, tmp_path, mock_tiles):
        master_path, fetch_radii = mock_tiles
        lat, lon = cell_latlon(master_path, *DIAMOND_OUTLET)
        out = tmp_path / "out"
        code = cli.main(["--lat", f"{lat}", "--lon", f"{lon}", "--radius", "0.3",
                         "--max-radius", "5", "--output-dir", str(out), "--no-whitebox"])
        assert code == 0
        assert len(fetch_radii) >= 2  # the initial window truncated the catchment
        assert fetch_radii == sorted(fetch_radii)
        report = _check_outputs(out, expect_cells=DIAMOND_CATCHMENT_CELLS)
        history = report["delineation"]["dem_expansion_history"]
        assert len(history) == len(fetch_radii)
        assert history[0]["touches_boundary"] is True
        assert history[-1]["touches_boundary"] is False

    def test_reports_failure_when_max_radius_insufficient(self, tmp_path, mock_tiles):
        master_path, fetch_radii = mock_tiles
        lat, lon = cell_latlon(master_path, *DIAMOND_OUTLET)
        out = tmp_path / "out"
        code = cli.main(["--lat", f"{lat}", "--lon", f"{lon}", "--radius", "0.3",
                         "--max-radius", "0.6", "--output-dir", str(out),
                         "--no-whitebox"])
        assert code == 1  # truncated catchment must be reported, not returned
        # No files that look like a valid delineation may be left behind.
        for name in ("catchment.geojson", "catchment.tif", "flow_accumulation.tif",
                     "flow_direction.tif", "watershed_assessment.json"):
            assert not (out / name).exists(), name


@pytest.mark.network
class TestAcceptance:
    def test_real_delineation(self, tmp_path):
        """python -m microwatershed.delineate --lat 17.68 --lon 74.02"""
        out = tmp_path / "out"
        code = cli.main(["--lat", "17.68", "--lon", "74.02", "--output-dir", str(out)])
        assert code == 0
        report = _check_outputs(out)
        deli = report["delineation"]
        assert deli["catchment"]["area_km2"] > 0.001
        assert deli["snapped_outlet"]["distance_moved_m"] <= 150.0
        by_name = {m["name"]: m for m in report["metrics"]}
        # Deccan plateau elevations around Satara.
        assert 400 < by_name["min_elevation"]["value"] < 1600
