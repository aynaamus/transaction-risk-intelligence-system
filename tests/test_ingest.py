import duckdb
import pytest

from tris.ingest import IngestionError, ingest
from tris.schema import COLUMNS, FEATURE_EXCLUDED_COLUMNS
from tris.validate import format_summary, run_validation

from conftest import HEADER, ROWS


def count(db):
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute("SELECT count(*) FROM transactions").fetchone()[0]
    finally:
        con.close()


def test_successful_ingestion(make_csv, tmp_path):
    db = tmp_path / "t.duckdb"
    res = ingest(make_csv(), db)
    assert res.status == "ingested" and res.rows == len(ROWS)
    con = duckdb.connect(str(db), read_only=True)
    cols = [r[0] for r in con.execute("DESCRIBE transactions").fetchall()]
    assert cols == ["row_id", *COLUMNS]
    # source values preserved, including both label fields
    row = con.execute("SELECT * FROM transactions WHERE row_id = 6").fetchone()
    assert row[1:] == (3, "TRANSFER", 300000.0, "C6", 300000.0, 0.0, "C7", 0.0, 0.0, 1, 1)
    con.close()


def test_rerun_does_not_duplicate(make_csv, tmp_path):
    db, csv = tmp_path / "t.duckdb", make_csv()
    ingest(csv, db)
    again = ingest(csv, db)
    assert again.status == "skipped"
    assert count(db) == len(ROWS)
    replaced = ingest(csv, db, replace=True)
    assert replaced.status == "ingested" and count(db) == len(ROWS)


def test_different_file_refused_without_replace(make_csv, tmp_path):
    db = tmp_path / "t.duckdb"
    ingest(make_csv(), db)
    other = make_csv(ROWS[:2], name="other.csv")
    with pytest.raises(IngestionError, match="different source"):
        ingest(other, db)
    assert count(db) == len(ROWS)
    ingest(other, db, replace=True)
    assert count(db) == 2


def test_missing_column_rejected(make_csv, tmp_path):
    bad = make_csv(header=HEADER.replace(",isFlaggedFraud", ""),
                   rows=[r.rsplit(",", 1)[0] for r in ROWS])
    with pytest.raises(IngestionError, match="isFlaggedFraud"):
        ingest(bad, tmp_path / "t.duckdb")


def test_unexpected_column_rejected(make_csv, tmp_path):
    bad = make_csv(header=HEADER + ",extra", rows=[r + ",x" for r in ROWS])
    with pytest.raises(IngestionError, match="extra"):
        ingest(bad, tmp_path / "t.duckdb")


def test_malformed_value_rejected_and_prior_data_kept(make_csv, tmp_path):
    db = tmp_path / "t.duckdb"
    ingest(make_csv(), db)
    bad = make_csv([ROWS[0].replace("9839.64", "abc")], name="bad.csv")
    with pytest.raises(IngestionError, match="nothing was changed"):
        ingest(bad, db, replace=True)
    assert count(db) == len(ROWS)


def test_missing_csv(tmp_path):
    with pytest.raises(IngestionError, match="not found"):
        ingest(tmp_path / "nope.csv", tmp_path / "t.duckdb")


def test_source_csv_unmodified(make_csv, tmp_path):
    csv = make_csv()
    before = csv.read_bytes()
    ingest(csv, tmp_path / "t.duckdb")
    assert csv.read_bytes() == before


def test_validation_summary(make_csv, tmp_path):
    db = tmp_path / "t.duckdb"
    ingest(make_csv(), db)
    con = duckdb.connect(str(db), read_only=True)
    r = run_validation(con)
    con.close()
    assert r["problems"] == []
    assert r["total"] == 6
    assert r["fraud"] == [(0, 3), (1, 3)]
    assert r["flagged"] == [(0, 5), (1, 1)]
    assert {t: n for t, n, _ in r["types"]}["TRANSFER"] == 2
    assert r["step"] == (1, 3, 3)
    assert r["amount"][0] == 181.0 and r["amount"][1] == 300000.0
    text = format_summary(r)
    assert "RESULT: PASS" in text and "NOT ground truth" in text


def test_validation_flags_bad_values(make_csv, tmp_path):
    db = tmp_path / "t.duckdb"
    rows = [ROWS[0], "1,WIRE,-5.0,C9,1.0,1.0,C8,0.0,0.0,2,0", "1,CASH_IN,1.0,,1.0,1.0,C8,0.0,0.0,0,0"]
    ingest(make_csv(rows), db)
    con = duckdb.connect(str(db), read_only=True)
    r = run_validation(con)
    con.close()
    joined = " | ".join(r["problems"])
    for expected in ("negative amount", "isFraud not in", "Unexpected transaction type: WIRE", "NULL values in nameOrig"):
        assert expected in joined


def test_feature_exclusion_policy():
    assert set(FEATURE_EXCLUDED_COLUMNS) == {
        "isFraud", "isFlaggedFraud", "oldbalanceOrg", "newbalanceOrig",
        "oldbalanceDest", "newbalanceDest",
    }


def test_summary_handles_single_row(make_csv, tmp_path):
    db = tmp_path / "t.duckdb"
    ingest(make_csv(ROWS[:1]), db)
    con = duckdb.connect(str(db), read_only=True)
    text = format_summary(run_validation(con))
    con.close()
    assert "stddev=n/a" in text and "RESULT: PASS" in text
