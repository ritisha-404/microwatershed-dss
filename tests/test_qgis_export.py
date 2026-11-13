"""Tests for the QGIS project export.

The export is display-only, so the tests check two things above all: that the
project QGIS will open describes the layers correctly (order, CRS, extent,
nodata transparency, stretches), and that building it leaves every analysis
output byte-for-byte unchanged.
"""

import hashlib
import json
import xml.etree.ElementTree as ET

import numpy as np
import pytest
import rasterio

from conftest import diamond_valley, write_dem
from microwatershed.qgis_export import QgisExportError, build_watershed_project
from microwatershed.qgis_export import stats as stats_module
from microwatershed.qgis_export import styles

RES = 30.0


def _accumulation_grid(shape):
    """Plausible accumulation: a few cells everywhere, a decade range in all."""
    rows, cols = shape
    grid = np.ones(shape, dtype="float64")
    grid[rows // 2, :] = np.linspace(1.0, 2000.0, cols)  # a trunk channel
    grid[:, cols // 2] = np.linspace(1.0, 50.0, rows)  # a tributary
    return grid


@pytest.fixture
def results_dir(tmp_path):
    """A directory shaped like a completed delineation run."""
    directory = tmp_path / "watershed"
    directory.mkdir()
    elevation = diamond_valley()
    elevation[0, 0] = -32768.0  # one nodata cell, as a reprojected DEM has
    write_dem(directory / "dem.tif", elevation, resolution=RES)
    write_dem(directory / "flow_accumulation.tif", _accumulation_grid(elevation.shape),
              resolution=RES, nodata=-9999.0)
    (directory / "catchment.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature",
             "geometry": {"type": "Polygon", "coordinates": [[
                 [74.0, 17.6], [74.01, 17.6], [74.01, 17.61], [74.0, 17.61], [74.0, 17.6]]]},
             "properties": {"name": "catchment", "area_ha": 154.08, "area_km2": 1.5408,
                            "cell_count": 1712, "cell_size_m": RES}},
            {"type": "Feature",
             "geometry": {"type": "Point", "coordinates": [74.005, 17.605]},
             "properties": {"name": "outlet_snapped", "distance_moved_m": 42.0}},
            {"type": "Feature",
             "geometry": {"type": "Point", "coordinates": [74.004, 17.604]},
             "properties": {"name": "outlet_requested"}},
        ],
    }))
    return directory


def _parse(path):
    return ET.parse(path).getroot()


def _layer_names(root):
    """Layer names in legend order, top first."""
    return [node.get("name") for node in root.findall("./layer-tree-group/layer-tree-layer")]


def _maplayer(root, name_prefix):
    for layer in root.findall("./projectlayers/maplayer"):
        if layer.findtext("layername", "").startswith(name_prefix):
            return layer
    raise AssertionError(f"no layer named {name_prefix!r}")


class TestBandStatistics:
    def test_excludes_nodata(self, tmp_path):
        data = np.full((10, 10), 5.0)
        data[0, :] = -9999.0
        path = write_dem(tmp_path / "r.tif", data, nodata=-9999.0)
        stats = stats_module.band_statistics(path)
        assert stats.minimum == stats.maximum == 5.0
        assert stats.valid_cells == 90
        assert stats.total_cells == 100
        assert stats.valid_percent == pytest.approx(90.0)
        assert stats.has_nodata

    def test_matches_numpy_over_valid_cells(self, tmp_path):
        rng = np.random.default_rng(7)
        data = rng.normal(700.0, 40.0, size=(60, 60))
        path = write_dem(tmp_path / "r.tif", data)
        stats = stats_module.band_statistics(path)
        reference = np.asarray(data, dtype="float32").astype("float64")
        assert stats.mean == pytest.approx(reference.mean(), rel=1e-9)
        assert stats.stddev == pytest.approx(reference.std(), rel=1e-9)
        assert stats.cut_lower == pytest.approx(np.percentile(reference, 2), rel=1e-9)
        assert stats.cut_upper == pytest.approx(np.percentile(reference, 98), rel=1e-9)

    def test_stretch_range_falls_back_when_cut_collapses(self, tmp_path):
        data = np.full((20, 20), 3.0)
        data[0, 0] = 9.0  # too rare to move the 98th percentile
        path = write_dem(tmp_path / "r.tif", data)
        stats = stats_module.band_statistics(path)
        assert stats.cut_lower == stats.cut_upper
        assert stats.stretch_range() == (3.0, 9.0)

    def test_stretch_range_never_zero_width(self, tmp_path):
        path = write_dem(tmp_path / "r.tif", np.full((8, 8), 2.0))
        low, high = stats_module.band_statistics(path).stretch_range()
        assert high > low

    def test_empty_band_is_an_error(self, tmp_path):
        path = write_dem(tmp_path / "r.tif", np.full((5, 5), -32768.0))
        with pytest.raises(ValueError):
            stats_module.band_statistics(path)


