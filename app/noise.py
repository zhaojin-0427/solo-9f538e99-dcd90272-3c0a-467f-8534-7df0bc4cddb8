"""Laplace mechanism helpers."""

from __future__ import annotations

import math
import secrets

# SystemRandom is backed by os.urandom and is safe to share between threads.
_RNG = secrets.SystemRandom()


def sample_laplace(scale: float) -> float:
    """Draw a sample from Laplace(0, scale) using inverse-CDF sampling.

    With u ~ Uniform(-0.5, 0.5):
        x = -scale * sign(u) * ln(1 - 2|u|)
    """

    if scale < 0:
        raise ValueError("scale must be non-negative")
    if scale == 0:
        return 0.0

    u = _RNG.random() - 0.5
    return -scale * math.copysign(1.0, u) * math.log1p(-2.0 * abs(u))


def noisy_count(true_count: int, epsilon: float, sensitivity: int = 1) -> int:
    """Add Laplace noise and round to an integer count.

    Rounding is post-processing and does not change the differential-privacy
    guarantee.  Negative counts are intentionally kept: clamping would alter
    the mechanism's output distribution for small cells.
    """

    if epsilon <= 0:
        raise ValueError("epsilon must be positive")
    scale = sensitivity / epsilon
    return int(round(true_count + sample_laplace(scale)))
