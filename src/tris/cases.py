"""Investigation persistence: one case per alert, notes, append-only audit.

Cases reference the source transaction `row_id` (stable across detection
reruns) and live in their own tables, so rebuilding `alerts` never deletes
or invalidates them. Local single-user workflow: the analyst identifier is a
plain label supplied by the caller, not an authenticated identity.
"""

from __future__ import annotations

import duckdb

STATUSES = ("OPEN", "IN_REVIEW", "ESCALATED", "CLOSED")
DISPOSITIONS = ("NONE", "NO_FURTHER_ACTION", "REFER_FOR_REVIEW", "INCONCLUSIVE")


class CaseError(Exception):
    """Invalid case operation (bad input, missing record, rule violation)."""


def ensure_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE SEQUENCE IF NOT EXISTS seq_case START 1")
    con.execute("CREATE SEQUENCE IF NOT EXISTS seq_note START 1")
    con.execute("CREATE SEQUENCE IF NOT EXISTS seq_audit START 1")
    con.execute("""CREATE TABLE IF NOT EXISTS investigation_cases (
        case_id BIGINT PRIMARY KEY DEFAULT nextval('seq_case'),
        row_id BIGINT NOT NULL UNIQUE,
        status VARCHAR NOT NULL, disposition VARCHAR NOT NULL, finding VARCHAR NOT NULL DEFAULT '',
        created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, updated_by VARCHAR NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS case_notes (
        note_id BIGINT PRIMARY KEY DEFAULT nextval('seq_note'),
        case_id BIGINT NOT NULL, row_id BIGINT NOT NULL,
        created_at TIMESTAMP NOT NULL, analyst VARCHAR NOT NULL, note VARCHAR NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS case_audit (
        audit_id BIGINT PRIMARY KEY DEFAULT nextval('seq_audit'),
        case_id BIGINT NOT NULL, row_id BIGINT NOT NULL, ts TIMESTAMP NOT NULL,
        analyst VARCHAR NOT NULL, action VARCHAR NOT NULL, field VARCHAR,
        old_value VARCHAR, new_value VARCHAR)""")


def _analyst(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 64:
        raise CaseError("Analyst identifier is required (1-64 characters).")
    return name


def _audit(con, case_id, row_id, analyst, action, field=None, old=None, new=None):
    con.execute(
        """INSERT INTO case_audit (case_id, row_id, ts, analyst, action, field, old_value, new_value)
           VALUES (?, ?, current_timestamp, ?, ?, ?, ?, ?)""",
        [case_id, row_id, analyst, action, field, old, new],
    )


def get_case(con, row_id: int) -> dict | None:
    cur = con.execute("SELECT * FROM investigation_cases WHERE row_id = ?", [row_id])
    r = cur.fetchone()
    return dict(zip([d[0] for d in cur.description], r)) if r else None


def open_case(con, row_id: int, analyst: str) -> dict:
    """Create the case for an alert, or return the existing one (never duplicates)."""
    analyst = _analyst(analyst)
    con.execute("BEGIN")
    try:
        existing = get_case(con, row_id)
        if existing:
            con.execute("COMMIT")
            return existing
        if con.execute("SELECT count(*) FROM alerts WHERE row_id = ?", [row_id]).fetchone()[0] == 0:
            raise CaseError(f"No alert exists for transaction row_id {row_id}.")
        con.execute(
            """INSERT INTO investigation_cases
               (row_id, status, disposition, created_at, updated_at, updated_by)
               VALUES (?, 'OPEN', 'NONE', current_timestamp, current_timestamp, ?)""",
            [row_id, analyst],
        )
        case = get_case(con, row_id)
        _audit(con, case["case_id"], row_id, analyst, "CASE_OPENED", "status", None, "OPEN")
        con.execute("COMMIT")
        return case
    except Exception:
        con.execute("ROLLBACK")
        raise


def update_case(con, row_id: int, analyst: str, status=None, disposition=None, finding=None) -> dict:
    """Change status / disposition / finding; each actual change is audited with old and new value."""
    analyst = _analyst(analyst)
    if status is not None and status not in STATUSES:
        raise CaseError(f"Invalid status: {status}")
    if disposition is not None and disposition not in DISPOSITIONS:
        raise CaseError(f"Invalid disposition: {disposition}")
    con.execute("BEGIN")
    try:
        case = get_case(con, row_id)
        if case is None:
            raise CaseError(f"No case exists for row_id {row_id}.")
        new = {"status": status, "disposition": disposition,
               "finding": finding.strip() if finding is not None else None}
        changes = {k: v for k, v in new.items() if v is not None and v != case[k]}
        final_status = changes.get("status", case["status"])
        final_disp = changes.get("disposition", case["disposition"])
        if final_status == "CLOSED" and final_disp == "NONE":
            raise CaseError("A case can only be closed with a disposition.")
        for field, value in changes.items():
            con.execute(
                f"UPDATE investigation_cases SET {field} = ?, updated_at = current_timestamp, "
                "updated_by = ? WHERE case_id = ?",
                [value, analyst, case["case_id"]],
            )
            _audit(con, case["case_id"], row_id, analyst, "FIELD_CHANGED", field, case[field], value)
        con.execute("COMMIT")
        return get_case(con, row_id)
    except Exception:
        con.execute("ROLLBACK")
        raise


def add_note(con, row_id: int, analyst: str, note: str) -> None:
    analyst, note = _analyst(analyst), (note or "").strip()
    if not note:
        raise CaseError("Note text is required.")
    con.execute("BEGIN")
    try:
        case = get_case(con, row_id)
        if case is None:
            raise CaseError(f"No case exists for row_id {row_id}.")
        con.execute(
            "INSERT INTO case_notes (case_id, row_id, created_at, analyst, note) "
            "VALUES (?, ?, current_timestamp, ?, ?)",
            [case["case_id"], row_id, analyst, note],
        )
        _audit(con, case["case_id"], row_id, analyst, "NOTE_ADDED", "note", None, note)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def _all(con, sql, params):
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def list_notes(con, row_id: int) -> list[dict]:
    return _all(con, "SELECT created_at, analyst, note FROM case_notes WHERE row_id = ? ORDER BY note_id", [row_id])


def list_audit(con, row_id: int) -> list[dict]:
    return _all(con, "SELECT ts, analyst, action, field, old_value, new_value FROM case_audit "
                     "WHERE row_id = ? ORDER BY audit_id", [row_id])
