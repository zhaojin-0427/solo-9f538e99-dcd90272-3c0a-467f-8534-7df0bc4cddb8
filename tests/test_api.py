"""End-to-end API tests for the fever-clinic DP release service."""

from __future__ import annotations

import os
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app.groups import AGE_BANDS, STREETS
from app.main import create_app

WEEK = "2026-W20"


def make_records(n: int = 5, street: str = "东城街道", age_band: str = "0-14"):
    return [
        {"patient_id": f"p-{i}", "street": street, "age_band": age_band}
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


def import_week(client, week=WEEK, records=None):
    payload = {"week": week, "records": records or make_records()}
    return client.post("/v1/imports", json=payload)


class TestGroups:
    def test_fixed_public_groups_are_exposed(self, client):
        body = client.get("/v1/groups").json()
        assert body["streets"] == list(STREETS)
        assert body["age_bands"] == list(AGE_BANDS)
        assert body["epsilon"] == 1.0
        assert body["sensitivity"] == 1
        assert body["cost_per_publication"] == 1


class TestImports:
    def test_import_creates_immutable_version(self, client):
        response = import_week(client)
        assert response.status_code == 201
        body = response.json()
        assert body["version"] == 1
        assert body["week"] == WEEK
        assert body["record_count"] == 5

        fetched = client.get("/v1/imports/1").json()
        assert fetched == body

    def test_duplicate_patient_rejects_whole_batch(self, client, db_path):
        records = make_records(3) + [
            {"patient_id": "p-0", "street": "西城街道", "age_band": "15-29"}
        ]
        response = client.post(
            "/v1/imports", json={"week": WEEK, "records": records}
        )
        assert response.status_code == 409
        assert response.json()["error"] == "duplicate_patient_in_week"

        # Nothing was written.
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0
        assert client.get("/v1/imports/1").status_code == 404

    def test_duplicate_patient_error_does_not_leak_identifier(self, client):
        records = make_records(2)
        records.append(dict(records[0]))
        response = client.post(
            "/v1/imports", json={"week": WEEK, "records": records}
        )
        assert response.status_code == 409
        assert records[0]["patient_id"] not in response.text

    def test_same_week_cannot_be_imported_twice(self, client):
        assert import_week(client).status_code == 201
        second = client.post(
            "/v1/imports",
            json={"week": WEEK, "records": make_records(1, street="西城街道")},
        )
        assert second.status_code == 409
        assert second.json()["error"] == "week_already_imported"

    def test_unknown_street_or_age_band_rejected(self, client):
        bad_street = client.post(
            "/v1/imports",
            json={
                "week": WEEK,
                "records": [
                    {"patient_id": "x", "street": "火星街道", "age_band": "0-14"}
                ],
            },
        )
        assert bad_street.status_code == 422
        bad_age = client.post(
            "/v1/imports",
            json={
                "week": WEEK,
                "records": [
                    {"patient_id": "x", "street": "东城街道", "age_band": "7-9"}
                ],
            },
        )
        assert bad_age.status_code == 422

    def test_empty_batch_and_bad_week_rejected(self, client):
        assert client.post(
            "/v1/imports", json={"week": WEEK, "records": []}
        ).status_code == 422
        assert client.post(
            "/v1/imports", json={"week": "2026-20", "records": make_records(1)}
        ).status_code == 422

    def test_unknown_field_rejected(self, client):
        record = {"patient_id": "x", "street": "东城街道", "age_band": "0-14",
                  "name": "张三"}
        response = client.post(
            "/v1/imports", json={"week": WEEK, "records": [record]}
        )
        assert response.status_code == 422


class TestPublications:
    def test_publication_covers_all_cells_and_spends_one_budget(self, client):
        import_week(client)
        response = client.post("/v1/publications", json={"import_version": 1})
        assert response.status_code == 200
        body = response.json()

        assert body["version"] == 1
        assert body["remaining_budget"] == 2
        assert len(body["counts"]) == len(STREETS) * len(AGE_BANDS)
        assert {(c["street"], c["age_band"]) for c in body["counts"]} == {
            (s, a) for s in STREETS for a in AGE_BANDS
        }
        # Only the noisy count, version and budget are exposed; never the true
        # count or raw records.
        assert set(body) == {"version", "counts", "remaining_budget"}
        assert set(body["counts"][0]) == {"street", "age_band", "count"}

        budget = client.get("/v1/budget").json()
        assert budget == {"total_budget": 3, "spent": 1, "remaining_budget": 2}

    def test_repeat_publication_is_idempotent_and_free(self, client):
        import_week(client)
        first = client.post("/v1/publications", json={"import_version": 1}).json()
        second = client.post("/v1/publications", json={"import_version": 1}).json()
        assert second == first
        assert client.get("/v1/budget").json()["spent"] == 1

    def test_budget_exhaustion_blocks_new_publication(self, client):
        for week in ("2026-W20", "2026-W21", "2026-W22", "2026-W23"):
            import_week(client, week=week)
        for version in (1, 2, 3):
            assert client.post(
                "/v1/publications", json={"import_version": version}
            ).status_code == 200

        blocked = client.post("/v1/publications", json={"import_version": 4})
        assert blocked.status_code == 409
        assert blocked.json()["error"] == "privacy_budget_exhausted"

        # No fourth publication exists.
        assert client.get("/v1/publications/4").status_code == 404
        assert len(client.get("/v1/publications").json()["publications"]) == 3

        # Existing releases remain readable.
        assert client.get("/v1/publications/2").status_code == 200

        # Replaying an already published version still works and is free.
        replay = client.post("/v1/publications", json={"import_version": 2})
        assert replay.status_code == 200
        assert replay.json()["remaining_budget"] == 0

    def test_publication_of_missing_import_is_404_and_free(self, client):
        response = client.post("/v1/publications", json={"import_version": 99})
        assert response.status_code == 404
        assert client.get("/v1/budget").json()["spent"] == 0

    def test_failed_write_rolls_back_release_and_budget(self, db_path):
        def explode(_import_version: int) -> None:
            raise sqlite3.OperationalError("simulated disk failure")

        app = create_app(
            database_path=db_path, total_budget=3, publish_before_commit=explode
        )
        with TestClient(app) as c:
            assert import_week(c).status_code == 201

            response = c.post("/v1/publications", json={"import_version": 1})
            assert response.status_code == 503
            assert response.json()["error"] == "storage_write_failed"

            assert c.get("/v1/budget").json()["spent"] == 0
            assert c.get("/v1/publications/1").status_code == 404

        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM publication_cells"
            ).fetchone()[0] == 0

        # Without the fault the same publication succeeds and charges once.
        healthy = create_app(database_path=db_path, total_budget=3)
        with TestClient(healthy) as c:
            ok = c.post("/v1/publications", json={"import_version": 1})
            assert ok.status_code == 200
            assert ok.json()["remaining_budget"] == 2

    def test_publication_validation(self, client):
        assert client.post(
            "/v1/publications", json={"import_version": 0}
        ).status_code == 422
        assert client.post(
            "/v1/publications", json={}
        ).status_code == 422

    def test_failed_write_leaves_earlier_release_intact(self, db_path):
        def explode(_import_version: int) -> None:
            raise sqlite3.OperationalError("simulated disk failure")

        app = create_app(
            database_path=db_path, total_budget=3, publish_before_commit=explode
        )
        with TestClient(app) as c:
            import_week(c, week="2026-W20")
            import_week(c, week="2026-W21", records=make_records(2, "西城街道"))
            # Cannot publish at all while the fault is armed, so seed the
            # healthy release through a separate app instance first.
            assert c.get("/v1/publications/1").status_code == 404

        healthy = create_app(database_path=db_path, total_budget=3)
        with TestClient(healthy) as c:
            seeded = c.post("/v1/publications", json={"import_version": 1}).json()

        failed = create_app(
            database_path=db_path, total_budget=3, publish_before_commit=explode
        )
        with TestClient(failed) as c:
            response = c.post("/v1/publications", json={"import_version": 2})
            assert response.status_code == 503
            assert c.get("/v1/budget").json()["spent"] == 1
            # The earlier release is unchanged and still readable.
            assert c.get("/v1/publications/1").json()["counts"] == seeded["counts"]
            assert c.get("/v1/publications/2").status_code == 404


