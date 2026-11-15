"""Tests for the streaming, memory-bounded import endpoint.

A full week batch (~150k records, under the 200k limit) must validate and
import inside a small address space.  These tests cover the streaming parser
itself (chunk-boundary safety, trailing data, caps) and the end-to-end
guarantees: anything wrong anywhere in the batch rejects the whole import
and leaves neither a week version nor partial group truth behind.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.errors import (
    DuplicatePatientError,
    InvalidImportPayloadError,
    PayloadTooLargeError,
)
from app.groups import AGE_BANDS, STREETS
from app.import_parser import parse_import_payload
from app.main import create_app
from app.schemas import IMPORT_MAX_RECORDS

WEEK = "2026-W20"
CELLS = [(street, age_band) for street in STREETS for age_band in AGE_BANDS]
REPO_ROOT = Path(__file__).resolve().parent.parent

# The incident budget: a full week batch must import inside 160 MiB.
MEMORY_BUDGET_KIB = 160 * 1024


def make_records(n: int, street: str = "东城街道", age_band: str = "0-14"):
    return [
        {"patient_id": f"anon-{i:06d}", "street": street, "age_band": age_band}
        for i in range(n)
    ]


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture()
def client(db_path):
    app = create_app(database_path=db_path, total_budget=3)
    with TestClient(app) as c:
        yield c


def table_counts(path):
    with sqlite3.connect(path) as conn:
        imports = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
        cells = conn.execute("SELECT COUNT(*) FROM import_cells").fetchone()[0]
    return imports, cells


def parse_body(body: bytes, chunk_size: int = 65536, **kwargs):
    """Run the streaming parser over ``body`` split into fixed-size chunks."""

    async def chunks():
        for offset in range(0, len(body), chunk_size):
            yield body[offset : offset + chunk_size]

    return asyncio.run(parse_import_payload(chunks(), **kwargs))


class TestStreamingParser:
    def test_parses_one_byte_chunks(self):
        body = json.dumps(
            {
                "week": WEEK,
                "records": [
                    {"patient_id": "a", "street": "东城街道", "age_band": "0-14"},
                    {"patient_id": "b", "street": "西城街道", "age_band": "60+"},
                    {"patient_id": "c", "street": "东城街道", "age_band": "0-14"},
                ],
            }
        ).encode()
        week, counts, record_count = parse_body(body, chunk_size=1)
        assert week == WEEK
        assert record_count == 3
        assert counts == {("东城街道", "0-14"): 2, ("西城街道", "60+"): 1}

    def test_records_may_come_before_week(self):
        body = (
            '{"records": [{"patient_id": "a", "street": "东城街道", '
            '"age_band": "0-14"}], "week": "2026-W20"}'
        ).encode()
        week, counts, record_count = parse_body(body, chunk_size=7)
        assert week == WEEK
        assert record_count == 1
        assert counts == {("东城街道", "0-14"): 1}

    def test_unicode_and_escaped_strings(self):
        body = json.dumps(
            {
                "week": WEEK,
                "records": [
                    {"patient_id": "张\t三", "street": "开发区街道", "age_band": "45-59"}
                ],
            },
            ensure_ascii=False,
        ).encode()
        _, counts, record_count = parse_body(body, chunk_size=5)
        assert record_count == 1
        assert counts == {("开发区街道", "45-59"): 1}

    def test_trailing_data_after_document_rejected(self):
        body = (
            '{"week": "2026-W20", "records": [{"patient_id": "a", '
            '"street": "东城街道", "age_band": "0-14"}]} {"extra": true}'
        ).encode()
        with pytest.raises(InvalidImportPayloadError):
            parse_body(body, chunk_size=3)

    def test_trailing_garbage_bytes_rejected(self):
        body = (
            '{"week": "2026-W20", "records": [{"patient_id": "a", '
            '"street": "东城街道", "age_band": "0-14"}]}garbage'
        ).encode()
        with pytest.raises(InvalidImportPayloadError):
            parse_body(body)

    def test_unexpected_top_level_field_rejected(self):
        body = (
            '{"week": "2026-W20", "note": "x", "records": [{"patient_id": "a", '
            '"street": "东城街道", "age_band": "0-14"}]}'
        ).encode()
        with pytest.raises(InvalidImportPayloadError):
            parse_body(body)

    def test_missing_fields_rejected(self):
        with pytest.raises(InvalidImportPayloadError):
            parse_body(b"{}")
        with pytest.raises(InvalidImportPayloadError):
            parse_body(b'{"week": "2026-W20"}')
        with pytest.raises(InvalidImportPayloadError):
            parse_body(
                (
                    '{"records": [{"patient_id": "a", "street": "东城街道", '
                    '"age_band": "0-14"}]}'
                ).encode()
            )

    def test_record_with_extra_field_rejected(self):
        body = json.dumps(
            {
                "week": WEEK,
                "records": [
                    {
                        "patient_id": "a",
                        "street": "东城街道",
                        "age_band": "0-14",
                        "name": "张三",
                    }
                ],
            }
        ).encode()
        with pytest.raises(InvalidImportPayloadError):
            parse_body(body)

    def test_record_missing_field_rejected(self):
        body = (
            '{"week": "2026-W20", "records": [{"patient_id": "a", '
            '"street": "东城街道"}]}'
        ).encode()
        with pytest.raises(InvalidImportPayloadError):
            parse_body(body)

    def test_non_string_values_rejected(self):
        for record in (
            {"patient_id": 7, "street": "东城街道", "age_band": "0-14"},
            {"patient_id": "a", "street": ["东城街道"], "age_band": "0-14"},
            {"patient_id": "a", "street": "东城街道", "age_band": None},
        ):
            body = json.dumps({"week": WEEK, "records": [record]}).encode()
            with pytest.raises(InvalidImportPayloadError):
                parse_body(body)

    def test_malformed_json_rejected(self):
        for body in (
            b"",
            b"   ",
            b"{",
            b'{"week": "2026-W20", "records": [',
            b'{"week": "2026-W20", "records": [{"patient_id": "a}]}',
            b'{"week": "2026-W20", "records": [{"patient_id": "a",}]}',
            b"[1, 2, 3]",
        ):
            with pytest.raises(InvalidImportPayloadError):
                parse_body(body, chunk_size=2)

    def test_bad_week_rejected(self):
        for week in ("2026-20", "2026-W99", "W20", 202620):
            body = json.dumps(
                {
                    "week": week,
                    "records": [
                        {"patient_id": "a", "street": "东城街道", "age_band": "0-14"}
                    ],
                }
            ).encode()
            with pytest.raises(InvalidImportPayloadError):
                parse_body(body)

    def test_empty_records_rejected(self):
        with pytest.raises(InvalidImportPayloadError):
            parse_body(b'{"week": "2026-W20", "records": []}')

    def test_record_limit_enforced(self):
        records = ",".join(
            f'{{"patient_id": "id-{i}", "street": "东城街道", "age_band": "0-14"}}'
            for i in range(IMPORT_MAX_RECORDS + 1)
        )
        body = f'{{"week": "2026-W20", "records": [{records}]}}'.encode()
        with pytest.raises(InvalidImportPayloadError):
            parse_body(body, chunk_size=1024 * 1024)

    def test_body_size_cap_enforced(self):
        body = json.dumps(
            {
                "week": WEEK,
                "records": [
                    {"patient_id": "a", "street": "东城街道", "age_band": "0-14"}
                ],
            }
        ).encode()
        with pytest.raises(PayloadTooLargeError):
            parse_body(body, max_body_bytes=len(body) - 1)

    def test_duplicate_patient_rejected_by_parser(self):
        body = json.dumps(
            {
                "week": WEEK,
                "records": [
                    {"patient_id": "dup", "street": "东城街道", "age_band": "0-14"},
                    {"patient_id": "dup", "street": "西城街道", "age_band": "60+"},
                ],
            }
        ).encode()
        with pytest.raises(DuplicatePatientError):
            parse_body(body)

    def test_whitespace_only_patient_id_rejected(self):
        body = json.dumps(
            {
                "week": WEEK,
                "records": [
                    {"patient_id": "   ", "street": "东城街道", "age_band": "0-14"}
                ],
            }
        ).encode()
        with pytest.raises(InvalidImportPayloadError):
            parse_body(body)

    def test_patient_id_length_boundary(self):
        ok = json.dumps(
            {
                "week": WEEK,
                "records": [
                    {"patient_id": "x" * 200, "street": "东城街道", "age_band": "0-14"}
                ],
            }
        ).encode()
        _, _, record_count = parse_body(ok)
        assert record_count == 1

        too_long = json.dumps(
            {
                "week": WEEK,
                "records": [
                    {"patient_id": "x" * 201, "street": "东城街道", "age_band": "0-14"}
                ],
            }
        ).encode()
        with pytest.raises(InvalidImportPayloadError):
            parse_body(too_long)

    def test_records_must_be_an_array(self):
        for records in ('{}', '"x"', "12", "null"):
            body = f'{{"week": "2026-W20", "records": {records}}}'.encode()
            with pytest.raises(InvalidImportPayloadError):
                parse_body(body)


class TestLargeBatchImport:
    def test_full_week_batch_imports_within_memory_budget(self, client, db_path):
        # Run the 150k-record import in a clean subprocess so the peak-RSS
        # measurement is isolated from the pytest process.  The old
        # parse-everything implementation peaked at ~260 MiB RSS here.
        script = (
            "import resource, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from fastapi.testclient import TestClient\n"
            "from app.groups import STREETS, AGE_BANDS\n"
            "from app.main import create_app\n"
            "cells = [(s, a) for s in STREETS for a in AGE_BANDS]\n"
            "records = [\n"
            "    {'patient_id': f'anon-{i:06d}', 'street': cells[i % 25][0],\n"
            "     'age_band': cells[i % 25][1]}\n"
            "    for i in range(150_000)\n"
            "]\n"
            "app = create_app(database_path=sys.argv[2], total_budget=3)\n"
            "with TestClient(app) as c:\n"
            "    r = c.post('/v1/imports',\n"
            "               json={'week': '2026-W20', 'records': records})\n"
            "    assert r.status_code == 201, r.text\n"
            "    assert r.json()['record_count'] == 150_000\n"
            "peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
            "print(f'PEAK_RSS_KIB={peak}')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, str(REPO_ROOT), db_path],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert completed.returncode == 0, completed.stderr
        peak_line = completed.stdout.strip().splitlines()[-1]
        peak_kib = int(peak_line.removeprefix("PEAK_RSS_KIB="))
        assert peak_kib < MEMORY_BUDGET_KIB, (
            f"150k-record import peaked at {peak_kib // 1024} MiB, "
            f"over the 160 MiB budget"
        )

        # The immutable version and its fixed street x age-band truth exist.
        fetched = client.get("/v1/imports/1")
        assert fetched.status_code == 200
        assert fetched.json() == {
            "version": 1,
            "week": WEEK,
            "record_count": 150_000,
        }
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT street, age_band, true_count FROM import_cells "
                "WHERE import_version = 1"
            ).fetchall()
        assert len(rows) == 25
        per_cell = {(row[0], row[1]): row[2] for row in rows}
        assert set(per_cell) == set(CELLS)
        assert all(count == 6000 for count in per_cell.values())

        # Raw identifiers never reach the database.
        with sqlite3.connect(db_path) as conn:
            dump = "\n".join(conn.iterdump())
        assert "anon-" not in dump

        # The stored truth feeds the unchanged publication flow.
        published = client.post("/v1/publications", json={"import_version": 1})
        assert published.status_code == 200
        assert len(published.json()["counts"]) == 25

    def test_invalid_last_record_rejects_whole_batch(self, client, db_path):
        records = make_records(5_000)
        records.append(
            {
                "patient_id": "anon-tail",
                "street": "东城街道",
                "age_band": "0-14",
                "comment": "trailing field",
            }
        )
        response = client.post("/v1/imports", json={"week": WEEK, "records": records})
        assert response.status_code == 422
        assert table_counts(db_path) == (0, 0)
        assert client.get("/v1/imports/1").status_code == 404

    def test_bad_group_in_last_record_rejects_whole_batch(self, client, db_path):
        records = make_records(5_000)
        records.append(
            {"patient_id": "anon-tail", "street": "火星街道", "age_band": "0-14"}
        )
        response = client.post("/v1/imports", json={"week": WEEK, "records": records})
        assert response.status_code == 422
        assert table_counts(db_path) == (0, 0)

    def test_duplicate_in_last_record_rejects_whole_batch(self, client, db_path):
        records = make_records(5_000)
        records.append(
            {"patient_id": "anon-000000", "street": "西城街道", "age_band": "60+"}
        )
        response = client.post("/v1/imports", json={"week": WEEK, "records": records})
        assert response.status_code == 409
        assert response.json()["error"] == "duplicate_patient_in_week"
        assert "anon-000000" not in response.text
        assert table_counts(db_path) == (0, 0)

    def test_trailing_bytes_reject_whole_batch(self, client, db_path):
        payload = json.dumps({"week": WEEK, "records": make_records(10)}).encode()
        response = client.post(
            "/v1/imports",
            content=payload + b'\n{"unexpected": true}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert table_counts(db_path) == (0, 0)

    def test_extra_top_level_field_rejects_whole_batch(self, client, db_path):
        payload = {"week": WEEK, "records": make_records(3), "source": "cdc"}
        response = client.post("/v1/imports", json=payload)
        assert response.status_code == 422
        assert table_counts(db_path) == (0, 0)

    def test_oversized_content_length_rejected_before_reading(self, client, db_path):
        response = client.post(
            "/v1/imports",
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(65 * 1024 * 1024),
            },
        )
        assert response.status_code == 413
        assert response.json()["error"] == "payload_too_large"
        assert table_counts(db_path) == (0, 0)

    def test_failed_import_write_rolls_back_everything(self, db_path):
        def explode(_version: int) -> None:
            raise sqlite3.OperationalError("simulated disk failure")

        app = create_app(
            database_path=db_path, total_budget=3, import_before_commit=explode
        )
        with TestClient(app) as c:
            response = c.post(
                "/v1/imports", json={"week": WEEK, "records": make_records(4)}
            )
            assert response.status_code == 503
            assert response.json()["error"] == "storage_write_failed"
            assert c.get("/v1/imports/1").status_code == 404

        assert table_counts(db_path) == (0, 0)

        # The failed attempt left nothing behind: the same week imports
        # cleanly once the fault is gone, and the budget is untouched.
        healthy = create_app(database_path=db_path, total_budget=3)
        with TestClient(healthy) as c:
            ok = c.post("/v1/imports", json={"week": WEEK, "records": make_records(4)})
            assert ok.status_code == 201
            assert c.get("/v1/budget").json()["spent"] == 0
