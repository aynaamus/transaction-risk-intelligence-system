"""SQL-based validation of the ingested PaySim table."""

from __future__ import annotations

import duckdb

from .schema import COLUMNS, CRITICAL_COLUMNS, KNOWN_TYPES, TABLE


def _one(con, sql, params=None):
    return con.execute(sql, params or []).fetchone()


def run_validation(con: duckdb.DuckDBPyConnection) -> dict:
    """Run all checks; returns a dict with `problems` (empty means pass)."""
    problems: list[str] = []
    r: dict = {"problems": problems}

    # Required columns and compatible types.
    actual = dict(
        con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ?",
            [TABLE],
        ).fetchall()
    )
    if not actual:
        problems.append(f"Table '{TABLE}' does not exist.")
        return r
    for col, typ in COLUMNS.items():
        if col not in actual:
            problems.append(f"Missing column: {col}")
        elif actual[col] != typ:
            problems.append(f"Column {col} has type {actual[col]}, expected {typ}")
    if problems:
        return r

    r["total"] = _one(con, f"SELECT count(*) FROM {TABLE}")[0]
    if r["total"] == 0:
        problems.append("Table is empty.")
        return r

    # Missing / malformed critical values.
    nulls = {}
    for c in CRITICAL_COLUMNS:
        n = _one(con, f'SELECT count(*) FROM {TABLE} WHERE "{c}" IS NULL')[0]
        if n:
            nulls[c] = n
            problems.append(f"{n} NULL values in {c}")
    r["nulls"] = nulls
    checks = {
        "empty nameOrig/nameDest/type": f"""SELECT count(*) FROM {TABLE}
            WHERE trim(nameOrig) = '' OR trim(nameDest) = '' OR trim(type) = ''""",
        "step < 0": f"SELECT count(*) FROM {TABLE} WHERE step < 0",
        "negative amount": f"SELECT count(*) FROM {TABLE} WHERE amount < 0",
        "non-finite amount": f"SELECT count(*) FROM {TABLE} WHERE NOT isfinite(amount)",
        "isFraud not in (0,1)": f"SELECT count(*) FROM {TABLE} WHERE isFraud NOT IN (0,1)",
        "isFlaggedFraud not in (0,1)": f"SELECT count(*) FROM {TABLE} WHERE isFlaggedFraud NOT IN (0,1)",
    }
    for label, sql in checks.items():
        n = _one(con, sql)[0]
        if n:
            problems.append(f"{n} rows with {label}")

    # Distributions.
    r["fraud"] = con.execute(
        f"SELECT isFraud, count(*) FROM {TABLE} GROUP BY 1 ORDER BY 1"
    ).fetchall()
    r["flagged"] = con.execute(
        f"SELECT isFlaggedFraud, count(*) FROM {TABLE} GROUP BY 1 ORDER BY 1"
    ).fetchall()
    r["types"] = con.execute(
        f"""SELECT type, count(*) n, sum(isFraud) fraud FROM {TABLE}
            GROUP BY 1 ORDER BY n DESC"""
    ).fetchall()
    for t, _, _ in r["types"]:
        if t not in KNOWN_TYPES:
            problems.append(f"Unexpected transaction type: {t}")
    r["step"] = _one(con, f"SELECT min(step), max(step), count(DISTINCT step) FROM {TABLE}")
    r["amount"] = _one(
        con,
        f"""SELECT min(amount), max(amount), avg(amount), median(amount),
                   stddev_samp(amount) FROM {TABLE}""",
    )
    dup = _one(con, f"SELECT count(*) - count(DISTINCT row_id) FROM {TABLE}")[0]
    if dup:
        problems.append(f"{dup} duplicate row_id values")
    return r


def format_summary(r: dict) -> str:
    L = ["TRIS validation summary", "=" * 23]
    if "total" in r:
        L.append(f"Transactions: {r['total']:,}")
        L.append("\nisFraud (simulated ground truth):")
        for v, n in r["fraud"]:
            L.append(f"  {v}: {n:,} ({n / r['total']:.4%})")
        L.append("\nisFlaggedFraud (simulator rule output, NOT ground truth):")
        for v, n in r["flagged"]:
            L.append(f"  {v}: {n:,}")
        L.append("\nTransaction types:")
        for t, n, f in r["types"]:
            L.append(f"  {t:<10} {n:>12,}  ({n / r['total']:.2%})  fraud={int(f):,}")
        lo, hi, distinct = r["step"]
        L.append(f"\nSimulated steps (hours, not timestamps): min={lo} max={hi} distinct={distinct}")
        mn, mx, avg, med, sd = r["amount"]
        sd_text = "n/a" if sd is None else f"{sd:,.2f}"
        L.append(
            f"Amount: min={mn:,.2f} max={mx:,.2f} mean={avg:,.2f} "
            f"median={med:,.2f} stddev={sd_text}"
        )
        if r["nulls"]:
            L.append(f"NULLs: {r['nulls']}")
    L.append("")
    if r["problems"]:
        L.append("RESULT: FAIL")
        L += [f"  - {p}" for p in r["problems"]]
    else:
        L.append("RESULT: PASS")
    return "\n".join(L)
