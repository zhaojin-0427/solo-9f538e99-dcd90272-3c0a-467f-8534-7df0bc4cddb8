"""Tests for the Laplace mechanism and its parameters."""

from __future__ import annotations

import math
import statistics

from app import groups
from app.noise import noisy_count, sample_laplace


def test_mechanism_parameters():
    assert groups.SENSITIVITY == 1
    assert groups.EPSILON == 1.0
    assert groups.LAPLACE_SCALE == 1.0
    assert groups.COST_PER_PUBLICATION == 1
    assert groups.DEFAULT_TOTAL_BUDGET == 3


def test_laplace_distribution_moments():
    scale = 1.0
    samples = [sample_laplace(scale) for _ in range(20_000)]
    # Laplace(0, b): mean 0, variance 2b^2.
    assert abs(statistics.fmean(samples)) < 0.05
    assert abs(statistics.variance(samples) - 2 * scale**2) < 0.2


def test_noisy_count_returns_integers_around_truth():
    samples = [noisy_count(10, epsilon=1.0) for _ in range(5_000)]
    assert all(isinstance(s, int) for s in samples)
    assert abs(statistics.fmean(samples) - 10) < 0.15


def test_noisy_count_is_unbiased_and_keeps_negatives_possible():
    # A cell with true count 0 must still sometimes go negative; clamping
    # would distort the mechanism.
    samples = [noisy_count(0, epsilon=1.0) for _ in range(2_000)]
    assert min(samples) < 0
    assert max(samples) > 0


def test_smaller_epsilon_means_larger_noise():
    tight = [abs(noisy_count(0, epsilon=10.0)) for _ in range(2_000)]
    loose = [abs(noisy_count(0, epsilon=0.5)) for _ in range(2_000)]
    assert statistics.fmean(loose) > statistics.fmean(tight) * 3


def test_invalid_epsilon_rejected():
    try:
        noisy_count(1, epsilon=0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("epsilon=0 must be rejected")


def test_laplace_scale_validation():
    assert sample_laplace(0) == 0.0
    assert math.isfinite(sample_laplace(1.0))
