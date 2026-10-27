"""Explainable, SQL-driven suspicious-transaction detection.

Rules use only: row_id, step, type, amount, nameOrig, nameDest.
They never read isFraud, isFlaggedFraud or the four balance columns
(see tris.schema.FEATURE_EXCLUDED_COLUMNS). Data-dependent thresholds are
derived from the reference period only (step <= REFERENCE_END_STEP).

Alerts are suspicious-activity indicators on simulated data, not proof of
fraud or money laundering. `step` is simulated hours, not a timestamp.
"""

from __future__ import annotations

import duckdb

from .schema import TABLE

REFERENCE_END_STEP = 400  # reference: step <= 400; held-out: step > 400
AMOUNT_QUANTILE = 0.99  # Rule A: per-type quantile of reference amounts
FREQ_WINDOW = 6  # Rule B: current + previous 5 simulated hours
FREQ_QUANTILE = 0.999  # Rule B: quantile of reference inbound counts
FLOW_WINDOW = 6  # Rule C: onward transfer within 6 simulated hours
FLOW_MIN_RATIO = 0.8  # Rule C: outflow >= 80% of inflow
FLOW_MAX_RATIO = 1.0  # Rule C: outflow <= inflow (cannot forward more than received)

RULE_WEIGHTS = {"A_LARGE_AMOUNT": 30, "B_DEST_CONCENTRATION": 20, "C_RAPID_ONWARD": 40}
SEVERITY_HIGH, SEVERITY_MEDIUM = 60, 40  # score >= HIGH -> HIGH; >= MEDIUM -> MEDIUM; else LOW

ANALYTIC_TABLES = ("rule_thresholds", "alert_evidence", "alerts")


def _drop_all(con):
    for t in ANALYTIC_TABLES:
        con.execute(f"DROP TABLE IF EXISTS {t}")


def _rule_a(con):
    con.execute(f"""
        INSERT INTO rule_thresholds
        SELECT 'A_LARGE_AMOUNT', type, quantile_cont(amount, {AMOUNT_QUANTILE}),
               'p{AMOUNT_QUANTILE} of reference-period amounts for this type'
        FROM {TABLE} WHERE step <= {REFERENCE_END_STEP} GROUP BY type""")
    con.execute(f"""
        INSERT INTO alert_evidence
        SELECT t.row_id, 'A_LARGE_AMOUNT', th.value, t.amount,
               'amount ' || t.amount || ' > ' || th.note || ' (' || th.value || ')', NULL
        FROM {TABLE} t JOIN rule_thresholds th
          ON th.rule = 'A_LARGE_AMOUNT' AND th.key = t.type
        WHERE t.amount > 0 AND t.amount > th.value""")


def _rule_b(con):
    # Inbound transactions to the same destination within the window; each
    # transaction counted once (window frame over distinct rows, step-ordered).
    con.execute(f"""
        CREATE TEMP TABLE inbound AS
        SELECT row_id, step, nameDest,
               count(*) OVER (PARTITION BY nameDest ORDER BY step
                              RANGE BETWEEN {FREQ_WINDOW - 1} PRECEDING AND CURRENT ROW) AS n_in_window
        FROM {TABLE}""")
    con.execute(f"""
        INSERT INTO rule_thresholds
        SELECT 'B_DEST_CONCENTRATION', 'all', quantile_cont(n_in_window, {FREQ_QUANTILE}),
               'p{FREQ_QUANTILE} of reference-period inbound counts per {FREQ_WINDOW}-hour window'
        FROM inbound WHERE step <= {REFERENCE_END_STEP}""")
    con.execute(f"""
        INSERT INTO alert_evidence
        SELECT i.row_id, 'B_DEST_CONCENTRATION', th.value, i.n_in_window,
               i.n_in_window || ' inbound transactions to ' || i.nameDest || ' in steps '
               || (i.step - {FREQ_WINDOW - 1}) || '-' || i.step || ' (threshold > ' || th.value || ')', NULL
        FROM inbound i JOIN rule_thresholds th ON th.rule = 'B_DEST_CONCENTRATION'
        WHERE i.n_in_window > th.value""")
    con.execute("DROP TABLE inbound")


