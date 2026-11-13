"""Generate the self-contained HTML watershed delineation report.

Follows the Milestone 1 report design: matplotlib maps embedded as base64
PNGs, the same validated single-hue ramps per data layer (blue for elevation
and water-related layers, orange for slope), light-surface map cards, and
page chrome that adapts to light/dark mode. The catchment boundary is drawn
on every map so the delineation can be inspected against the terrain.
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
from .html_report import (
    ELEVATION_RAMP,
    LIGHT_SURFACE,
    MUTED_INK,
    SLOPE_RAMP,
    STYLE,
    _metric_tiles,
    _slope_class_chart,
)

OUTLINE_COLOR = "#0b0b0b"
SNAPPED_MARKER = "#b94a1e"  # orange 600: stands out on the blue ramps


def _render_catchment_map(
    raster: RasterData,
    catchment: np.ndarray,
    title: str,
    unit: str,
    ramp: list[str],
    outlet_rc: tuple[int, int],
    requested_rc: tuple[int, int] | None = None,
    log_scale: bool = False,
) -> str:
    """Raster map with the catchment outline and outlet markers, as base64 PNG."""
    values = raster.values.copy()
    if log_scale:
        values = np.log10(np.where(values > 0, values, np.nan))
        unit = f"log10({unit})"
    h, w = values.shape
    half_w_km = (w * raster.resolution_m) / 2000.0
    half_h_km = (h * raster.resolution_m) / 2000.0
    extent = (-half_w_km, half_w_km, -half_h_km, half_h_km)

    def to_km(row: int, col: int) -> tuple[float, float]:
        x = (col + 0.5) * raster.resolution_m / 1000.0 - half_w_km
        y = half_h_km - (row + 0.5) * raster.resolution_m / 1000.0
        return x, y

    cmap = LinearSegmentedColormap.from_list("ramp", ramp).copy()
    cmap.set_bad(color="#e1e0d9")

    fig, ax = plt.subplots(figsize=(5.4, 5.0), dpi=120)
    fig.patch.set_facecolor(LIGHT_SURFACE)
    ax.set_facecolor(LIGHT_SURFACE)
    im = ax.imshow(np.ma.masked_invalid(values), cmap=cmap, extent=extent, interpolation="nearest")
    ax.contour(
        catchment.astype(float), levels=[0.5], extent=extent, origin="upper",
        colors=[OUTLINE_COLOR], linewidths=1.2,
    )
    if requested_rc is not None and requested_rc != outlet_rc:
        ax.plot(*to_km(*requested_rc), marker="x", color=OUTLINE_COLOR, markersize=7,
                markeredgewidth=1.6, linestyle="none")
    ax.plot(*to_km(*outlet_rc), marker="o", color=SNAPPED_MARKER, markersize=7,
            markeredgecolor="white", markeredgewidth=1.2, linestyle="none")

    ax.set_title(title, fontsize=11, color="#0b0b0b", pad=10)
    ax.set_xlabel("km (east-west from DEM centre)", fontsize=8, color=MUTED_INK)
    ax.set_ylabel("km (north-south)", fontsize=8, color=MUTED_INK)
    ax.tick_params(colors=MUTED_INK, labelsize=7)
    for spine in ax.spines.values():
        spine.set_color("#c3c2b7")
        spine.set_linewidth(0.6)
    cbar = fig.colorbar(im, ax=ax, shrink=0.75, pad=0.03)
    cbar.set_label(unit, fontsize=8, color=MUTED_INK)
    cbar.ax.tick_params(colors=MUTED_INK, labelsize=7)
    cbar.outline.set_edgecolor("#c3c2b7")
    cbar.outline.set_linewidth(0.6)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=LIGHT_SURFACE)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _dl(pairs: list[tuple[str, Any]]) -> str:
    rows = "".join(f"<dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd>" for k, v in pairs)
    return f"<dl>{rows}</dl>"


def _delineation_sections(report: dict[str, Any]) -> str:
    deli = report["delineation"]
    outlet = deli["snapped_outlet"]
    checks = deli["validation_checks"]
    mass = checks["mass_balance"]

    outlet_html = _dl([
        ("Snapped outlet", f"{outlet['latitude']:.6f}, {outlet['longitude']:.6f}"),
        ("Distance moved by snapping", f"{outlet['distance_moved_m']:g} m"),
        ("On drainage network", "yes" if outlet["on_drainage_network"] else "no (see warnings)"),
        ("Upstream cells at outlet", f"{outlet['flow_accumulation_cells']:,}"),
    ])

    checks_html = _dl([
        ("Interior pits after conditioning", checks["interior_pits_after_conditioning"]),
        ("Interior cells without flow direction", checks["unresolved_interior_cells"]),
        ("Flow-accumulation mass balance",
         f"{'passed' if mass['passed'] else 'FAILED'} "
         f"({mass['terminal_accumulation_sum']:.0f} terminal vs {mass['valid_cells']:,} valid cells)"),
        ("Catchment equals outlet accumulation",
         "yes" if checks["catchment_equals_outlet_accumulation"] else "NO"),
        ("Total DEM relief", f"{checks['total_relief_m']:g} m"),
        ("Catchment touches data boundary",
         ", ".join(deli["boundary"]["sides"]) if deli["boundary"]["touches_data_boundary"] else "no"),
    ])

    comparison_html = ""
    comp = deli.get("depression_treatment_comparison")
    if comp:
        rows = "".join(
            f"<tr><td>{html.escape(label)}</td><td>{comp[key]['cells_modified']:,}</td>"
            f"<td>{comp[key]['max_raise_m']:g}</td><td>{comp[key]['max_lower_m']:g}</td></tr>"
            for key, label in (("breach_then_fill", "Breach, then fill (selected)"),
                               ("fill_only", "Fill only"))
        )
        comparison_html = f"""<h3>Depression treatment comparison</h3>
  <p class="muted">{html.escape(comp['rationale'])}</p>
  <table><thead><tr><th>Strategy</th><th>Cells modified</th><th>Max raise (m)</th>
  <th>Max lower (m)</th></tr></thead><tbody>{rows}</tbody></table>"""

    expansion_html = ""
    history = deli.get("dem_expansion_history") or []
    if len(history) > 1:
        rows = "".join(
            f"<tr><td>{step['radius_km']:g}</td>"
            f"<td>{'yes — expanded' if step['touches_boundary'] else 'no'}</td>"
            f"<td>{html.escape(', '.join(step.get('boundary_sides', [])) or '—')}</td></tr>"
            for step in history
        )
        expansion_html = f"""<h3>Automatic DEM expansion</h3>
  <p class="muted">The DEM radius was grown until the catchment no longer reached the data boundary.</p>
  <table><thead><tr><th>Radius (km)</th><th>Catchment reached boundary</th><th>Sides</th></tr></thead>
  <tbody>{rows}</tbody></table>"""

    return f"""<section>
    <h2>Delineation details</h2>
    <h3>Outlet</h3>
    {outlet_html}
    <h3>Validation checks</h3>
    {checks_html}
    {comparison_html}
    {expansion_html}
  </section>"""


def write_watershed_html_report(
    report: dict[str, Any],
    dem: RasterData,
    slope: RasterData,
    accumulation: RasterData,
    catchment: np.ndarray,
    outlet_rc: tuple[int, int],
    requested_rc: tuple[int, int],
    path: str,
) -> None:
    dem_png = _render_catchment_map(
        dem, catchment, "Elevation & catchment", "m above sea level",
        ELEVATION_RAMP, outlet_rc, requested_rc,
    )
    accum_png = _render_catchment_map(
        accumulation, catchment, "Flow accumulation", "upstream cells",
        ELEVATION_RAMP, outlet_rc, log_scale=True,
    )
    slope_png = _render_catchment_map(
        slope, catchment, "Slope & catchment", "degrees", SLOPE_RAMP, outlet_rc,
    )

    req = report["request"]
    src = report["data_source"]
    deli = report["delineation"]
    location = f"outlet {req['outlet_latitude']:.4f}, {req['outlet_longitude']:.4f}"
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
<title>Watershed delineation &mdash; {req['outlet_latitude']:.4f}, {req['outlet_longitude']:.4f}</title>
<style>{STYLE}
h3 {{ font-size: 14px; margin: 18px 0 4px; }}</style>
</head>
<body>
<main>
  <h1>Watershed delineation</h1>
  <p class="sub">{location} &middot; catchment {deli['catchment']['area_ha']:,} ha
     ({deli['catchment']['area_km2']:g} km&sup2;) &middot;
     generated {html.escape(report['generated_utc'])} &middot;
     microwatershed-dss v{html.escape(report['application']['version'])}</p>
  {warnings_html}

  <div class="tiles">
  {_metric_tiles(report)}
  </div>

  <section>
    <h2>Maps</h2>
    <div class="maps">
      <div class="map-card"><img alt="Elevation and catchment map" src="data:image/png;base64,{dem_png}"></div>
      <div class="map-card"><img alt="Flow accumulation map" src="data:image/png;base64,{accum_png}"></div>
      <div class="map-card"><img alt="Slope and catchment map" src="data:image/png;base64,{slope_png}"></div>
    </div>
    <p class="muted">Black outline: delineated catchment. Orange dot: snapped outlet;
    black cross: requested outlet (when different). Terrain metrics below are computed
    from cells inside the catchment only. Grey cells have no data.</p>
  </section>

  {_slope_class_chart(report)}

  {_delineation_sections(report)}

  <section>
    <h2>Data source &amp; processing</h2>
    <dl>
      <dt>Elevation source</dt><dd>{html.escape(src['description'])}</dd>
      <dt>Analysis resolution</dt><dd>{src['native_resolution_m']} m per pixel</dd>
      <dt>Analysis CRS</dt><dd>{html.escape(src['analysis_crs'])}</dd>
      <dt>Hydrology engine</dt><dd>{html.escape(report['processing']['hydrology_engine'])}</dd>
      <dt>Depression treatment</dt><dd>{html.escape(report['processing']['depression_treatment'])}</dd>
      <dt>Flow model</dt><dd>{html.escape(report['processing']['flow_model'])}</dd>
      <dt>Outlet snapping</dt><dd>{html.escape(report['processing']['outlet_snapping'])}</dd>
      <dt>Slope engine</dt><dd>{html.escape(report['processing']['slope_engine'])}
          ({html.escape(report['processing']['slope_algorithm'])})</dd>
    </dl>
  </section>

  <section>
    <h2>Metrics &amp; methodology</h2>
    <p class="muted">{html.escape(report['provenance_note'])}</p>
    <p class="muted">{html.escape(report['delineation_note'])}</p>
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