class TestPamSidecar:
    def test_writes_gdal_statistics_keys(self, tmp_path):
        path = write_dem(tmp_path / "r.tif", np.arange(100.0).reshape(10, 10))
        stats = stats_module.band_statistics(path)
        aux = stats_module.write_pam_statistics(path, stats)
        items = {mdi.get("key"): mdi.text
                 for mdi in _parse(aux).iter("MDI")}
        assert items["STATISTICS_MINIMUM"] == "0"
        assert items["STATISTICS_MAXIMUM"] == "99"
        assert float(items["STATISTICS_MEAN"]) == pytest.approx(49.5)
        assert float(items["STATISTICS_VALID_PERCENT"]) == pytest.approx(100.0)
        assert "STATISTICS_APPROXIMATE" not in items

    def test_replaces_stale_values_and_keeps_other_metadata(self, tmp_path):
        path = write_dem(tmp_path / "r.tif", np.arange(100.0).reshape(10, 10))
        aux_path = tmp_path / "r.tif.aux.xml"
        aux_path.write_text(
            "<PAMDataset><PAMRasterBand band=\"1\"><Metadata>"
            "<MDI key=\"STATISTICS_MAXIMUM\">12345</MDI>"
            "<MDI key=\"SOMETHING_ELSE\">keep me</MDI>"
            "</Metadata></PAMRasterBand></PAMDataset>"
        )
        stats_module.write_pam_statistics(path, stats_module.band_statistics(path))
        items = {mdi.get("key"): mdi.text for mdi in _parse(aux_path).iter("MDI")}
        assert items["STATISTICS_MAXIMUM"] == "99"
        assert items["SOMETHING_ELSE"] == "keep me"
        assert len([k for k in items if k == "STATISTICS_MAXIMUM"]) == 1

    def test_gdal_reads_back_the_statistics(self, tmp_path):
        path = write_dem(tmp_path / "r.tif", np.arange(100.0).reshape(10, 10))
        stats_module.write_pam_statistics(path, stats_module.band_statistics(path))
        with rasterio.open(path) as src:
            tags = src.tags(1)
        assert float(tags["STATISTICS_MINIMUM"]) == 0.0
        assert float(tags["STATISTICS_MAXIMUM"]) == 99.0


class TestColourRamps:
    def test_elevation_stops_span_the_stretched_range(self):
        stops = styles.elevation_stops(640.0, 860.0)
        assert stops[0][0] == 640.0
        assert stops[-1][0] == 860.0
        assert [value for value, _, _ in stops] == sorted(value for value, _, _ in stops)
        assert all(colour[3] == 255 for _, colour, _ in stops)

    def test_accumulation_stops_are_log_spaced(self):
        stops = styles.accumulation_stops(1.0, 3264.0)
        values = [value for value, _, _ in stops]
        assert values[0] == 1.0
        assert values[-1] == 3264.0
        assert values == sorted(values)
        assert {2.0, 10.0, 100.0, 1000.0} <= set(values)
        # Equal ratios, not equal differences: that is what "log scale" means.
        assert values[values.index(10.0)] / values[values.index(2.0)] == 5.0

    def test_low_accumulation_is_transparent_and_rises(self):
        stops = styles.accumulation_stops(1.0, 3264.0)
        alphas = [colour[3] for _, colour, _ in stops]
        assert alphas[0] == 0  # a single upstream cell is invisible
        assert alphas[2] < 128  # hillslopes stay subdued
        assert alphas[-1] == 255  # the trunk channel is opaque
        assert alphas == sorted(alphas)

    def test_accumulation_stops_handle_a_tiny_range(self):
        stops = styles.accumulation_stops(1.0, 4.0)
        values = [value for value, _, _ in stops]
        assert values[0] == 1.0
        assert values[-1] > values[0]
        assert values == sorted(values)

    def test_sample_ramp_hits_both_ends(self):
        assert styles.sample_ramp(styles.TERRAIN_RAMP, 0.0) == styles.TERRAIN_RAMP[0][1]
        assert styles.sample_ramp(styles.TERRAIN_RAMP, 1.0) == styles.TERRAIN_RAMP[-1][1]
        assert styles.sample_ramp(styles.TERRAIN_RAMP, 2.0) == styles.TERRAIN_RAMP[-1][1]


