"""Uvicorn entry point: ``uvicorn app.entrypoint:app``."""

from __future__ import annotations

from .main import create_app

app = create_app()
