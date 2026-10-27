from pathlib import Path

import duckdb
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from tris import detect
from tris.cases import ensure_tables
from tris.detect import run_detection
from tris.ingest import ingest

APP = str(Path(__file__).resolve().parents[1] / "src" / "tris" / "app.py")


def row(step, typ, amount, orig, dest):
    return f"{step},{typ},{amount},{orig},9,9,{dest},9,9,0,0"


@pytest.fixture
def app_db(make_csv, tmp_path, monkeypatch):
    monkeypatch.setattr(detect, "REFERENCE_END_STEP", 50)
    rows = [row(1 + i % 50, "PAYMENT", i + 1, f"CR{i}", f"MR{i}") for i in range(100)]
    rows.append(row(60, "PAYMENT", 10000, "BIG", "M1"))  # held-out < step 401: evaluation population is empty
    db = tmp_path / "app.duckdb"
    ingest(make_csv(rows), db)
    c = duckdb.connect(str(db))
    ensure_tables(c)
    run_detection(c)
    c.close()
    monkeypatch.setenv("TRIS_DB", str(db))
    return db


@pytest.fixture(autouse=True)
def clear_streamlit_caches():
    st.cache_data.clear()
    st.cache_resource.clear()  # closes nothing explicitly; fresh connection per test
    yield
    st.cache_resource.clear()


def run_view(view, timeout=60):
    at = AppTest.from_file(APP, default_timeout=timeout)
    at.run()
    if view != "Overview":
        at.sidebar.radio[0].set_value(view).run()
    return at


@pytest.mark.parametrize("view", ["Overview", "Alert Queue", "Alert Investigation",
                                  "Account Network", "Detection Performance"])
def test_views_render_without_errors(app_db, view):
    at = run_view(view)  # includes the empty held-out evaluation edge case
    assert not at.exception and not at.error


def test_missing_database_is_reported_clearly(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIS_DB", str(tmp_path / "nope.duckdb"))
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception and any("Database not found" in e.value for e in at.error)