class TestProject:
    def test_writes_project_styles_and_hillshade(self, results_dir):
        outputs = build_watershed_project(results_dir)
        assert (results_dir / "watershed.qgs").exists()
        assert (results_dir / "hillshade.tif").exists()
        for name in ("dem.qml", "flow_accumulation.qml", "hillshade.qml",
                     "catchment_boundary.qml", "outlets.qml"):
            assert (results_dir / name).exists(), name
        assert outputs["qgis_project"].endswith("watershed.qgs")

    def test_layer_order_and_names(self, results_dir):
        build_watershed_project(results_dir)
        names = _layer_names(_parse(results_dir / "watershed.qgs"))
        assert len(names) == 5
        assert names[0].startswith("Outlets")
        assert names[1].startswith("Catchment boundary")
        assert names[2].startswith("Flow accumulation")
        assert names[3].startswith("Elevation")
        assert names[4] == "Hillshade"  # beneath everything else

    def test_catchment_layer_name_carries_the_area(self, results_dir):
        build_watershed_project(results_dir)
        names = _layer_names(_parse(results_dir / "watershed.qgs"))
        assert names[1] == "Catchment boundary (154.1 ha)"

    def test_every_layer_is_visible_and_in_drawing_order(self, results_dir):
        build_watershed_project(results_dir)
        root = _parse(results_dir / "watershed.qgs")
        nodes = root.findall("./layer-tree-group/layer-tree-layer")
        assert all(node.get("checked") == "Qt::Checked" for node in nodes)
        # <layerorder> paints bottom-up, the reverse of the legend.
        order = [layer.get("id") for layer in root.findall("./layerorder/layer")]
        assert order == [node.get("id") for node in reversed(nodes)]

    def test_project_crs_and_extent_match_the_dem(self, results_dir):
        build_watershed_project(results_dir)
        root = _parse(results_dir / "watershed.qgs")
        assert root.findtext("./projectCrs/spatialrefsys/authid") == "EPSG:32643"
        with rasterio.open(results_dir / "dem.tif") as src:
            bounds = src.bounds
        extent = root.find("./mapcanvas/extent")
        assert float(extent.findtext("xmin")) < bounds.left
        assert float(extent.findtext("ymin")) < bounds.bottom
        assert float(extent.findtext("xmax")) > bounds.right
        assert float(extent.findtext("ymax")) > bounds.top
        view = root.find("./ProjectViewSettings/DefaultViewExtent")
        assert float(view.get("xmin")) == pytest.approx(float(extent.findtext("xmin")))
        assert view.find("spatialrefsys/authid").text == "EPSG:32643"

    def test_raster_nodata_is_transparent(self, results_dir):
        build_watershed_project(results_dir)
        root = _parse(results_dir / "watershed.qgs")
        for prefix in ("Elevation", "Flow accumulation", "Hillshade"):
            no_data = _maplayer(root, prefix).find("./noData/noDataList")
            assert no_data.get("useSrcNoData") == "1"
            assert no_data.get("bandNo") == "1"

    def test_elevation_uses_the_percentile_cut_not_the_extremes(self, results_dir):
        build_watershed_project(results_dir)
        shader = _maplayer(_parse(results_dir / "watershed.qgs"), "Elevation").find(
            ".//colorrampshader")
        stats = stats_module.band_statistics(results_dir / "dem.tif")
        assert float(shader.get("minimumValue")) == pytest.approx(stats.cut_lower, rel=1e-6)
        assert float(shader.get("maximumValue")) == pytest.approx(stats.cut_upper, rel=1e-6)
        assert stats.minimum < float(shader.get("minimumValue"))
        assert shader.get("clip") == "0"  # outside the cut, clamp - never blank
        assert len(shader.findall("item")) == 9

    def test_elevation_is_translucent_over_the_hillshade(self, results_dir):
        build_watershed_project(results_dir)
        root = _parse(results_dir / "watershed.qgs")
        elevation = _maplayer(root, "Elevation").find(".//rasterrenderer")
        hillshade = _maplayer(root, "Hillshade").find(".//rasterrenderer")
        assert 0.0 < float(elevation.get("opacity")) < 1.0
        assert float(hillshade.get("opacity")) == 1.0
        assert hillshade.get("type") == "singlebandgray"

    def test_flow_accumulation_covers_the_whole_data_range(self, results_dir):
        build_watershed_project(results_dir)
        shader = _maplayer(_parse(results_dir / "watershed.qgs"), "Flow accum").find(
            ".//colorrampshader")
        stats = stats_module.band_statistics(results_dir / "flow_accumulation.tif")
        items = shader.findall("item")
        assert float(items[0].get("value")) == pytest.approx(stats.minimum)
        assert float(items[-1].get("value")) == pytest.approx(stats.maximum)
        assert items[0].get("alpha") == "0"
        assert items[-1].get("alpha") == "255"
        assert shader.get("colorRampType") == "INTERPOLATED"

    def test_catchment_polygon_is_unfilled_with_a_visible_outline(self, results_dir):
        build_watershed_project(results_dir)
        layer = _maplayer(_parse(results_dir / "watershed.qgs"), "Catchment boundary")
        symbol_layers = layer.findall(".//renderer-v2/symbols/symbol/layer")
        classes = [element.get("class") for element in symbol_layers]
        assert classes[0] == "SimpleFill"
        fill = {prop.get("k"): prop.get("v") for prop in symbol_layers[0].findall("prop")}
        assert fill["style"] == "no"  # transparent interior
        assert fill["color"].endswith(",0")
        outlines = [element for element in symbol_layers if element.get("class") == "SimpleLine"]
        assert len(outlines) == 2  # halo plus the divide itself
        widths = [float({p.get("k"): p.get("v") for p in element.findall("prop")}["line_width"])
                  for element in outlines]
        assert widths[0] > widths[1] > 0.0

    def test_outlets_are_distinct_symbols_and_colours(self, results_dir):
        build_watershed_project(results_dir)
        layer = _maplayer(_parse(results_dir / "watershed.qgs"), "Outlets")
        renderer = layer.find("./renderer-v2")
        assert renderer.get("type") == "categorizedSymbol"
        assert renderer.get("attr") == "name"
        categories = {element.get("value"): element.get("symbol")
                      for element in renderer.findall("./categories/category")}
        assert set(categories) == {"outlet_snapped", "outlet_requested"}
        marker = {}
        for symbol in renderer.findall("./symbols/symbol"):
            first = symbol.find("layer")
            props = {prop.get("k"): prop.get("v") for prop in first.findall("prop")}
            marker[symbol.get("name")] = (props["name"], props["color"])
        snapped = marker[categories["outlet_snapped"]]
        requested = marker[categories["outlet_requested"]]
        assert snapped[0] != requested[0]  # different shapes
        assert snapped[1] != requested[1]  # different colours
        assert layer.get("labelsEnabled") == "1"
        assert layer.find("./labeling/settings/text-style") is not None

    def test_sources_are_relative_and_split_by_geometry(self, results_dir):
        build_watershed_project(results_dir)
        root = _parse(results_dir / "watershed.qgs")
        assert root.findtext("./properties/Paths/Absolute") == "false"
        sources = [layer.findtext("datasource")
                   for layer in root.findall("./projectlayers/maplayer")]
        assert all(source.startswith("./") for source in sources), sources
        assert "./catchment.geojson|layername=catchment|geometrytype=Polygon" in sources
        assert "./catchment.geojson|layername=catchment|geometrytype=Point" in sources

    def test_leaves_the_analysis_outputs_untouched(self, results_dir):
        tracked = sorted(results_dir.glob("*.tif")) + [results_dir / "catchment.geojson"]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in tracked}
        build_watershed_project(results_dir)
        for path, digest in before.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, path

    def test_layer_ids_are_stable_across_rebuilds(self, results_dir):
        build_watershed_project(results_dir)
        first = [node.get("id") for node in
                 _parse(results_dir / "watershed.qgs").findall(
                     "./layer-tree-group/layer-tree-layer")]
        build_watershed_project(results_dir)
        second = [node.get("id") for node in
                  _parse(results_dir / "watershed.qgs").findall(
                      "./layer-tree-group/layer-tree-layer")]
        assert first == second

    def test_project_path_can_be_chosen(self, results_dir, tmp_path):
        target = tmp_path / "maps" / "catchment.qgs"
        build_watershed_project(results_dir, project_path=target)
        assert target.exists()
        # Sources stay resolvable from the project's own directory.
        sources = [layer.findtext("datasource")
                   for layer in _parse(target).findall("./projectlayers/maplayer")]
        assert all(not source.startswith("./") for source in sources)

    def test_missing_outputs_are_reported(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(QgisExportError, match="dem.tif"):
            build_watershed_project(tmp_path / "empty")

    def test_geojson_without_a_polygon_is_reported(self, results_dir):
        (results_dir / "catchment.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": []}))
        with pytest.raises(QgisExportError, match="no catchment polygon"):
            build_watershed_project(results_dir)

    def test_existing_hillshade_is_not_regenerated(self, results_dir):
        build_watershed_project(results_dir)
        hillshade = results_dir / "hillshade.tif"
        digest = hashlib.sha256(hillshade.read_bytes()).hexdigest()
        marker = hillshade.stat().st_mtime_ns
        build_watershed_project(results_dir)
        assert hashlib.sha256(hillshade.read_bytes()).hexdigest() == digest
        assert hillshade.stat().st_mtime_ns == marker
