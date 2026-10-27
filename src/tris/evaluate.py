"""Detection evaluation against the simulated ground truth (isFraud).

isFraud is read here only, after alerts exist; it never feeds detection,
scoring or thresholds. Evaluation covers the held-out period only
(step > REFERENCE_END_STEP), which thresholds were not derived from.
"""

from __future__ import annotations

import duckdb

from .detect import REFERENCE_END_STEP, RULE_WEIGHTS
from .schema import TABLE


def _div(n: float, d: float) -> float | None:
    """None (not 0) when the denominator is zero, so 'undefined' is explicit."""
    return n / d if d else None


def metrics(tp: int, fp: int, tn: int, fn: int) -> dict:
    precision, recall = _div(tp, tp + fp), _div(tp, tp + fn)
    f1 = _div(2 * precision * recall, precision + recall) if precision is not None and recall is not None else None
    total = tp + fp + tn + fn
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn, "population": total,
        "precision": precision, "recall": recall, "f1": f1,
        "alert_rate": _div(tp + fp, total), "fraud_prevalence": _div(tp + fn, total),
    }


def evaluate(con: duckdb.DuckDBPyConnection, min_step: int = REFERENCE_END_STEP + 1) -> dict:
    """Per-rule and combined confusion matrices on rows with step >= min_step."""
    out = {"min_step": min_step}
    scopes = {"COMBINED": "SELECT row_id FROM alerts"} | {
        r: f"SELECT DISTINCT row_id FROM alert_evidence WHERE rule = '{r}'" for r in RULE_WEIGHTS
    }
    for name, sub in scopes.items():
        tp, fp, tn, fn = con.execute(f"""
            SELECT coalesce(sum(CASE WHEN a.row_id IS NOT NULL AND t.isFraud = 1 THEN 1 END), 0),
                   coalesce(sum(CASE WHEN a.row_id IS NOT NULL AND t.isFraud = 0 THEN 1 END), 0),
                   coalesce(sum(CASE WHEN a.row_id IS NULL AND t.isFraud = 0 THEN 1 END), 0),
                   coalesce(sum(CASE WHEN a.row_id IS NULL AND t.isFraud = 1 THEN 1 END), 0)
            FROM {TABLE} t LEFT JOIN ({sub}) a USING (row_id)
            WHERE t.step >= ?""", [min_step]).fetchone()
        out[name] = metrics(tp, fp, tn, fn)
    return out


def format_evaluation(res: dict) -> str:
    f = lambda v: "undefined" if v is None else f"{v:.4f}"
    L = [f"Held-out evaluation (step >= {res['min_step']}; thresholds derived from earlier steps only)"]
    for name, m in ((k, v) for k, v in res.items() if isinstance(v, dict)):
        L.append(
            f"{name:<22} TP={m['tp']:,} FP={m['fp']:,} TN={m['tn']:,} FN={m['fn']:,} "
            f"precision={f(m['precision'])} recall={f(m['recall'])} F1={f(m['f1'])} "
            f"alert_rate={f(m['alert_rate'])} prevalence={f(m['fraud_prevalence'])} n={m['population']:,}"
        )
    return "\n".join(L)
