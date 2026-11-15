"""SQLite persistence with strict transactional and immutability guarantees.

Design notes
------------
* A patient's raw identifier never reaches the database.  Duplicate detection
  happens in memory on the submitted batch; only per-cell aggregate counts are
  persisted.
* Imports and publications are append-only at the storage layer (triggers), not
  merely by application convention.
* Budget state lives in the same SQLite database and is mutated only inside the
  same ``BEGIN IMMEDIATE`` transaction that inserts a publication.  A release
  and its budget charge therefore commit or roll back together.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    version      INTEGER PRIMARY KEY AUTOINCREMENT,
    week         TEXT    NOT NULL UNIQUE,
    record_count INTEGER NOT NULL CHECK (record_count > 0),
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS import_cells (
    import_version INTEGER NOT NULL REFERENCES imports(version),
    street         TEXT    NOT NULL,
    age_band       TEXT    NOT NULL,
    true_count     INTEGER NOT NULL CHECK (true_count >= 0),
    PRIMARY KEY (import_version, street, age_band)
);

CREATE TABLE IF NOT EXISTS publications (
    version        INTEGER PRIMARY KEY AUTOINCREMENT,
    import_version INTEGER NOT NULL UNIQUE REFERENCES imports(version),
    epsilon        REAL    NOT NULL CHECK (epsilon > 0),
    cost           INTEGER NOT NULL CHECK (cost > 0),
    created_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_cells (
    publication_version INTEGER NOT NULL REFERENCES publications(version),
    street              TEXT    NOT NULL,
    age_band            TEXT    NOT NULL,
    noisy_count         INTEGER NOT NULL,
    PRIMARY KEY (publication_version, street, age_band)
);

CREATE TABLE IF NOT EXISTS budget_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    total_budget INTEGER NOT NULL CHECK (total_budget >= 0),
    spent        INTEGER NOT NULL DEFAULT 0 CHECK (spent >= 0),
    CHECK (spent <= total_budget)
);

-- Imported weeks, their aggregate cells and every published release are
-- append-only.  Even a bug in the application layer (or a manual SQL client)
-- cannot rewrite them.
CREATE TRIGGER IF NOT EXISTS imports_no_update
BEFORE UPDATE ON imports
BEGIN
    SELECT RAISE(ABORT, 'imports are immutable');
END;

CREATE TRIGGER IF NOT EXISTS imports_no_delete
BEFORE DELETE ON imports
BEGIN
    SELECT RAISE(ABORT, 'imports are immutable');
END;

CREATE TRIGGER IF NOT EXISTS import_cells_no_update
BEFORE UPDATE ON import_cells
BEGIN
    SELECT RAISE(ABORT, 'imported data is immutable');
END;

CREATE TRIGGER IF NOT EXISTS import_cells_no_delete
BEFORE DELETE ON import_cells
BEGIN
    SELECT RAISE(ABORT, 'imported data is immutable');
END;

CREATE TRIGGER IF NOT EXISTS publications_no_update
BEFORE UPDATE ON publications
BEGIN
    SELECT RAISE(ABORT, 'publications are immutable');
END;

CREATE TRIGGER IF NOT EXISTS publications_no_delete
BEFORE DELETE ON publications
BEGIN
    SELECT RAISE(ABORT, 'publications are immutable');
END;

CREATE TRIGGER IF NOT EXISTS publication_cells_no_update
BEFORE UPDATE ON publication_cells
BEGIN
    SELECT RAISE(ABORT, 'publications are immutable');
END;

CREATE TRIGGER IF NOT EXISTS publication_cells_no_delete
BEFORE DELETE ON publication_cells
BEGIN
    SELECT RAISE(ABORT, 'publications are immutable');
END;
"""


class Database:
    """Thin SQLite helper: one short-lived connection per operation."""

    def __init__(self, path: str | Path, busy_timeout_seconds: float = 30.0) -> None:
        self.path = str(path)
        self.busy_timeout_seconds = busy_timeout_seconds

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_seconds,
            isolation_level=None,  # explicit transaction control
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1000)}")
        return conn

    def initialize(self, total_budget: int) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO budget_state (id, total_budget, spent) "
                "VALUES (1, ?, 0)",
                (total_budget,),
            )
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialized write transaction (``BEGIN IMMEDIATE``).

        SQLite acquires the database write lock before the first read, so a
        budget check and the corresponding publication insert cannot interleave
        with another writer.  Everything the block writes is one atomic unit:
        a failure rolls back both the release and the budget charge.
        """

        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            conn.close()
