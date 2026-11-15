"""Fixed, public grouping definitions and privacy parameters.

The street and age-band lists are part of the public release contract.  They
are intentionally hard-coded: changing them changes the published histogram
schema and must be treated as a new API/release contract.
"""

from __future__ import annotations

# Public, fixed street grouping (5 groups).
STREETS: tuple[str, ...] = (
    "东城街道",
    "西城街道",
    "南城街道",
    "北城街道",
    "开发区街道",
)

# Public, fixed age-band grouping (5 groups).
AGE_BANDS: tuple[str, ...] = (
    "0-14",
    "15-29",
    "30-44",
    "45-59",
    "60+",
)

# One person changes exactly one histogram cell by one, so l1 sensitivity = 1.
SENSITIVITY: int = 1

# Each cell is published with Laplace(0, SENSITIVITY / EPSILON).
EPSILON: float = 1.0
LAPLACE_SCALE: float = SENSITIVITY / EPSILON

# A published version, no matter how many cells it contains, costs one unit of
# the global (per-deployment) privacy budget.
DEFAULT_TOTAL_BUDGET: int = 3
COST_PER_PUBLICATION: int = 1

STREET_AGE_CELLS: tuple[tuple[str, str], ...] = tuple(
    (street, age_band) for street in STREETS for age_band in AGE_BANDS
)
