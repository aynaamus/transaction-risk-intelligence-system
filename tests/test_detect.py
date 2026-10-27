import duckdb
import pytest

from tris import detect
from tris.detect import run_detection
from tris.evaluate import evaluate, format_evaluation, metrics
from tris.ingest import ingest

from conftest import HEADER


def row(step, typ, amount, orig, dest, fraud=0):
    return f"{step},{typ},{amount},{orig},999,999,{dest},999,999,{fraud},0"


def reference_rows():
    """Steps 1-50: 100 unremarkable PAYMENTs, amounts 1..100, unique accounts."""
    return [row(1 + i % 50, "PAYMENT", i + 1, f"CR{i}", f"MR{i}") for i in range(100)]


@pytest.fixture
def build(make_csv, tmp_path, monkeypatch):
    monkeypatch.setattr(detect, "REFERENCE_END_STEP", 50)
    def _build(extra):
        db = tmp_path / "t.duckdb"
        ingest(make_csv(reference_rows() + extra), db)
        return duckdb.connect(str(db))
    return _build


def ids(con, rule):
    # Held-out rows only: by construction ~1% of reference rows exceed their own p99.
    return {r[0] for r in con.execute(
        "SELECT row_id FROM alert_evidence JOIN transactions USING (row_id) "
        "WHERE rule = ? AND step > 50", [rule]).fetchall()}


def rid(con, orig):
    return con.execute("SELECT row_id FROM transactions WHERE nameOrig = ?", [orig]).fetchone()[0]


def test_rule_a_large_amount_and_zero(build):
    con = build([row(60, "PAYMENT", 10000, "BIG", "M1"), row(60, "PAYMENT", 0, "ZERO", "M2"),
                 row(60, "PAYMENT", 50, "SMALL", "M3")])
    run_detection(con)
    a = ids(con, "A_LARGE_AMOUNT")
    assert a == {rid(con, "BIG")}  # zero amount and normal amount are not alerted
    th, obs = con.execute("SELECT threshold, observed FROM alert_evidence WHERE rule='A_LARGE_AMOUNT' AND observed = 10000").fetchone()
    assert obs == 10000 and 98 < th < 100  # p99 of reference amounts 1..100
    assert con.execute("SELECT count(*) FROM transactions WHERE amount = 0").fetchone()[0] == 1  # preserved


def test_rule_a_threshold_ignores_heldout(build):
    con = build([row(60, "PAYMENT", 10 ** 9, "HUGE", "M1")])
    run_detection(con)
    th = con.execute("SELECT value FROM rule_thresholds WHERE rule='A_LARGE_AMOUNT'").fetchone()[0]
    assert th < 101


def test_rule_b_concentration_counts_each_row_once(build):
    extra = [row(60 + i, "PAYMENT", 5, f"S{i}", "HUB") for i in range(4)] + \
            [row(70, "PAYMENT", 5, "LATE", "HUB")]  # outside the 6-step window of the others
    con = build(extra)
    run_detection(con)
    b = ids(con, "B_DEST_CONCENTRATION")
    assert b == {rid(con, f"S{i}") for i in range(1, 4)}  # reference p99.9 = 1, so 2nd..4th trigger
    counts = dict(con.execute("SELECT row_id, observed FROM alert_evidence WHERE rule='B_DEST_CONCENTRATION'").fetchall())
    assert counts[rid(con, "S3")] == 4
    assert con.execute("SELECT count(*) FROM alert_evidence WHERE rule='B_DEST_CONCENTRATION'").fetchone()[0] == 3


def test_rule_c_rapid_onward(build):
    extra = [
        row(60, "TRANSFER", 1000, "SRC", "MID"),
        row(62, "CASH_OUT", 900, "MID", "EXIT"),          # in window, 90%: alert
        row(61, "TRANSFER", 1000, "SRC2", "MID2"),
        row(62, "CASH_OUT", 1500, "MID2", "EXIT2"),       # sends more than received: no
        row(61, "TRANSFER", 1000, "SRC3", "MID3"),
        row(70, "CASH_OUT", 900, "MID3", "EXIT3"),        # outside window: no
        row(61, "CASH_OUT", 900, "MID4", "EXIT4"),        # outbound BEFORE inbound
        row(65, "TRANSFER", 1000, "SRC4", "MID4"),
    ]
    con = build(extra)
    run_detection(con)
    assert ids(con, "C_RAPID_ONWARD") == {rid(con, "MID")}
    rel, detail = con.execute("SELECT related_row_id, detail FROM alert_evidence WHERE rule='C_RAPID_ONWARD'").fetchone()
    assert rel == rid(con, "SRC") and "MID" in detail


