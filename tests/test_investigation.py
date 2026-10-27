import duckdb
import pytest

from tris import detect
from tris.alerts import alert_detail, filter_options, query_alerts, same_account_transactions
from tris.cases import (CaseError, add_note, ensure_tables, get_case, list_audit,
                        list_notes, open_case, update_case)
from tris.detect import run_detection
from tris.ingest import ingest


def row(step, typ, amount, orig, dest, fraud=0):
    return f"{step},{typ},{amount},{orig},999,999,{dest},999,999,{fraud},1"


@pytest.fixture
def con(make_csv, tmp_path, monkeypatch):
    monkeypatch.setattr(detect, "REFERENCE_END_STEP", 50)
    ref = [row(1 + i % 50, "PAYMENT", i + 1, f"CR{i}", f"MR{i}") for i in range(100)]
    extra = [
        row(60, "PAYMENT", 10000, "BIG", "M1"),            # A only (LOW)
        row(61, "TRANSFER", 5000, "SRC", "MID"),
        row(62, "PAYMENT", 4500, "MID", "EXIT"),           # A + C (HIGH)
        row(70, "PAYMENT", 20000, "BIG2", "M2"),          # A (LOW)
        row(80, "PAYMENT", 40, "QUIET", "M3"),             # no alert
    ]
    db = tmp_path / "t.duckdb"
    ingest(make_csv(ref + extra), db)
    c = duckdb.connect(str(db))
    ensure_tables(c)
    run_detection(c)
    yield c
    c.close()


def rid(con, orig):
    return con.execute("SELECT row_id FROM transactions WHERE nameOrig = ?", [orig]).fetchone()[0]


# ---- retrieval -----------------------------------------------------------

def test_pagination_and_totals(con):
    total = con.execute("SELECT count(*) FROM alerts").fetchone()[0]
    p1, n1 = query_alerts(con, page=1, page_size=2)
    p2, n2 = query_alerts(con, page=2, page_size=2)
    assert n1 == n2 == total and len(p1) == 2
    assert not {r["row_id"] for r in p1} & {r["row_id"] for r in p2}
    assert query_alerts(con, page=10_000)[0] == []  # beyond last page -> empty, no error


def test_sorting_is_deterministic_and_whitelisted(con):
    rows, _ = query_alerts(con, sort_by="amount", descending=True, page_size=500)
    amounts = [r["amount"] for r in rows]
    assert amounts == sorted(amounts, reverse=True)
    with pytest.raises(ValueError):
        query_alerts(con, sort_by="amount; DROP TABLE alerts")


def test_filters(con):
    rows, n = query_alerts(con, rules=["C_RAPID_ONWARD"])
    assert n == 1 and rows[0]["row_id"] == rid(con, "MID")
    assert rows[0]["severity"] == "HIGH" and rows[0]["risk_score"] == 70
    assert [r["row_id"] for r in query_alerts(con, severities=["HIGH"])[0]] == [rid(con, "MID")]
    assert query_alerts(con, severities=["MEDIUM"]) == ([], 0)
    rows, n = query_alerts(con, types=["PAYMENT"], step_range=(65, 100))
    assert n == 1 and rows[0]["nameOrig"] == "BIG2"
    assert query_alerts(con, step_range=(900, 999)) == ([], 0)
    assert query_alerts(con, rules=["B_DEST_CONCENTRATION"], types=["DEBIT"]) == ([], 0)


def test_rows_expose_no_labels_or_balances(con):
    rows, _ = query_alerts(con)
    forbidden = {"isFraud", "isFlaggedFraud", "oldbalanceOrg", "newbalanceOrig", "oldbalanceDest", "newbalanceDest"}
    assert not forbidden & set(rows[0])
    d = alert_detail(con, rows[0]["row_id"])
    assert not forbidden & set(d["transaction"])


def test_status_filter_uses_cases(con):
    a, b = rid(con, "BIG"), rid(con, "BIG2")
    assert query_alerts(con, statuses=["NEW"])[1] == con.execute("SELECT count(*) FROM alerts").fetchone()[0]
    open_case(con, a, "ana")
    assert query_alerts(con, statuses=["OPEN"])[0][0]["row_id"] == a
    assert b in {r["row_id"] for r in query_alerts(con, statuses=["NEW"], page_size=500)[0]}
    assert a not in {r["row_id"] for r in query_alerts(con, statuses=["NEW"], page_size=500)[0]}


def test_alert_detail_explains_rules(con):
    mid_alert = con.execute("SELECT row_id FROM alerts WHERE n_rules = 2").fetchone()[0]
    d = alert_detail(con, mid_alert)
    rules = {e["rule"]: e for e in d["evidence"]}
    assert set(rules) == {"A_LARGE_AMOUNT", "C_RAPID_ONWARD"}
    assert rules["A_LARGE_AMOUNT"]["threshold"] < rules["A_LARGE_AMOUNT"]["observed"]
    assert rules["C_RAPID_ONWARD"]["related_row_id"] == rid(con, "SRC")
    assert [t["row_id"] for t in d["linked_transactions"]] == [rid(con, "SRC")]
    assert all(e["description"] for e in d["evidence"])
    assert {t["row_id"] for t in same_account_transactions(con, mid_alert)} >= {rid(con, "SRC")}


