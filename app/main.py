"""FastAPI application factory and HTTP surface."""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .db import Database
from .errors import DomainError
from .groups import (
    AGE_BANDS,
    COST_PER_PUBLICATION,
    DEFAULT_TOTAL_BUDGET,
    EPSILON,
    SENSITIVITY,
    STREETS,
)
from .schemas import (
    BudgetResponse,
    HealthResponse,
    ImportRequest,
    ImportResponse,
    PublicationListResponse,
    PublicationResponse,
    PublishRequest,
    ServiceInfoResponse,
)
from .service import BeforeCommitHook, ReleaseService


def _total_budget_from_env() -> int:
    raw = os.environ.get("PRIVACY_TOTAL_BUDGET", str(DEFAULT_TOTAL_BUDGET))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("PRIVACY_TOTAL_BUDGET must be an integer") from exc
    if value < 0:
        raise RuntimeError("PRIVACY_TOTAL_BUDGET must be non-negative")
    return value


def create_app(
    database_path: str | None = None,
    total_budget: int | None = None,
    publish_before_commit: BeforeCommitHook | None = None,
) -> FastAPI:
    db_path = database_path or os.environ.get("DATABASE_PATH", "/data/fevers.db")
    budget = _total_budget_from_env() if total_budget is None else total_budget

    database = Database(db_path)
    database.initialize(budget)
    service = ReleaseService(
        database, total_budget=budget, before_commit=publish_before_commit
    )

    app = FastAPI(
        title="Fever Clinic DP Release API",
        version=__version__,
        description=(
            "Local API for importing de-identified weekly fever-clinic records "
            "and publishing fixed-schema street/age-band counts under "
            "epsilon=1 Laplace noise with a global budget of 3."
        ),
    )
    app.state.service = service

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "detail": exc.message},
        )

    @app.get("/", response_model=ServiceInfoResponse)
    def root() -> ServiceInfoResponse:
        return ServiceInfoResponse(service="fever-clinic-dp", docs="/docs")

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/v1/groups")
    def groups() -> dict:
        return {
            "streets": list(STREETS),
            "age_bands": list(AGE_BANDS),
            "epsilon": EPSILON,
            "sensitivity": SENSITIVITY,
            "cost_per_publication": COST_PER_PUBLICATION,
        }

    @app.post("/v1/imports", response_model=ImportResponse, status_code=201)
    def create_import(payload: ImportRequest) -> ImportResponse:
        result = service.import_week(payload.week, payload.records)
        return ImportResponse(**result)

    @app.get("/v1/imports/{version}", response_model=ImportResponse)
    def get_import(version: int) -> ImportResponse:
        result = service.get_import(version)
        return ImportResponse(
            version=result["version"],
            week=result["week"],
            record_count=result["record_count"],
        )

    @app.get("/v1/budget", response_model=BudgetResponse)
    def get_budget() -> BudgetResponse:
        return BudgetResponse(**service.budget())

    @app.post("/v1/publications", response_model=PublicationResponse)
    def publish(payload: PublishRequest) -> PublicationResponse:
        return PublicationResponse(**service.publish(payload.import_version))

    @app.get(
        "/v1/publications/{version}", response_model=PublicationResponse
    )
    def get_publication(version: int) -> PublicationResponse:
        return PublicationResponse(**service.get_publication(version))

    @app.get("/v1/publications", response_model=PublicationListResponse)
    def list_publications() -> PublicationListResponse:
        return PublicationListResponse(**service.list_publications())

    return app
