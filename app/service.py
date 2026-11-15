"""Application service: imports, release publication and budget accounting."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Iterable

from .db import Database
from .errors import (
    BudgetExhaustedError,
    DuplicatePatientError,
    ImportNotFoundError,
    PublicationNotFoundError,
    StorageError,
    WeekAlreadyImportedError,
)
from .groups import AGE_BANDS, COST_PER_PUBLICATION, EPSILON, STREETS
from .noise import noisy_count
from .schemas import CountCell, PatientRecordIn

# Optional test hook invoked after all publication rows are written but before
# COMMIT, letting tests prove that a failing write leaves nothing behind.
BeforeCommitHook = Callable[[int], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReleaseService:
    """All state transitions used by the HTTP layer.

    Every public method is safe to call concurrently from multiple threads or
    processes sharing the same SQLite file.
    """

    def __init__(
        self,
        db: Database,
        total_budget: int,
        cost_per_publication: int = COST_PER_PUBLICATION,
        epsilon: float = EPSILON,
        before_commit: BeforeCommitHook | None = None,
    ) -> None:
        if total_budget < 0:
            raise ValueError("total_budget must be non-negative")
        if cost_per_publication <= 0:
            raise ValueError("cost_per_publication must be positive")
        if epsilon <= 0:
            raise ValueError("epsilon must be positive")
        self.db = db
        self.total_budget = total_budget
        self.cost_per_publication = cost_per_publication
        self.epsilon = epsilon
        self._before_commit = before_commit

    # ------------------------------------------------------------------ #
    # Imports
    # ------------------------------------------------------------------ #
    def import_week(self, week: str, records: Iterable[PatientRecordIn]) -> dict:
        """Validate a week's records atomically and store an immutable version.

        If any de-identified patient number occurs more than once in the batch,
        the entire batch is rejected and nothing is written.  Raw identifiers
        are never persisted and are never echoed in error messages.
        """

        counts: dict[tuple[str, str], int] = defaultdict(int)
        seen: set[str] = set()
        record_count = 0

        for record in records:
            if record.patient_id in seen:
                raise DuplicatePatientError(
                    "the same de-identified patient number appears more than "
                    "once in this week; the entire batch was rejected"
                )
            seen.add(record.patient_id)
            counts[(record.street, record.age_band)] += 1
            record_count += 1

        if record_count == 0:
            # Unreachable through the HTTP schema; keep the service honest.
            raise ValueError("a week import must contain at least one record")

        try:
            with self.db.transaction() as conn:
                existing = conn.execute(
                    "SELECT version FROM imports WHERE week = ?", (week,)
                ).fetchone()
                if existing is not None:
                    raise WeekAlreadyImportedError(
                        f"week {week} has already been imported as version "
                        f"{existing['version']}; imported versions are immutable"
                    )

                cursor = conn.execute(
                    "INSERT INTO imports (week, record_count, created_at) "
                    "VALUES (?, ?, ?)",
                    (week, record_count, _now()),
                )
                version = int(cursor.lastrowid)

                conn.executemany(
                    "INSERT INTO import_cells "
                    "(import_version, street, age_band, true_count) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (version, street, age_band, counts.get((street, age_band), 0))
                        for street in STREETS
                        for age_band in AGE_BANDS
                    ],
                )
        except sqlite3.Error as exc:
            raise StorageError(
                "the import could not be stored; no data was written"
            ) from exc

        return {"version": version, "week": week, "record_count": record_count}

    def get_import(self, version: int) -> dict:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT version, week, record_count FROM imports WHERE version = ?",
                (version,),
            ).fetchone()
        if row is None:
            raise ImportNotFoundError(f"import version {version} does not exist")
        return {
            "version": int(row["version"]),
            "week": row["week"],
            "record_count": int(row["record_count"]),
        }

    # ------------------------------------------------------------------ #
    # Budget
    # ------------------------------------------------------------------ #
    @staticmethod
    def _budget_from(conn: sqlite3.Connection) -> tuple[int, int, int]:
        row = conn.execute(
            "SELECT total_budget, spent FROM budget_state WHERE id = 1"
        ).fetchone()
        if row is None:
            raise StorageError("budget state is missing")
        total, spent = int(row["total_budget"]), int(row["spent"])
        return total, spent, total - spent

    def budget(self) -> dict:
        with self.db.read() as conn:
            total, spent, remaining = self._budget_from(conn)
        return {
            "total_budget": total,
            "spent": spent,
            "remaining_budget": remaining,
        }

    # ------------------------------------------------------------------ #
    # Publications
    # ------------------------------------------------------------------ #
    def publish(self, import_version: int) -> dict:
        """Publish (or re-fetch) the noisy release for an imported week.

        * Every fixed street/age-band cell is released, including zero counts.
        * Each cell gets Laplace(0, 1/epsilon) noise; epsilon=1, sensitivity=1.
        * The first publication of a version spends one budget unit.  A repeat
          request returns the stored release and spends nothing.
        * The budget charge and the release rows are written in one
          ``BEGIN IMMEDIATE`` transaction, so an exhausted budget or a failed
          write leaves neither a publication nor a spent budget unit behind.
        """

        # Cheap read-only replay path: no write lock, no budget movement.
        replay = self._existing_publication(import_version)
        if replay is not None:
            return replay

        try:
            with self.db.transaction() as conn:
                # Re-check under the write lock; another request may have just
                # created this release.
                existing = conn.execute(
                    "SELECT version FROM publications WHERE import_version = ?",
                    (import_version,),
                ).fetchone()
                if existing is not None:
                    total, spent, _ = self._budget_from(conn)
                    version = int(existing["version"])
                    return {
                        "version": version,
                        "counts": self._cells_from(conn, version),
                        "remaining_budget": total - spent,
                    }

                imported = conn.execute(
                    "SELECT version FROM imports WHERE version = ?",
                    (import_version,),
                ).fetchone()
                if imported is None:
                    raise ImportNotFoundError(
                        f"import version {import_version} does not exist"
                    )

                _, _, remaining = self._budget_from(conn)
                if remaining < self.cost_per_publication:
                    raise BudgetExhaustedError(
                        "the global privacy budget is exhausted; no publication "
                        "was created and no budget was spent"
                    )

                cursor = conn.execute(
                    "INSERT INTO publications "
                    "(import_version, epsilon, cost, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (import_version, self.epsilon, self.cost_per_publication, _now()),
                )
                publication_version = int(cursor.lastrowid)

                true_by_cell = {
                    (row["street"], row["age_band"]): int(row["true_count"])
                    for row in conn.execute(
                        "SELECT street, age_band, true_count FROM import_cells "
                        "WHERE import_version = ?",
                        (import_version,),
                    ).fetchall()
                }

                conn.executemany(
                    "INSERT INTO publication_cells "
                    "(publication_version, street, age_band, noisy_count) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (
                            publication_version,
                            street,
                            age_band,
                            noisy_count(
                                true_by_cell.get((street, age_band), 0), self.epsilon
                            ),
                        )
                        for street in STREETS
                        for age_band in AGE_BANDS
                    ],
                )

                # Conditional update: a second safety net against overspend.
                charged = conn.execute(
                    "UPDATE budget_state SET spent = spent + ? "
                    "WHERE id = 1 AND spent + ? <= total_budget",
                    (self.cost_per_publication, self.cost_per_publication),
                )
                if charged.rowcount != 1:
                    raise BudgetExhaustedError(
                        "the global privacy budget is exhausted; no publication "
                        "was created and no budget was spent"
                    )

                if self._before_commit is not None:
                    self._before_commit(import_version)

                return {
                    "version": publication_version,
                    "counts": self._cells_from(conn, publication_version),
                    "remaining_budget": remaining - self.cost_per_publication,
                }
        except sqlite3.Error as exc:
            raise StorageError(
                "the publication could not be stored; no release was created "
                "and no budget was spent"
            ) from exc

    def get_publication(self, version: int) -> dict:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT version FROM publications WHERE version = ?", (version,)
            ).fetchone()
            if row is None:
                raise PublicationNotFoundError(
                    f"publication version {version} does not exist"
                )
            total, spent, _ = self._budget_from(conn)
            return {
                "version": version,
                "counts": self._cells_from(conn, version),
                "remaining_budget": total - spent,
            }

    def list_publications(self) -> dict:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT version, import_version FROM publications ORDER BY version"
            ).fetchall()
            total, spent, _ = self._budget_from(conn)
            return {
                "publications": [
                    {
                        "version": int(row["version"]),
                        "import_version": int(row["import_version"]),
                        "counts": self._cells_from(conn, int(row["version"])),
                    }
                    for row in rows
                ],
                "remaining_budget": total - spent,
            }

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _existing_publication(self, import_version: int) -> dict | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT version FROM publications WHERE import_version = ?",
                (import_version,),
            ).fetchone()
            if row is None:
                return None
            total, spent, _ = self._budget_from(conn)
            version = int(row["version"])
            return {
                "version": version,
                "counts": self._cells_from(conn, version),
                "remaining_budget": total - spent,
            }

    @staticmethod
    def _cells_from(
        conn: sqlite3.Connection, publication_version: int
    ) -> list[CountCell]:
        rows = conn.execute(
            "SELECT street, age_band, noisy_count FROM publication_cells "
            "WHERE publication_version = ?",
            (publication_version,),
        ).fetchall()
        by_cell = {
            (row["street"], row["age_band"]): int(row["noisy_count"]) for row in rows
        }
        return [
            CountCell(
                street=street,
                age_band=age_band,
                count=by_cell.get((street, age_band), 0),
            )
            for street in STREETS
            for age_band in AGE_BANDS
        ]