def test_missing_alert_and_empty_filter_options(con):
    assert alert_detail(con, -1) is None
    assert alert_detail(con, rid(con, "QUIET")) is None  # transaction exists, no alert
    assert same_account_transactions(con, -1) == []
    o = filter_options(con)
    assert o["step_min"] == 1 and "PAYMENT" in o["types"]


# ---- cases ---------------------------------------------------------------

def test_one_case_per_alert(con):
    r = rid(con, "BIG")
    c1, c2 = open_case(con, r, "ana"), open_case(con, r, "bob")
    assert c1["case_id"] == c2["case_id"] and c1["status"] == "OPEN"
    assert con.execute("SELECT count(*) FROM investigation_cases").fetchone()[0] == 1
    assert len(list_audit(con, r)) == 1  # second open added nothing


def test_open_case_requires_alert_and_analyst(con):
    with pytest.raises(CaseError, match="No alert"):
        open_case(con, rid(con, "QUIET"), "ana")
    with pytest.raises(CaseError, match="Analyst"):
        open_case(con, rid(con, "BIG"), "  ")
    assert con.execute("SELECT count(*) FROM investigation_cases").fetchone()[0] == 0


def test_update_audit_records_old_and_new(con):
    r = rid(con, "BIG")
    open_case(con, r, "ana")
    update_case(con, r, "bob", status="IN_REVIEW", finding="  looks like a round-number payment ")
    update_case(con, r, "bob", status="IN_REVIEW")  # unchanged -> no audit row
    c = update_case(con, r, "cy", status="CLOSED", disposition="NO_FURTHER_ACTION")
    assert (c["status"], c["disposition"], c["updated_by"]) == ("CLOSED", "NO_FURTHER_ACTION", "cy")
    assert c["finding"] == "looks like a round-number payment"
    log = [(a["analyst"], a["field"], a["old_value"], a["new_value"]) for a in list_audit(con, r)]
    assert log == [
        ("ana", "status", None, "OPEN"),
        ("bob", "status", "OPEN", "IN_REVIEW"),
        ("bob", "finding", "", "looks like a round-number payment"),
        ("cy", "status", "IN_REVIEW", "CLOSED"),
        ("cy", "disposition", "NONE", "NO_FURTHER_ACTION"),
    ]


def test_validation_and_rollback(con):
    r = rid(con, "BIG")
    open_case(con, r, "ana")
    with pytest.raises(CaseError, match="disposition"):
        update_case(con, r, "ana", status="CLOSED")
    assert get_case(con, r)["status"] == "OPEN" and len(list_audit(con, r)) == 1
    with pytest.raises(CaseError, match="Invalid status"):
        update_case(con, r, "ana", status="DONE")
    with pytest.raises(CaseError, match="Invalid disposition"):
        update_case(con, r, "ana", disposition="GUILTY")
    with pytest.raises(CaseError, match="No case"):
        update_case(con, rid(con, "BIG2"), "ana", status="OPEN")
    with pytest.raises(CaseError, match="Note text"):
        add_note(con, r, "ana", "   ")
    with pytest.raises(CaseError, match="No case"):
        add_note(con, rid(con, "BIG2"), "ana", "x")


def test_notes_append_only_and_audited(con):
    r = rid(con, "BIG")
    open_case(con, r, "ana")
    add_note(con, r, "ana", "first")
    add_note(con, r, "bob", "it's 'quoted'; DROP TABLE alerts;--")
    notes = list_notes(con, r)
    assert [n["note"] for n in notes] == ["first", "it's 'quoted'; DROP TABLE alerts;--"]
    assert [a["action"] for a in list_audit(con, r)] == ["CASE_OPENED", "NOTE_ADDED", "NOTE_ADDED"]
    assert con.execute("SELECT count(*) FROM alerts").fetchone()[0] > 0


def test_cases_survive_detection_rerun(con):
    r = rid(con, "BIG")
    open_case(con, r, "ana")
    update_case(con, r, "ana", status="ESCALATED")
    add_note(con, r, "ana", "keep me")
    before = con.execute("SELECT case_id, status FROM investigation_cases").fetchall()
    run_detection(con)
    assert con.execute("SELECT case_id, status FROM investigation_cases").fetchall() == before
    assert len(list_notes(con, r)) == 1 and len(list_audit(con, r)) == 3
    assert query_alerts(con, statuses=["ESCALATED"])[0][0]["row_id"] == r  # still joins to the alert


def test_case_persists_across_restart(con, tmp_path):
    r = rid(con, "BIG")
    open_case(con, r, "ana")
    add_note(con, r, "ana", "persist")
    path = con.execute("PRAGMA database_list").fetchone()[2]
    con.close()
    c2 = duckdb.connect(path)
    assert get_case(c2, r)["status"] == "OPEN" and list_notes(c2, r)[0]["note"] == "persist"
    c2.close()


def test_case_for_removed_alert_keeps_history(con):
    r = rid(con, "BIG")
    open_case(con, r, "ana")
    con.execute("DELETE FROM alert_evidence WHERE row_id = ?", [r])
    con.execute("DELETE FROM alerts WHERE row_id = ?", [r])  # simulates a rerun that no longer alerts
    assert get_case(con, r) is not None and len(list_audit(con, r)) == 1
    assert alert_detail(con, r) is None
