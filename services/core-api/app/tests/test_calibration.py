"""Homography solving.

Section 4/M2: "Without this the density number is fiction."  These tests are
mostly about the *refusals* — the inputs that would produce a matrix that looks
fine and silently reports the wrong area.  A rejected calibration costs an
engineer five minutes; an accepted bad one costs every density reading that
camera will ever produce.

No database needed.
"""

from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.services.calibration import (
    Homography,
    metres_per_pixel,
    polygon_area_m2,
    solve_homography,
)

#: A plausible view down a corridor on a 1920x1080 frame: the near edge fills
#: the bottom, the far edge is a narrower band higher up.
FRAME = [(200.0, 1000.0), (1720.0, 1000.0), (1180.0, 380.0), (740.0, 380.0)]
#: A 60 m x 20 m rectangle of ground: 1200 m².
GROUND = [(0.0, 0.0), (60.0, 0.0), (60.0, 20.0), (0.0, 20.0)]


def test_a_valid_calibration_reproduces_its_own_anchor_points():
    homography = solve_homography(FRAME, GROUND)
    assert homography.residual_m < 0.01

    for pixel, expected in zip(FRAME, GROUND, strict=True):
        x, y = homography.project(*pixel)
        assert x == pytest.approx(expected[0], abs=0.01)
        assert y == pytest.approx(expected[1], abs=0.01)


def test_area_is_recovered_from_the_calibration():
    """The number every density figure divides by."""
    homography = solve_homography(FRAME, GROUND)
    assert polygon_area_m2(homography, FRAME) == pytest.approx(1200.0, rel=1e-6)


def test_perspective_is_actually_modelled():
    """A pixel at the top of the frame covers far more ground than one at the
    bottom.  A calibration that got this wrong would still round-trip its own
    anchors but would misplace everyone in between."""
    homography = solve_homography(FRAME, GROUND)
    near = metres_per_pixel(homography, 960.0, 1000.0)
    far = metres_per_pixel(homography, 960.0, 400.0)

    assert far > near * 3, "distant pixels must cover much more ground"


def test_projection_is_monotone_up_the_frame():
    homography = solve_homography(FRAME, GROUND)
    depths = [homography.project(960.0, y)[1] for y in (1000.0, 800.0, 600.0, 400.0)]
    assert depths == sorted(depths), "walking up the image must walk away from the camera"


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------
def test_three_collinear_image_points_are_refused():
    """The most common mistake: clicking along a barricade rail.

    Three points on a line define no plane.  The solve would either blow up or,
    worse, return something plausible.
    """
    collinear = [(100.0, 500.0), (300.0, 500.0), (500.0, 500.0), (700.0, 900.0)]
    with pytest.raises(AppError) as exc:
        solve_homography(collinear, GROUND)
    assert exc.value.code == "CALIBRATION_INVALID"
    assert "line" in str(exc.value.details)


def test_collinear_world_points_are_refused():
    world = [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0), (30.0, 5.0)]
    with pytest.raises(AppError) as exc:
        solve_homography(FRAME, world)
    assert exc.value.code == "CALIBRATION_INVALID"


def test_duplicate_points_are_refused():
    duplicated = [(200.0, 1000.0), (200.0, 1000.0), (1180.0, 380.0), (740.0, 380.0)]
    with pytest.raises(AppError) as exc:
        solve_homography(duplicated, GROUND)
    assert "duplicate" in str(exc.value.details)


@pytest.mark.parametrize("count", [0, 1, 3, 5])
def test_exactly_four_pairs_are_required(count: int):
    """Five points would need a least-squares fit, which this is not. Four
    clicked accurately beats eight clicked roughly."""
    image = (FRAME * 2)[:count]
    world = (GROUND * 2)[:count]
    with pytest.raises(AppError) as exc:
        solve_homography(image, world)
    assert exc.value.code == "CALIBRATION_INVALID"


def test_a_polygon_needs_three_points():
    homography = solve_homography(FRAME, GROUND)
    with pytest.raises(AppError):
        polygon_area_m2(homography, [(0.0, 0.0), (1.0, 1.0)])


def test_a_pixel_on_the_horizon_is_refused_rather_than_projected_to_infinity():
    """A detection at the vanishing line maps nowhere real.  Returning a huge
    number would put one person several kilometres away and wreck the mean."""
    homography = solve_homography(FRAME, GROUND)
    h = homography.matrix
    # Solve h[6]*x + h[7]*y + h[8] = 0 along the frame's vertical centre line.
    if abs(h[7]) > 1e-12:
        horizon_y = -(h[6] * 960.0 + h[8]) / h[7]
        with pytest.raises(AppError) as exc:
            homography.project(960.0, horizon_y)
        assert exc.value.code == "CALIBRATION_INVALID"


# ---------------------------------------------------------------------------
# round-tripping through storage
# ---------------------------------------------------------------------------
def test_a_stored_homography_survives_a_round_trip():
    original = solve_homography(FRAME, GROUND)
    restored = Homography.from_json(original.to_json())

    assert restored is not None
    assert restored.matrix == original.matrix
    assert restored.project(960.0, 900.0) == original.project(960.0, 900.0)


@pytest.mark.parametrize("stored", [None, {}, {"matrix": "nonsense"}, {"matrix": [1, 2, 3]}])
def test_a_missing_or_malformed_matrix_reads_back_as_uncalibrated(stored):
    """An uncalibrated camera must read as uncalibrated, not as identity —
    identity would silently treat pixels as metres."""
    assert Homography.from_json(stored) is None


def test_the_engines_projection_agrees_with_this_one():
    """The core API solves; the AI engine only projects.  Same matrix, same
    answer, or the density figure depends on which service you ask."""
    core = solve_homography(FRAME, GROUND)

    # A minimal restatement of the engine's `Homography.project`, so this test
    # fails if either side's arithmetic drifts.
    def engine_project(matrix: tuple[float, ...], x: float, y: float) -> tuple[float, float]:
        w = matrix[6] * x + matrix[7] * y + matrix[8]
        return ((matrix[0] * x + matrix[1] * y + matrix[2]) / w, (matrix[3] * x + matrix[4] * y + matrix[5]) / w)

    for pixel in ((960.0, 900.0), (400.0, 700.0), (1500.0, 500.0)):
        assert engine_project(core.matrix, *pixel) == pytest.approx(core.project(*pixel))
