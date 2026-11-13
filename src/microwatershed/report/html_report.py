"""Generate a self-contained HTML terrain assessment report.

Maps are rendered with matplotlib to base64 PNGs and embedded inline, so the
report is a single file with no external dependencies. Colors follow a
validated single-hue sequential scheme per data layer (blue for elevation,
orange for slope), with page chrome that adapts to light/dark mode. The PNG
maps are rendered on a fixed light surface and sit on light cards in both
modes.
"""

from __future__ import annotations

import base64
import html
import io
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap

from ..metrics.base import RasterData

# Single-hue sequential ramps (light -> dark) from the validated reference palette.
ELEVATION_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
SLOPE_RAMP = ["#fbe3d9", "#f5b193", "#eb6834", "#b94a1e", "#7c2f10"]
# Ordinal steps for the 6 slope classes (blue steps 250..700; light-mode-safe).
CLASS_COLORS = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281", "#0d366b"]

LIGHT_SURFACE = "#fcfcfb"
MUTED_INK = "#898781"


def _render_map(
    raster: RasterData,
    title: str,
    unit: str,
    ramp: list[str] | None,
    radius_km: float | None,
    grayscale: bool = False,
) -> str:
    """Render a raster to a base64 PNG map with a colorbar and AOI circle."""
    values = raster.values
    h, w = values.shape
    half_w_km = (w * raster.resolution_m) / 2000.0
    half_h_km = (h * raster.resolution_m) / 2000.0
    extent = (-half_w_km, half_w_km, -half_h_km, half_h_km)

    if grayscale:
        cmap = plt.get_cmap("gray")
    else:
        cmap = LinearSegmentedColormap.from_list("ramp", ramp)
    cmap = cmap.copy()
    cmap.set_bad(color="#e1e0d9")

    fig, ax = plt.subplots(figsize=(5.4, 5.0), dpi=120)
    fig.patch.set_facecolor(LIGHT_SURFACE)
    ax.set_facecolor(LIGHT_SURFACE)
    im = ax.imshow(np.ma.masked_invalid(values), cmap=cmap, extent=extent, interpolation="nearest")
    if radius_km is not None:
        circle = plt.Circle((0, 0), radius_km, fill=False, color="#0b0b0b", linewidth=1.0, linestyle=(0, (4, 3)))
        ax.add_patch(circle)
    ax.set_title(title, fontsize=11, color="#0b0b0b", pad=10)
    ax.set_xlabel("km (east-west from centre)", fontsize=8, color=MUTED_INK)
    ax.set_ylabel("km (north-south)", fontsize=8, color=MUTED_INK)
    ax.tick_params(colors=MUTED_INK, labelsize=7)
    for spine in ax.spines.values():
        spine.set_color("#c3c2b7")
        spine.set_linewidth(0.6)
    if not grayscale:
        cbar = fig.colorbar(im, ax=ax, shrink=0.75, pad=0.03)
        cbar.set_label(unit, fontsize=8, color=MUTED_INK)
        cbar.ax.tick_params(colors=MUTED_INK, labelsize=7)
        cbar.outline.set_edgecolor("#c3c2b7")
        cbar.outline.set_linewidth(0.6)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=LIGHT_SURFACE)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _metric_tiles(report: dict[str, Any]) -> str:
    tiles = []
    for metric in report["metrics"]:
        if metric["name"] == "slope_classification":
            continue
        label = metric["name"].replace("_", " ").title()
        kind = metric["kind"]
        tiles.append(
            f"""<div class="tile">
  <div class="tile-label">{html.escape(label)}</div>
  <div class="tile-value">{metric['value']:,.1f}<span class="tile-unit">{html.escape(metric['unit'])}</span></div>
  <div class="badge badge-{kind}" title="{html.escape(metric['description'])}">{kind}</div>
</div>"""
        )
    return "\n".join(tiles)


