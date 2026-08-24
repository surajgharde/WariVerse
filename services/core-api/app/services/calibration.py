"""Camera homography: four clicked points to real metres.

Section 4/M2: *"each zone needs a homography (4 image points -> 4 real world
points) so pixel counts convert to m². Without this the density number is
fiction."*

That sentence is the whole reason this module exists, and it is why
`solve_homography` refuses degenerate input instead of returning a matrix that
would quietly produce a plausible-looking wrong answer.  A density of 4.1 p/m²
dispatches officers and closes a gate; it has to be earned.

Deliberately dependency-free.  This is one 8x8 linear solve — pulling numpy into
the core API for it would add 30 MB to an image whose job is to own state, not
to do vision work.

Solving happens *only* here.  The AI engine never derives a homography; it pulls
the matrix from `GET /ingest/config` and projects with it, so there is one
implementation of the maths that matters and no chance of the two drifting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.errors import AppError

#: Below this, the 8x8 system is effectively singular — the four points do not
#: describe a plane (three of them are collinear, or two coincide).
_SINGULAR_EPS = 1e-9

#: Round-trip tolerance in metres.  A homography that cannot reproduce its own
#: four anchor points to within a centimetre is not a calibration.
_RESIDUAL_TOLERANCE_M = 0.01

Point = tuple[float, float]


@dataclass(frozen=True, slots=True)
class Homography:
    """Row-major 3x3 mapping image pixels to ground-plane metres."""

    matrix: tuple[float, ...]  # 9 values, h[8] normalised to 1.0
    residual_m: float

    def project(self, x: float, y: float) -> Point:
        """Pixel -> metres on the ground plane."""
        h = self.matrix
        w = h[6] * x + h[7] * y + h[8]
        if abs(w) < _SINGULAR_EPS:
            # The point is on the horizon line: it maps to infinity, which for
            # a ground plane means "outside the calibrated region".
            raise AppError(
                "CALIBRATION_INVALID",
                details={"reason": "point projects to the horizon", "pixel": [x, y]},
            )
        return ((h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w)

    def to_json(self) -> dict[str, Any]:
        return {"matrix": list(self.matrix), "residual_m": self.residual_m}

    @classmethod
    def from_json(cls, value: dict[str, Any] | None) -> Homography | None:
        if not value or "matrix" not in value:
            return None
        matrix = value["matrix"]
        if not isinstance(matrix, list) or len(matrix) != 9:
            return None
        return cls(
            matrix=tuple(float(v) for v in matrix),
            residual_m=float(value.get("residual_m", 0.0)),
        )


def _solve_linear(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting.  None if singular."""
    n = len(b)
    # Work on a copy — the caller's rows are used again for the residual check.
    m = [row[:] + [b[i]] for i, row in enumerate(a)]

    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < _SINGULAR_EPS:
            return None
        m[col], m[pivot] = m[pivot], m[col]

        inv = 1.0 / m[col][col]
        for j in range(col, n + 1):
            m[col][j] *= inv

        for row in range(n):
            if row == col:
                continue
            factor = m[row][col]
            if factor == 0.0:
                continue
            for j in range(col, n + 1):
                m[row][j] -= factor * m[col][j]

    return [m[i][n] for i in range(n)]


def solve_homography(image_points: list[Point], world_points: list[Point]) -> Homography:
    """Direct linear transform from exactly four correspondences.

    `image_points` are pixels on a still frame (origin top-left).
    `world_points` are metres in any consistent local ground frame — a surveyed
    corner, a tape measure across a plaza, anything real.  The origin does not
    matter; only distances do, because all we ever compute from this is area.
    """
    if len(image_points) != 4 or len(world_points) != 4:
        raise AppError(
            "CALIBRATION_INVALID",
            details={
                "reason": "exactly four point pairs are required",
                "image_points": len(image_points),
                "world_points": len(world_points),
            },
        )

    for name, points in (("image", image_points), ("world", world_points)):
        if _has_duplicate(points):
            raise AppError("CALIBRATION_INVALID", details={"reason": f"duplicate {name} points"})
        if _any_collinear(points):
            raise AppError(
                "CALIBRATION_INVALID",
                details={"reason": f"three or more {name} points lie on one line"},
            )

    rows: list[list[float]] = []
    rhs: list[float] = []
    for (x, y), (wx, wy) in zip(image_points, world_points, strict=True):
        rows.append([x, y, 1.0, 0.0, 0.0, 0.0, -wx * x, -wx * y])
        rhs.append(wx)
        rows.append([0.0, 0.0, 0.0, x, y, 1.0, -wy * x, -wy * y])
        rhs.append(wy)

    solution = _solve_linear(rows, rhs)
    if solution is None:
        raise AppError("CALIBRATION_INVALID", details={"reason": "the four points are degenerate"})

    homography = Homography(matrix=(*solution, 1.0), residual_m=0.0)

    # Verify by mapping the anchors back.  A matrix that fails its own inputs
    # would fail every real pixel too, silently.
    worst = 0.0
    for (x, y), (wx, wy) in zip(image_points, world_points, strict=True):
        px, py = homography.project(x, y)
        worst = max(worst, ((px - wx) ** 2 + (py - wy) ** 2) ** 0.5)

    if worst > _RESIDUAL_TOLERANCE_M:
        raise AppError(
            "CALIBRATION_INVALID",
            details={"reason": "solution does not reproduce its own anchor points", "residual_m": round(worst, 4)},
        )

    return Homography(matrix=homography.matrix, residual_m=round(worst, 6))


def _has_duplicate(points: list[Point]) -> bool:
    return len({(round(x, 6), round(y, 6)) for x, y in points}) < len(points)


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _any_collinear(points: list[Point], tolerance: float = 1e-6) -> bool:
    n = len(points)
    for i in range(n):
        for j in range(i + 1, n):
            for k in range(j + 1, n):
                if abs(_cross(points[i], points[j], points[k])) < tolerance:
                    return True
    return False


def polygon_area_m2(homography: Homography, image_polygon: list[Point]) -> float:
    """Ground area of a polygon drawn on the image, by shoelace after projection.

    This is what turns "the operator drew a box around the queue" into the
    `area_m2` that every density figure divides by.
    """
    if len(image_polygon) < 3:
        raise AppError("CALIBRATION_INVALID", details={"reason": "a polygon needs at least three points"})

    projected = [homography.project(x, y) for x, y in image_polygon]
    total = 0.0
    for i, (x1, y1) in enumerate(projected):
        x2, y2 = projected[(i + 1) % len(projected)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def metres_per_pixel(homography: Homography, x: float, y: float, delta: float = 1.0) -> float:
    """Local scale at one pixel — used to convert tracked px/s into m/s.

    Perspective means this varies across the frame: a pilgrim at the top of the
    image covers far more ground per pixel than one at the bottom.  Flow speed
    is therefore calibrated where it was measured, not with one global factor.
    """
    ax, ay = homography.project(x, y)
    bx, by = homography.project(x + delta, y)
    cx, cy = homography.project(x, y + delta)
    dx = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
    dy = ((cx - ax) ** 2 + (cy - ay) ** 2) ** 0.5
    return (dx + dy) / (2.0 * delta)
