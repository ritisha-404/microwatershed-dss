"""Small XML helpers shared by the style and project builders.

QGIS project and style files are plain XML, so they are built with the
standard library rather than a dependency; these helpers keep the builders
readable and make sure every file is written the same way.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

DOCTYPE = b"<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"


def el(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    """Append ``<tag attrs...>text</tag>`` to ``parent`` and return it."""
    element = ET.SubElement(parent, tag, dict(attrs))
    if text is not None:
        element.text = text
    return element


def num(value: float, decimals: int = 10) -> str:
    """Format a number for QGIS: fixed notation, no trailing zeros.

    QGIS parses these with ``QString::toDouble``, which does accept scientific
    notation, but extents and classification bounds are far easier to check by
    eye in plain decimal.
    """
    formatted = f"{float(value):.{decimals}f}".rstrip("0").rstrip(".")
    return formatted or "0"


def write_document(root: ET.Element, path: str | Path) -> str:
    """Write ``root`` as an indented QGIS XML document. Returns the path."""
    path = Path(path)
    ET.indent(root, space="  ")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(DOCTYPE)
        ET.ElementTree(root).write(handle, encoding="utf-8", xml_declaration=False)
        handle.write(b"\n")
    return str(path)
