"""Database-backed alert retrieval for the investigator interface.

Filtering, sorting and pagination run in DuckDB; only one page is ever
materialised. These queries never select isFraud, isFlaggedFraud or the
balance columns, so labels cannot influence alert prioritisation.
"""

from __future__ import annotations

import duckdb

from .detect import RULE_WEIGHTS
from .schema import TABLE

RULE_DESCRIPTIONS = {
    "A_LARGE_AMOUNT": "Amount exceeds the per-type p99 of reference-period (steps 1-400) amounts.",
    "B_DEST_CONCENTRATION": "Unusually many inbound transactions to the same destination account within 6 simulated hours.",
    "C_RAPID_ONWARD": "The origin account received a transfer and sent 80-100% of it onward within 6 simulated hours (linked by shared account id only).",
}

SORT_COLUMNS = {
    "risk_score": "a.risk_score",
    "amount": "t.amount",
    "step": "t.step",
    "row_id": "a.row_id",
    "type": "t.type",
    "severity": "a.risk_score",
}
SEVERITIES = ("HIGH", "MEDIUM", "LOW")
STATUS_NEW = "NEW"  # alert with no case yet

PAGE_SIZE_MAX = 500


def _where(rules, severities, step_range, types, statuses):
    clauses, params = [], []
    if rules:
        clauses.append("list_has_any(a.rules, ?)")
        params.append(list(rules))
    if severities:
        clauses.append("a.severity IN (SELECT unnest(?))")
        params.append(list(severities))
    if step_range:
        clauses.append("t.step BETWEEN ? AND ?")
        params += [int(step_range[0]), int(step_range[1])]
    if types:
        clauses.append("t.type IN (SELECT unnest(?))")
        params.append(list(types))
    if statuses:
        clauses.append(f"coalesce(c.status, '{STATUS_NEW}') IN (SELECT unnest(?))")
        params.append(list(statuses))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def query_alerts(
    con: duckdb.DuckDBPyConnection,
    rules=None, severities=None, step_range=None, types=None, statuses=None,
    sort_by: str = "risk_score", descending: bool = True,
    page: int = 1, page_size: int = 50,
) -> tuple[list[dict], int]:
    """Return (one page of alert rows, total matching rows)."""
    if sort_by not in SORT_COLUMNS:
        raise ValueError(f"Unsupported sort column: {sort_by}")
    page, page_size = max(1, int(page)), min(max(1, int(page_size)), PAGE_SIZE_MAX)
    where, params = _where(rules, severities, step_range, types, statuses)
    frm = f"""FROM alerts a JOIN {TABLE} t USING (row_id)
              LEFT JOIN investigation_cases c ON c.row_id = a.row_id"""
    total = con.execute(f"SELECT count(*) {frm}{where}", params).fetchone()[0]
    direction = "DESC" if descending else "ASC"
    cur = con.execute(
        f"""SELECT a.row_id, t.step, t.type, t.amount, t.nameOrig, t.nameDest,
                   a.risk_score, a.severity, array_to_string(a.rules, ', ') AS rules,
                   coalesce(c.status, '{STATUS_NEW}') AS status
            {frm}{where}
            ORDER BY {SORT_COLUMNS[sort_by]} {direction}, a.row_id
            LIMIT {page_size} OFFSET {(page - 1) * page_size}""",
        params,
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()], total


def filter_options(con) -> dict:
    lo, hi = con.execute(f"SELECT min(step), max(step) FROM {TABLE}").fetchone()
    return {
        "rules": list(RULE_WEIGHTS),
        "severities": list(SEVERITIES),
        "types": [r[0] for r in con.execute(f"SELECT DISTINCT type FROM {TABLE} ORDER BY 1").fetchall()],
        "step_min": lo, "step_max": hi,
    }


def _rows(con, sql, params):
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


TX_COLS = "row_id, step, type, amount, nameOrig, nameDest"  # deliberately no labels/balances


def alert_detail(con, row_id: int) -> dict | None:
    """Transaction, score, per-rule evidence and linked transactions; None if no such alert."""
    alert = _rows(con, "SELECT row_id, risk_score, severity, rules, n_rules FROM alerts WHERE row_id = ?", [row_id])
    if not alert:
        return None
    tx = _rows(con, f"SELECT {TX_COLS} FROM {TABLE} WHERE row_id = ?", [row_id])[0]
    evidence = _rows(
        con,
        """SELECT DISTINCT rule, threshold, observed, detail, related_row_id
           FROM alert_evidence WHERE row_id = ? ORDER BY rule, related_row_id""",
        [row_id],
    )
    for e in evidence:
        e["description"] = RULE_DESCRIPTIONS.get(e["rule"], "")
    linked_ids = sorted({e["related_row_id"] for e in evidence if e["related_row_id"] is not None})
    linked = _rows(con, f"SELECT {TX_COLS} FROM {TABLE} WHERE row_id IN (SELECT unnest(?)) ORDER BY row_id",
                   [linked_ids]) if linked_ids else []
    return {"alert": alert[0], "transaction": tx, "evidence": evidence, "linked_transactions": linked}


def same_account_transactions(con, row_id: int, limit: int = 20) -> list[dict]:
    """Other observed transactions sharing an origin or destination account id.

    A shared identifier is the only relationship shown; it implies nothing about
    ownership, control or coordination.
    """
    return _rows(
        con,
        f"""SELECT o.{TX_COLS.replace(', ', ', o.')} FROM {TABLE} o JOIN {TABLE} t
              ON (o.nameOrig = t.nameOrig OR o.nameDest = t.nameDest
                  OR o.nameOrig = t.nameDest OR o.nameDest = t.nameOrig)
            WHERE t.row_id = ? AND o.row_id <> t.row_id
            ORDER BY o.step, o.row_id LIMIT {int(limit)}""",
        [row_id],
    )