class TestConcurrency:
    def test_concurrent_publications_never_overspend(self, db_path):
        app = create_app(database_path=db_path, total_budget=3)
        for i in range(6):
            week = f"2026-W{20 + i:02d}"
            with TestClient(app) as setup:
                assert import_week(setup, week=week, records=make_records(2)).status_code == 201

        statuses: list[tuple[int, int | None]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def release(version: int) -> None:
            with TestClient(app) as c:
                barrier.wait()
                response = c.post(
                    "/v1/publications", json={"import_version": version}
                )
                with lock:
                    statuses.append(
                        (
                            response.status_code,
                            response.json().get("remaining_budget"),
                        )
                    )

        threads = [threading.Thread(target=release, args=(v,)) for v in range(1, 7)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [s for s in statuses if s[0] == 200]
        conflicts = [s for s in statuses if s[0] == 409]
        assert len(successes) == 3
        assert len(conflicts) == 3
        assert sorted(remaining for _, remaining in successes) == [0, 1, 2]

        with TestClient(app) as c:
            assert c.get("/v1/budget").json()["spent"] == 3
            assert len(c.get("/v1/publications").json()["publications"]) == 3


class TestStorageInvariants:
    def test_database_rejects_rewrites(self, client, db_path):
        import_week(client)
        client.post("/v1/publications", json={"import_version": 1})

        with sqlite3.connect(db_path) as conn:
            for statement in (
                "UPDATE imports SET week = '2026-W21' WHERE version = 1",
                "DELETE FROM imports WHERE version = 1",
                "UPDATE import_cells SET true_count = 0",
                "DELETE FROM import_cells",
                "UPDATE publications SET cost = 0",
                "DELETE FROM publications",
                "UPDATE publication_cells SET noisy_count = 0",
                "DELETE FROM publication_cells",
            ):
                with pytest.raises(sqlite3.DatabaseError):
                    conn.execute(statement)

    def test_raw_patient_ids_are_not_stored(self, client, db_path):
        import_week(client, records=make_records(4))
        with sqlite3.connect(db_path) as conn:
            dump = "\n".join(conn.iterdump())
        assert "p-0" not in dump
        assert "p-1" not in dump
