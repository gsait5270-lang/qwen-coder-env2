"""Comments that explain WHY must never be flagged as noise."""

import math


def ray_radius(d: float) -> float:
    # Halve the diameter: callers pass measured diameters, but the
    # downstream integrator expects radii (see spec 4.2).
    return d / 2.0


def safe_log(x: float) -> float:
    # Clamp to avoid log(0) blowing up on sparse histograms; 1e-12 is
    # below the sensor noise floor, so this never masks real signal.
    return math.log(max(x, 1e-12))
