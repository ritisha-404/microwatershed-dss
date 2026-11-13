"""Exact band statistics for the QGIS layers, and GDAL PAM sidecar refresh.

QGIS decides a raster's default stretch from the statistics GDAL reports. When
those statistics are missing, GDAL estimates them from an overview or a
subsample, and when they are stale (written by an earlier tool against an
earlier version of the file) QGIS stretches the display against numbers that
no longer describe the data. Both produce a map that looks wrong without any
visible error.

This module therefore computes min/max/mean/standard deviation and the
percentiles used by the stretches *exactly*, over the valid (non-nodata) cells
only, and writes them into the GeoTIFF's ``.aux.xml`` PAM sidecar in the keys
GDAL itself uses. Nothing in the raster is touched: the sidecar is metadata
that GDAL and QGIS read, and any entries written by other tools are preserved.

The rasters produced by this project are small (a few megapixels at most), so
the statistics are computed over the whole band rather than a sample.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio

# Percentiles used by the cumulative-count-cut stretches. Keeping them here
# means the numbers baked into the styles and the numbers written to the PAM
# sidecar always come from the same computation.
CUT_LOWER_PERCENT = 2.0
CUT_UPPER_PERCENT = 98.0

_STAT_KEYS = (
    "STATISTICS_MINIMUM",
    "STATISTICS_MAXIMUM",
    "STATISTICS_MEAN",
    "STATISTICS_STDDEV",
    "STATISTICS_VALID_PERCENT",
    "STATISTICS_APPROXIMATE",
)


@dataclass
class BandStats:
    """Exact statistics of one raster band over its valid cells."""

    minimum: float
    maximum: float
    mean: float
    stddev: float
    valid_cells: int
    total_cells: int
    cut_lower: float  # value at CUT_LOWER_PERCENT
    cut_upper: float  # value at CUT_UPPER_PERCENT

    @property
    def valid_percent(self) -> float:
        return 100.0 * self.valid_cells / self.total_cells if self.total_cells else 0.0

    @property
    def has_nodata(self) -> bool:
        return self.valid_cells < self.total_cells

    def stretch_range(self) -> tuple[float, float]:
        """The display range: the 2-98% cut, widened if it would collapse.

        A cut can degenerate (constant raster, or a value so dominant that the
        2nd and 98th percentile coincide). A zero-width range makes QGIS paint
        everything in one colour, so fall back to the full range, and finally
        to a symmetric unit interval around the value.
        """
        lo, hi = float(self.cut_lower), float(self.cut_upper)
        if hi > lo:
            return lo, hi
        lo, hi = float(self.minimum), float(self.maximum)
        if hi > lo:
            return lo, hi
        return lo - 0.5, hi + 0.5


def band_statistics(path: str | Path, band: int = 1) -> BandStats:
    """Compute exact statistics for ``band`` of ``path`` over valid cells.

    Cells equal to the band's nodata value, and non-finite cells, are excluded
    the same way the analysis code excludes them.
    """
    with rasterio.open(path) as src:
        values = src.read(band).astype("float64")
        nodata = src.nodatavals[band - 1]

    valid = np.isfinite(values)
    if nodata is not None and np.isfinite(nodata):
        valid &= values != nodata
    data = values[valid]
    total = int(values.size)
    if data.size == 0:
        raise ValueError(f"{path} band {band} contains no valid data to summarise.")

    lo, hi = np.percentile(data, [CUT_LOWER_PERCENT, CUT_UPPER_PERCENT])
    return BandStats(
        minimum=float(data.min()),
        maximum=float(data.max()),
        mean=float(data.mean()),
        stddev=float(data.std()),
        valid_cells=int(data.size),
        total_cells=total,
        cut_lower=float(lo),
        cut_upper=float(hi),
    )


def write_pam_statistics(path: str | Path, stats: BandStats, band: int = 1) -> str:
    """Write ``stats`` into the GDAL PAM sidecar (``<path>.aux.xml``).

    Existing sidecar content is preserved; only the ``STATISTICS_*`` metadata
    items of the given band are replaced, and the band is marked as having
    exact (not approximate) statistics. Returns the sidecar path.
    """
    aux_path = Path(f"{path}.aux.xml")
    if aux_path.exists():
        try:
            tree = ET.parse(aux_path)
            root = tree.getroot()
        except ET.ParseError:  # a corrupt sidecar is replaced rather than kept
            root = ET.Element("PAMDataset")
    else:
        root = ET.Element("PAMDataset")
    if root.tag != "PAMDataset":
        root = ET.Element("PAMDataset")

    pam_band = None
    for candidate in root.findall("PAMRasterBand"):
        if candidate.get("band") == str(band):
            pam_band = candidate
            break
    if pam_band is None:
        pam_band = ET.SubElement(root, "PAMRasterBand", {"band": str(band)})

    metadata = pam_band.find("Metadata")
    if metadata is None:
        metadata = ET.SubElement(pam_band, "Metadata")
    for item in [mdi for mdi in metadata.findall("MDI") if mdi.get("key") in _STAT_KEYS]:
        metadata.remove(item)

    for key, value in (
        ("STATISTICS_APPROXIMATE", None),  # dropped: these statistics are exact
        ("STATISTICS_MAXIMUM", _g(stats.maximum)),
        ("STATISTICS_MEAN", _g(stats.mean)),
        ("STATISTICS_MINIMUM", _g(stats.minimum)),
        ("STATISTICS_STDDEV", _g(stats.stddev)),
        ("STATISTICS_VALID_PERCENT", _g(stats.valid_percent)),
    ):
        if value is None:
            continue
        ET.SubElement(metadata, "MDI", {"key": key}).text = value

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(aux_path, encoding="utf-8", xml_declaration=False)
    with open(aux_path, "a", encoding="utf-8") as handle:
        handle.write("\n")
    return str(aux_path)


def _g(value: float) -> str:
    """Format a statistic the way GDAL does: plain decimal, no exponent."""
    return f"{value:.14g}"