def _slope_class_chart(report: dict[str, Any]) -> str:
    classification = next((m for m in report["metrics"] if m["name"] == "slope_classification"), None)
    if classification is None:
        return ""
    classes = classification["value"]["classes"]
    max_pct = max((c["percent_of_area"] for c in classes), default=0.0) or 1.0

    bars, rows = [], []
    for color, cls in zip(CLASS_COLORS, classes):
        pct = cls["percent_of_area"]
        width = max(72.0 * pct / max_pct, 0.5)  # leave room for the inline value label
        rng = f"{cls['range_degrees'][0]:g}&ndash;{cls['range_degrees'][1]:g}&deg;"
        tooltip = f"{cls['label']}: {pct}% of area, {cls['area_km2']} km²"
        bars.append(
            f"""<div class="bar-row" title="{html.escape(tooltip)}">
  <div class="bar-name">{html.escape(cls['label'])}<span class="bar-range">{rng}</span></div>
  <div class="bar-track"><div class="bar-fill" style="width:{width:.1f}%;background:{color}"></div>
  <span class="bar-value">{pct:g}% &middot; {cls['area_km2']:g} km&sup2;</span></div>
</div>"""
        )
        rows.append(
            f"<tr><td>{html.escape(cls['label'])}</td><td>{rng}</td>"
            f"<td>{cls['area_km2']:g}</td><td>{pct:g}%</td></tr>"
        )

    total = classification["value"]["total_assessed_area_km2"]
    return f"""<section>
  <h2>Slope classification <span class="badge badge-derived">derived</span></h2>
  <p class="muted">Share of the assessed area ({total:g} km&sup2;) in each slope class. FAO-style class bounds in degrees.</p>
  <div class="bars">{''.join(bars)}</div>
  <details><summary>Data table</summary>
  <table><thead><tr><th>Class</th><th>Slope</th><th>Area (km&sup2;)</th><th>Share</th></tr></thead>
  <tbody>{''.join(rows)}</tbody></table></details>
</section>"""


STYLE = """
:root {
  color-scheme: light dark;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
  --muted: #898781; --line: #e1e0d9; --border: rgba(11,11,11,0.10);
}
@media (prefers-color-scheme: dark) {
  :root { --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
          --line: #2c2c2a; --border: rgba(255,255,255,0.10); }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
       font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 0 0 8px; }
.sub, .muted { color: var(--ink-2); font-size: 13px; }
section { background: var(--surface); border: 1px solid var(--border);
          border-radius: 10px; padding: 20px; margin-top: 18px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
         gap: 12px; margin-top: 18px; }
.tile { background: var(--surface); border: 1px solid var(--border);
        border-radius: 10px; padding: 14px 16px; }
.tile-label { font-size: 12px; color: var(--ink-2); }
.tile-value { font-size: 26px; font-weight: 650; margin: 2px 0 6px; }
.tile-unit { font-size: 13px; font-weight: 400; color: var(--muted); margin-left: 4px; }
.badge { display: inline-block; font-size: 10.5px; letter-spacing: 0.04em;
         padding: 1px 8px; border-radius: 999px; border: 1px solid var(--border);
         color: var(--ink-2); text-transform: uppercase; }
.badge-measured { border-color: #2a78d6; }
.badge-derived { border-color: #eb6834; }
.maps { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; }
.map-card { background: #fcfcfb; border: 1px solid var(--border); border-radius: 10px;
            padding: 8px; overflow-x: auto; }
.map-card img { max-width: 100%; display: block; margin: 0 auto; }
.bars { margin-top: 14px; }
.bar-row { display: grid; grid-template-columns: 220px 1fr; gap: 10px;
           align-items: center; margin-bottom: 8px; }
.bar-name { font-size: 13px; }
.bar-range { color: var(--muted); font-size: 11.5px; margin-left: 6px; }
.bar-track { display: flex; align-items: center; gap: 8px; height: 18px; }
.bar-fill { height: 100%; border-radius: 0 4px 4px 0; min-width: 2px; flex-shrink: 0; }
.bar-value { font-size: 12px; color: var(--ink-2); white-space: nowrap; }
table { border-collapse: collapse; width: 100%; margin-top: 10px; font-size: 13px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); }
th { color: var(--ink-2); font-weight: 600; }
td:nth-child(n+3), th:nth-child(n+3) { text-align: right; font-variant-numeric: tabular-nums; }
details summary { cursor: pointer; color: var(--ink-2); font-size: 13px; margin-top: 12px; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 18px;
     font-size: 13px; margin: 8px 0 0; }
dt { color: var(--ink-2); }
dd { margin: 0; }
.warn { border-left: 3px solid #fab219; padding: 8px 12px; background: var(--surface);
        margin-top: 10px; font-size: 13px; }
code { background: var(--line); border-radius: 4px; padding: 1px 5px; font-size: 12.5px; }
a { color: #2a78d6; }
"""


