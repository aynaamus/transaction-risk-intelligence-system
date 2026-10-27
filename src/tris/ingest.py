"""Memory-efficient, idempotent PaySim CSV ingestion into DuckDB.

DuckDB streams the CSV directly into a typed table; nothing is loaded into
Pandas. The source CSV is only ever opened for reading.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

import duckdb

from .schema import COLUMNS, LOG_TABLE, TABLE


class IngestionError(Exception):
    """Raised when a CSV cannot be ingested safely."""


@dataclass(frozen=True)
class IngestResult:
    status: str  # "ingested" or "skipped"
    rows: int
    sha256: str


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def check_disk_space(db_path: Path, csv_path: Path, factor: float = 2.0) -> None:
    """Fail early if free space near the DB is below factor x CSV size."""
    needed = int(csv_path.stat().st_size * factor)
    free = shutil.disk_usage(db_path.resolve().parent).free
    if free < needed:
        raise IngestionError(
            f"Insufficient disk space: {free / 1e9:.1f} GB free, "
            f"~{needed / 1e9:.1f} GB required."
        )


def check_header(con: duckdb.DuckDBPyConnection, csv_path: Path) -> None:
    """Require exactly the 11 PaySim columns (any order)."""
    try:
        rows = con.execute(
            f"DESCRIBE SELECT * FROM read_csv({_sql_str(str(csv_path))}, "
            "header=true, all_varchar=true)"
        ).fetchall()
    except duckdb.Error as e:
        raise IngestionError(f"Cannot read CSV header: {e}") from e
    found = [r[0] for r in rows]
    missing = [c for c in COLUMNS if c not in found]
    extra = [c for c in found if c not in COLUMNS]
    if missing or extra:
        raise IngestionError(
            f"Schema mismatch. Missing columns: {missing or 'none'}; "
            f"unexpected columns: {extra or 'none'}."
        )


def _ensure_log(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
            source_path VARCHAR, sha256 VARCHAR, size_bytes BIGINT,
            row_count BIGINT, ingested_at TIMESTAMP DEFAULT current_timestamp)"""
    )


def ingest(
    csv_path: str | Path, db_path: str | Path, replace: bool = False
) -> IngestResult:
    """Load the CSV into the `transactions` table.

    - Same file (by SHA-256) already loaded: skipped, no duplicates.
    - Different file already loaded: refused unless replace=True.
    - The table is rebuilt inside one transaction, so a failed load leaves
      any previous data intact.
    """
    csv_path, db_path = Path(csv_path), Path(db_path)
    if not csv_path.is_file():
        raise IngestionError(f"CSV not found: {csv_path}")
    if csv_path.resolve() == db_path.resolve():
        raise IngestionError("Database path must differ from the CSV path.")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    check_disk_space(db_path, csv_path)

    digest = _sha256(csv_path)
    con = duckdb.connect(str(db_path))
    try:
        check_header(con, csv_path)
        _ensure_log(con)
        exists = bool(
            con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                [TABLE],
            ).fetchone()[0]
        )
        if exists:
            prior = con.execute(
                f"SELECT sha256, row_count FROM {LOG_TABLE} ORDER BY ingested_at DESC LIMIT 1"
            ).fetchone()
            if prior and prior[0] == digest and not replace:
                return IngestResult("skipped", prior[1], digest)
            if not replace:
                raise IngestionError(
                    f"'{TABLE}' already holds data from a different source. "
                    "Re-run with --replace to rebuild it."
                )

        types = ", ".join(f"'{c}': '{t}'" for c, t in COLUMNS.items())
        cols = ", ".join(f'"{c}"' for c in COLUMNS)
        col_defs = ", ".join(f'"{c}" {t}' for c, t in COLUMNS.items())
        con.execute("BEGIN")
        try:
            con.execute(f"DROP TABLE IF EXISTS {TABLE}")
            con.execute(f"CREATE TABLE {TABLE} (row_id BIGINT NOT NULL, {col_defs})")
            con.execute(
                f"""INSERT INTO {TABLE}
                    SELECT row_number() OVER () AS row_id, {cols}
                    FROM read_csv({_sql_str(str(csv_path))}, header=true,
                                  columns={{{types}}})"""
            )
            rows = con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
            con.execute(
                f"INSERT INTO {LOG_TABLE} (source_path, sha256, size_bytes, row_count) "
                "VALUES (?, ?, ?, ?)",
                [str(csv_path), digest, csv_path.stat().st_size, rows],
            )
            con.execute("COMMIT")
        except duckdb.Error as e:
            con.execute("ROLLBACK")
            raise IngestionError(f"Malformed data, nothing was changed: {e}") from e
        return IngestResult("ingested", rows, digest)
    finally:
        con.close()
