import math

import numpy as np
import pytest

from microwatershed.data import acquisition


class TestGeographicBbox:
    def test_symmetric_around_point(self):
        west, south, east, north = acquisition.geographic_bbox(17.68, 74.02, 2.0)
        assert west < 74.02 < east
        assert south < 17.68 < north
        # 2 km in latitude is ~0.0181 degrees.
        assert north - 17.68 == pytest.approx(2.0 / 110.574, rel=1e-6)

    def test_longitude_span_grows_with_latitude(self):
        _, _, east_eq, _ = acquisition.geographic_bbox(0.0, 0.0, 2.0)
        _, _, east_60, _ = acquisition.geographic_bbox(60.0, 0.0, 2.0)
        assert (east_60 - 0.0) > (east_eq - 0.0)


class TestChooseZoom:
    def test_tropical_latitude_matches_srtm(self):
        # ~30 m target: zoom 12 pixels are ~36 m at 17.68N, zoom 13 ~18 m.
        assert acquisition.choose_zoom(17.68) == 12

    def test_clamped(self):
        assert acquisition.MIN_ZOOM <= acquisition.choose_zoom(84.0) <= acquisition.MAX_ZOOM

    def test_resolution_near_target(self):
        for lat in (0.0, 30.0, 55.0):
            zoom = acquisition.choose_zoom(lat)
            res = 156543.03392 * math.cos(math.radians(lat)) / 2**zoom
            assert 15.0 <= res <= 60.0


class TestDeg2Tile:
    def test_known_tile(self):
        # Greenwich equator at zoom 1 is tile (1, 1)'s corner; (0,0) maps to (1,1)... center of map
        x, y = acquisition.deg2tile(0.0, 0.0, 1)
        assert (x, y) == (1, 1)

    def test_satara_region_zoom12(self):
        x, y = acquisition.deg2tile(17.68, 74.02, 12)
        n = 2**12
        assert 0 <= x < n and 0 <= y < n
        # Longitude 74.02E is just past 70.5% of the world width.
        assert x == int((74.02 + 180) / 360 * n)


class TestDecodeTerrarium:
    def test_zero_elevation(self):
        rgb = np.zeros((3, 2, 2), dtype="uint8")
        rgb[0] = 128  # R=128 -> 128*256 - 32768 = 0
        assert np.allclose(acquisition.decode_terrarium(rgb), 0.0)

    def test_known_value(self):
        # 650.25 m -> value+32768 = 33418.25 -> R=130, G=138, B=64
        rgb = np.array([[[130]], [[138]], [[64]]], dtype="uint8")
        assert acquisition.decode_terrarium(rgb)[0, 0] == pytest.approx(650.25)

    def test_negative_elevation(self):
        rgb = np.array([[[127]], [[246]], [[0]]], dtype="uint8")
        assert acquisition.decode_terrarium(rgb)[0, 0] == pytest.approx(-10.0)


class TestUtmCrs:
    def test_northern_hemisphere(self):
        assert acquisition.utm_crs(17.68, 74.02).to_epsg() == 32643

    def test_southern_hemisphere(self):
        assert acquisition.utm_crs(-33.9, 18.4).to_epsg() == 32734