def write_html_report(
    report: dict[str, Any],
    dem: RasterData,
    slope: RasterData,
    hillshade: RasterData,
    path: str,
) -> None:
    radius = report["request"]["radius_km"]
    dem_png = _render_map(dem, "Elevation", "m above sea level", ELEVATION_RAMP, radius)
    slope_png = _render_map(slope, "Slope", "degrees", SLOPE_RAMP, radius)
    shade_png = _render_map(hillshade, "Hillshade (visualization only)", "", None, radius, grayscale=True)

    req = report["request"]
    src = report["data_source"]
    val = src["validation"]
    location = (
        f"{req['latitude']:.4f}, {req['longitude']:.4f} &middot; radius {req['radius_km']:g} km"
        if req["latitude"] is not None
        else "user-supplied DEM extent"
    )
    warnings_html = "".join(f'<div class="warn">{html.escape(w)}</div>' for w in report["warnings"])

    methodology_rows = "".join(
        f"<tr><td>{html.escape(m['name'].replace('_', ' ').title())}</td>"
        f"<td><span class='badge badge-{m['kind']}'>{m['kind']}</span></td>"
        f"<td><code>docs/{html.escape(m['methodology'])}</code></td></tr>"
        for m in report["metrics"]
    )

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terrain assessment &mdash; {location.split(' &middot;')[0]}</title>
<style>{STYLE}</style>
</head>
<body>
<main>
  <h1>Terrain assessment</h1>
  <p class="sub">{location} &middot; generated {html.escape(report['generated_utc'])} &middot;
     microwatershed-dss v{html.escape(report['application']['version'])}</p>
  {warnings_html}

  <div class="tiles">
  {_metric_tiles(report)}
  </div>

  <section>
    <h2>Maps</h2>
    <div class="maps">
      <div class="map-card"><img alt="Elevation map" src="data:image/png;base64,{dem_png}"></div>
      <div class="map-card"><img alt="Slope map" src="data:image/png;base64,{slope_png}"></div>
      <div class="map-card"><img alt="Hillshade map" src="data:image/png;base64,{shade_png}"></div>
    </div>
    <p class="muted">Dashed circle marks the analysis radius; metrics are computed from cells inside it.
    Grey cells have no data.</p>
  </section>

  {_slope_class_chart(report)}

  <section>
    <h2>Data source &amp; processing</h2>
    <dl>
      <dt>Elevation source</dt><dd>{html.escape(src['description'])}</dd>
      <dt>Analysis resolution</dt><dd>{src['native_resolution_m']} m per pixel</dd>
      <dt>Analysis CRS</dt><dd>{html.escape(src['analysis_crs'])}</dd>
      <dt>Valid data coverage</dt><dd>{val['valid_fraction'] * 100:.1f}% of the raster</dd>
      <dt>Slope engine</dt><dd>{html.escape(report['processing']['slope_engine'])}
          ({html.escape(report['processing']['slope_algorithm'])})</dd>
    </dl>
  </section>

  <section>
    <h2>Metrics &amp; methodology</h2>
    <p class="muted">{html.escape(report['provenance_note'])}</p>
    <table>
      <thead><tr><th>Metric</th><th>Provenance</th><th>Methodology document</th></tr></thead>
      <tbody>{methodology_rows}</tbody>
    </table>
  </section>
</main>
</body>
</html>
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