def _rule_c(con):
    con.execute(f"""
        INSERT INTO rule_thresholds VALUES
        ('C_RAPID_ONWARD', 'window_hours', {FLOW_WINDOW}, 'onward step - inbound step <= window'),
        ('C_RAPID_ONWARD', 'min_ratio', {FLOW_MIN_RATIO}, 'outflow / inflow lower bound'),
        ('C_RAPID_ONWARD', 'max_ratio', {FLOW_MAX_RATIO}, 'outflow / inflow upper bound')""")
    # Alert the onward (outbound) transaction; the inbound one is evidence.
    # Sequencing: inbound row_id < outbound row_id (CSV order is step-sorted),
    # so same-step pairs are ordered. The link is by shared account id only.
    con.execute(f"""
        INSERT INTO alert_evidence
        SELECT o.row_id, 'C_RAPID_ONWARD', {FLOW_MIN_RATIO}, o.amount / i.amount,
               'account ' || o.nameOrig || ' received ' || i.amount || ' (row ' || i.row_id
               || ', step ' || i.step || ') then sent ' || o.amount || ' (step ' || o.step || ')',
               i.row_id
        FROM {TABLE} i JOIN {TABLE} o
          ON o.nameOrig = i.nameDest AND o.row_id > i.row_id
         AND o.step BETWEEN i.step AND i.step + {FLOW_WINDOW}
        WHERE i.amount > 0 AND o.amount >= {FLOW_MIN_RATIO} * i.amount
          AND o.amount <= {FLOW_MAX_RATIO} * i.amount""")


def _score(con):
    cases = " ".join(f"WHEN '{r}' THEN {w}" for r, w in RULE_WEIGHTS.items())
    con.execute(f"""
        CREATE TABLE alerts AS
        WITH per_rule AS (SELECT DISTINCT row_id, rule FROM alert_evidence)
        SELECT row_id,
               least(100, sum(CASE rule {cases} END)) AS risk_score,
               CASE WHEN least(100, sum(CASE rule {cases} END)) >= {SEVERITY_HIGH} THEN 'HIGH'
                    WHEN least(100, sum(CASE rule {cases} END)) >= {SEVERITY_MEDIUM} THEN 'MEDIUM'
                    ELSE 'LOW' END AS severity,
               list(rule ORDER BY rule) AS rules,
               count(*) AS n_rules
        FROM per_rule GROUP BY row_id""")


def run_detection(con: duckdb.DuckDBPyConnection) -> dict:
    """Rebuild thresholds, evidence and alerts atomically. Safe to rerun."""
    con.execute("BEGIN")
    try:
        _drop_all(con)
        con.execute("""CREATE TABLE rule_thresholds (
            rule VARCHAR, key VARCHAR, value DOUBLE, note VARCHAR)""")
        con.execute("""CREATE TABLE alert_evidence (
            row_id BIGINT, rule VARCHAR, threshold DOUBLE, observed DOUBLE,
            detail VARCHAR, related_row_id BIGINT)""")
        _rule_a(con)
        _rule_b(con)
        _rule_c(con)
        _score(con)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return summarize(con)


def summarize(con) -> dict:
    return {
        "total_alerts": con.execute("SELECT count(*) FROM alerts").fetchone()[0],
        "by_rule": con.execute(
            "SELECT rule, count(DISTINCT row_id) FROM alert_evidence GROUP BY 1 ORDER BY 1").fetchall(),
        "by_severity": con.execute(
            "SELECT severity, count(*) FROM alerts GROUP BY 1 ORDER BY 1").fetchall(),
        "thresholds": con.execute(
            "SELECT rule, key, value FROM rule_thresholds ORDER BY 1, 2").fetchall(),
    }
