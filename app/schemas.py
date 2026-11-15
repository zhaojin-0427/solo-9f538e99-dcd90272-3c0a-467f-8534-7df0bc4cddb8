"""Pydantic request/response models."""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .groups import AGE_BANDS, STREETS

_WEEK_PATTERN = re.compile(r"^(\d{4})-W(\d{2})$")
_STREET_SET = frozenset(STREETS)
_AGE_BAND_SET = frozenset(AGE_BANDS)

PatientId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]


class PatientRecordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: PatientId
    street: str
    age_band: str

    @field_validator("street")
    @classmethod
    def _known_street(cls, value: str) -> str:
        if value not in _STREET_SET:
            raise ValueError(
                f"unknown street; expected one of the fixed public groups: "
                f"{', '.join(STREETS)}"
            )
        return value

    @field_validator("age_band")
    @classmethod
    def _known_age_band(cls, value: str) -> str:
        if value not in _AGE_BAND_SET:
            raise ValueError(
                f"unknown age_band; expected one of the fixed public groups: "
                f"{', '.join(AGE_BANDS)}"
            )
        return value


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    week: str
    records: list[PatientRecordIn] = Field(min_length=1, max_length=200_000)

    @field_validator("week")
    @classmethod
    def _iso_week(cls, value: str) -> str:
        match = _WEEK_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError("week must use the ISO form YYYY-Www, e.g. 2026-W20")
        year, week = int(match.group(1)), int(match.group(2))
        try:
            date.fromisocalendar(year, week, 1)
        except ValueError as exc:
            raise ValueError("week is not a valid ISO week") from exc
        return value


class ImportResponse(BaseModel):
    version: int
    week: str
    record_count: int


class CountCell(BaseModel):
    street: str
    age_band: str
    count: int


class PublicationResponse(BaseModel):
    """Public release payload: noisy cells, release version, remaining budget."""

    version: int
    counts: list[CountCell]
    remaining_budget: int


class PublicationSummary(BaseModel):
    version: int
    import_version: int
    counts: list[CountCell]


class PublicationListResponse(BaseModel):
    publications: list[PublicationSummary]
    remaining_budget: int


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    import_version: int = Field(ge=1)


class BudgetResponse(BaseModel):
    total_budget: int
    spent: int
    remaining_budget: int


class HealthResponse(BaseModel):
    status: str


class ServiceInfoResponse(BaseModel):
    service: str
    docs: str
