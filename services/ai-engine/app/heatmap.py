"""Heat-map overlays and density time series (Section 4/M2 acceptance).

    "run a sample crowd video, produce a heat map overlay and a density time
     series, and trigger a CRITICAL alert when the threshold is crossed"

The overlay is an operator's sanity check on the pipeline, not a product
surface.  Its job is to answer "is the detector actually seeing the crowd, or is
it seeing umbrellas" — which is a question you can only answer by looking.

It is a *density* map, not a person map.  Cells are 1–2 metres of ground each
and hold a count; nothing in the output distinguishes one person from another,
and the source frame is never saved alongside it.  An overlay written to disk is
still an image of a crowd, so `render_overlay` refuses to write anywhere except
the configured scratch directory and stamps every file with its timestamp and
zone so an operator can find and delete it.

SVG rather than PNG when matplotlib/opencv are absent: the fallback path has no
dependencies at all, which keeps the acceptance test runnable anywhere.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.logging import get_logger
from app.models import ZoneObservation

logger = get_logger(__name__)

#: Colour ramp keyed to the published safety bands, so the overlay and the map
#: and the alert feed all use one visual language.  Deuteranopia-safe: the
#: sequence is blue -> amber -> vermillion -> deep red, which separates by
#: lightness as well as hue (Section 10 accessibility).
BANDS: tuple[tuple[float, str, str], ...] = (
    (2.0, "#1f6feb", "safe"),
    (3.5, "#d4a017", "moderate"),
    (5.0, "#e8590c", "high"),
    (float("inf"), "#b3261e", "critical"),
)


def colour_for(density: float) -> str:
    for ceiling, colour, _ in BANDS:
        if density < ceiling:
            return colour
    return BANDS[-1][1]


def band_for(density: float) -> str:
    for ceiling, _, name in BANDS:
        if density < ceiling:
            return name
    return BANDS[-1][2]


@dataclass(frozen=True, slots=True)
class Overlay:
    zone_code: str
    observed_at: datetime
    svg: str
    peak_density: float
    mean_density: float

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = self.observed_at.strftime("%Y%m%dT%H%M%S")
        path = directory / f"heatmap-{self.zone_code}-{stamp}.svg"
        path.write_text(self.svg, encoding="utf-8")
        return path


def render_overlay(
    observation: ZoneObservation,
    *,
    width: int = 720,
    height: int = 480,
    grid: tuple[int, int] = (6, 4),
) -> Overlay:
    """A density heat map for one zone window, as standalone SVG.

    `observation.heat_cells` are normalised (x, y, density) triples — the
    pipeline's coarse grid of local densities.  They never leave the process in
    a published payload; this is the only consumer.
    """
    cols, rows = grid
    cells = observation.heat_cells
    densities = [d for _, _, d in cells] or [observation.density]
    peak = max(densities)
    mean = sum(densities) / len(densities)

    cell_w = width / cols
    cell_h = height / rows

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height + 64}" '
        f'viewBox="0 0 {width} {height + 64}" role="img" '
        f'aria-label="Crowd density heat map for zone {html.escape(observation.zone_code)}">',
        f'<rect width="{width}" height="{height + 64}" fill="#0d1117"/>',
    ]

    for cx, cy, density in cells:
        x = (cx * width) - cell_w / 2
        y = (cy * height) - cell_h / 2
        # Opacity carries magnitude within a band, colour carries the band —
        # so the image is still readable printed in greyscale on a control-room
        # wall, which is where these end up.
        opacity = min(1.0, 0.25 + 0.75 * min(density / 6.0, 1.0))
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell_w:.1f}" height="{cell_h:.1f}" '
            f'fill="{colour_for(density)}" opacity="{opacity:.2f}"/>'
        )

    # The flow arrow: the direction the crowd is actually moving, drawn from the
    # centre. Section 4/M3 asks for this explicitly, and it is what separates a
    # heat map from a crowd-*intelligence* display.
    speed = (observation.flow_dx**2 + observation.flow_dy**2) ** 0.5
    if speed > 0.05:
        scale = min(1.0, speed / 1.5) * (min(width, height) * 0.28)
        cx0, cy0 = width / 2, height / 2
        # SVG y grows downward; flow_dy is northward, hence the negation.
        x1 = cx0 + (observation.flow_dx / speed) * scale
        y1 = cy0 - (observation.flow_dy / speed) * scale
        parts.append(
            '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">'
            '<path d="M0,0 L6,3 L0,6 z" fill="#f0f6fc"/></marker></defs>'
        )
        parts.append(
            f'<line x1="{cx0:.1f}" y1="{cy0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
            'stroke="#f0f6fc" stroke-width="3" marker-end="url(#arrow)" opacity="0.85"/>'
        )

    caption = (
        f"{observation.zone_code} · {observation.person_count} people · "
        f"{observation.density:.2f} p/m² ({band_for(observation.density)}) · "
        f"flow {speed:.2f} m/s · stagnation {observation.stagnation_index:.2f} · "
        f"counter-flow {observation.counterflow_ratio:.2f}"
    )
    provenance = (
        f"{observation.observed_at.isoformat(timespec='seconds')} · "
        f"confidence {observation.confidence:.2f} · {observation.camera_count} camera(s) · "
        "aggregate only, no individual is identified"
    )

    parts.append(
        f'<text x="12" y="{height + 24}" fill="#e6edf3" font-family="sans-serif" font-size="14">'
        f"{html.escape(caption)}</text>"
    )
    parts.append(
        f'<text x="12" y="{height + 46}" fill="#8b949e" font-family="sans-serif" font-size="11">'
        f"{html.escape(provenance)}</text>"
    )
    parts.append("</svg>")

    return Overlay(
        zone_code=observation.zone_code,
        observed_at=observation.observed_at,
        svg="".join(parts),
        peak_density=round(peak, 3),
        mean_density=round(mean, 3),
    )


def render_series(points: list[tuple[datetime, float]], *, width: int = 720, height: int = 220) -> str:
    """A sparkline of density over time, with the safety bands drawn behind it.

    The bands are the point.  A line that climbs is ambiguous; a line that
    crosses from the amber band into the vermillion one is not.
    """
    if not points:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"></svg>'

    values = [v for _, v in points]
    ceiling = max(6.0, max(values) * 1.15)
    pad = 32.0
    plot_w = width - pad * 2
    plot_h = height - pad * 2

    def y_of(value: float) -> float:
        return pad + plot_h * (1.0 - min(value / ceiling, 1.0))

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="Density over time">',
        f'<rect width="{width}" height="{height}" fill="#0d1117"/>',
    ]

    previous = 0.0
    for ceiling_value, colour, name in BANDS:
        top = min(ceiling_value, ceiling)
        if previous >= ceiling:
            break
        parts.append(
            f'<rect x="{pad}" y="{y_of(top):.1f}" width="{plot_w:.1f}" '
            f'height="{max(0.0, y_of(previous) - y_of(top)):.1f}" fill="{colour}" opacity="0.14"/>'
        )
        parts.append(
            f'<text x="{width - pad + 4}" y="{y_of(top) + 12:.1f}" fill="#8b949e" '
            f'font-family="sans-serif" font-size="9">{name}</text>'
        )
        previous = ceiling_value

    step = plot_w / max(1, len(points) - 1)
    coords = " ".join(f"{pad + i * step:.1f},{y_of(v):.1f}" for i, v in enumerate(values))
    parts.append(f'<polyline points="{coords}" fill="none" stroke="#58a6ff" stroke-width="2"/>')

    first, last = points[0][0], points[-1][0]
    parts.append(
        f'<text x="{pad}" y="{height - 8}" fill="#8b949e" font-family="sans-serif" font-size="10">'
        f"{html.escape(first.strftime('%H:%M:%S'))}</text>"
    )
    parts.append(
        f'<text x="{width - pad:.0f}" y="{height - 8}" fill="#8b949e" font-family="sans-serif" '
        f'font-size="10" text-anchor="end">{html.escape(last.strftime("%H:%M:%S"))}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def summarise(observations: list[ZoneObservation]) -> dict[str, Any]:
    """A one-line state of the world, for `/status` and the acceptance script."""
    if not observations:
        return {"zones": 0}
    worst = max(observations, key=lambda o: o.density)
    return {
        "zones": len(observations),
        "total_people": sum(o.person_count for o in observations),
        "peak_zone": worst.zone_code,
        "peak_density": round(worst.density, 3),
        "peak_band": band_for(worst.density),
        "peak_stagnation": round(max(o.stagnation_index for o in observations), 3),
        "peak_counterflow": round(max(o.counterflow_ratio for o in observations), 3),
    }