def test_overlap_single_alert_with_combined_score(build):
    extra = [row(60, "TRANSFER", 5000, "SRC", "MID"), row(61, "PAYMENT", 4500, "MID", "EXIT")]
    con = build(extra)
    run_detection(con)
    r = con.execute("SELECT row_id, risk_score, severity, rules, n_rules FROM alerts WHERE row_id = ?",
                    [rid(con, "MID")]).fetchone()
    assert r[1:] == (70, "HIGH", ["A_LARGE_AMOUNT", "C_RAPID_ONWARD"], 2)
    assert con.execute("SELECT count(*) - count(DISTINCT row_id) FROM alerts").fetchone()[0] == 0


def test_rerun_is_idempotent_and_sources_untouched(build):
    con = build([row(60, "PAYMENT", 10000, "BIG", "M1")])
    before = con.execute("SELECT count(*) FROM transactions").fetchone()[0], \
        con.execute("SELECT count(*) FROM ingestion_log").fetchone()[0]
    first = run_detection(con)
    n_ev = con.execute("SELECT count(*) FROM alert_evidence").fetchone()[0]
    second = run_detection(con)
    assert first == second
    assert con.execute("SELECT count(*) FROM alert_evidence").fetchone()[0] == n_ev
    assert (con.execute("SELECT count(*) FROM transactions").fetchone()[0],
            con.execute("SELECT count(*) FROM ingestion_log").fetchone()[0]) == before


def test_detection_independent_of_labels(build):
    con = build([row(60, "PAYMENT", 10000, "BIG", "M1", fraud=0), row(60, "PAYMENT", 10, "OK", "M2", fraud=0)])
    run_detection(con)
    base = con.execute("SELECT * FROM alerts ORDER BY row_id").fetchall()
    con.execute("UPDATE transactions SET isFraud = 1 - isFraud, isFlaggedFraud = 1, "
                "oldbalanceOrg = 12345, newbalanceDest = 54321")
    run_detection(con)
    assert con.execute("SELECT * FROM alerts ORDER BY row_id").fetchall() == base


def test_empty_results(build):
    con = build([row(60, "PAYMENT", 50, "OK", "M1")])
    run_detection(con)
    assert con.execute("SELECT count(*) FROM alerts JOIN transactions USING (row_id) WHERE step > 50").fetchone()[0] == 0
    con.execute("DELETE FROM alert_evidence"); con.execute("DELETE FROM alerts")
    r = detect.summarize(con)
    assert r["total_alerts"] == 0 and r["by_rule"] == [] and r["by_severity"] == []
    res = evaluate(con, min_step=51)
    c = res["COMBINED"]
    assert (c["tp"], c["fp"]) == (0, 0) and c["precision"] is None and c["f1"] is None
    assert "undefined" in format_evaluation(res)


def test_evaluation_counts_on_heldout_only(build):
    extra = [row(60, "PAYMENT", 10000, "BIGF", "M1", fraud=1),   # TP
             row(60, "PAYMENT", 20000, "BIGN", "M2", fraud=0),   # FP
             row(61, "PAYMENT", 5, "MISS", "M3", fraud=1),       # FN
             row(61, "PAYMENT", 5, "OKAY", "M4", fraud=0)]       # TN
    con = build(extra)
    run_detection(con)
    m = evaluate(con, min_step=51)["COMBINED"]
    assert (m["tp"], m["fp"], m["tn"], m["fn"], m["population"]) == (1, 1, 1, 1, 4)
    assert m["precision"] == 0.5 and m["recall"] == 0.5 and m["f1"] == 0.5
    assert m["alert_rate"] == 0.5 and m["fraud_prevalence"] == 0.5


def test_metrics_zero_division():
    m = metrics(0, 0, 10, 0)
    assert m["precision"] is None and m["recall"] is None and m["f1"] is None
    assert m["alert_rate"] == 0 and m["fraud_prevalence"] == 0
    assert metrics(0, 0, 0, 0)["alert_rate"] is None
    assert metrics(0, 3, 5, 2)["f1"] is None  # precision 0, recall 0 -> F1 undefined
