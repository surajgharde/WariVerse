"""Projecting pixels to ground metres.

Consumer only.  The matrix is solved once, in the core API, by an operator
clicking four points; this module applies it.  Keeping the solve on one side of
the boundary means there is one implementation of the arithmetic that decides
whether a zone reads 3.4 or 4.1 people per square metre.

No numpy: a 3x3 matrix-vector product per detection is faster in plain Python
than the array allocation would be, and it keeps this module importable in the
test environment without the vision stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot

_EPS = 1e-9


class NotOnGroundPlane(ValueError):
    """The pixel projects to the horizon — it is outside the calibrated area."""


@dataclass(frozen=True, slots=True)
class Homography:
    matrix: tuple[float, ...]  # row-major 3x3

    @classmethod
    def from_list(cls, values: list[float] | tuple[float, ...]) -> Homography:
        if len(values) != 9:
            raise ValueError("A homography is nine numbers, row-major 3x3")
        return cls(matrix=tuple(float(v) for v in values))

    def project(self, x: float, y: float) -> tuple[float, float]:
        h = self.matrix
        w = h[6] * x + h[7] * y + h[8]
        if abs(w) < _EPS:
            raise NotOnGroundPlane(f"pixel ({x}, {y}) projects to the horizon")
        return ((h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w)

    def try_project(self, x: float, y: float) -> tuple[float, float] | None:
        """Projection that drops rather than raises.

        A detection near the top of the frame can genuinely sit on the horizon
        line.  Losing one detection is correct; losing the whole window because
        of it is not.
        """
        try:
            return self.project(x, y)
        except NotOnGroundPlane:
            return None

    def metres_per_pixel(self, x: float, y: float, delta: float = 1.0) -> float:
        """Local ground scale.

        Perspective makes this vary hugely across a frame — a pilgrim near the
        top can cover four or five times more ground per pixel than one at the
        bottom.  Velocity is therefore converted where it was measured, never
        with one global factor.
        """
        origin = self.try_project(x, y)
        along_x = self.try_project(x + delta, y)
        along_y = self.try_project(x, y + delta)
        if origin is None or along_x is None or along_y is None:
            return 0.0
        dx = hypot(along_x[0] - origin[0], along_x[1] - origin[1])
        dy = hypot(along_y[0] - origin[0], along_y[1] - origin[1])
        return (dx + dy) / (2.0 * delta)
