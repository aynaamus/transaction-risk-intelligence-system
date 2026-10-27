"""TRIS investigator application (local, single user).

Run:  streamlit run src/tris/app.py
All business logic lives in tris.alerts / tris.cases / tris.detect /
tris.evaluate / tris.network; this file only renders it.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

from tris import cases
from tris.alerts import alert_detail, filter_options, query_alerts, same_account_transactions
from tris.detect import REFERENCE_END_STEP, RULE_WEIGHTS
from tris.evaluate import evaluate
from tris.network import build_figure, chains, ego_network
from tris.schema import TABLE

DB_PATH = Path(os.environ.get("TRIS_DB", Path(__file__).resolve().parents[2] / "data" / "tris.duckdb"))
VIEWS = ["Overview", "Alert Queue", "Alert Investigation", "Account Network", "Detection Performance"]
SYNTHETIC = "Synthetic PaySim data. Alerts are suspicious-activity indicators, not confirmed fraud or money laundering."

st.set_page_config(page_title="TRIS", page_icon=":mag:", layout="wide")
st.markdown(
    """<style>
    .block-container {padding-top: 2rem; max-width: 1400px;}
    h1, h2, h3 {font-weight: 600; letter-spacing: -0.01em;}
    [data-testid="stMetricValue"] {font-size: 1.6rem;}
    </style>""",
    unsafe_allow_html=True,
)


@st.cache_resource
def _open(path: str):
    if not Path(path).exists():
        return None
    con = duckdb.connect(path)
    cases.ensure_tables(con)
    return con


def _connection():
    return _open(str(DB_PATH))


def db():
    """Per-call cursor on the shared connection; stops the page with a clear message if unusable."""
    con = _connection()
    if con is None:
        st.error(f"Database not found at {DB_PATH.name}. Run ingestion and `python -m tris.cli detect` first.")
        st.stop()
    try:
        con.execute("SELECT 1 FROM alerts LIMIT 1")
    except duckdb.Error:
        st.error("No alerts table found. Run `python -m tris.cli detect` first.")
        st.stop()
    return con.cursor()


def pct(v, nd=1):
    return "undefined" if v is None else f"{v:.{nd}%}"


def num(v, nd=3):
    return "undefined" if v is None else f"{v:.{nd}f}"


def money(v):
    return f"{v:,.2f}"


def df(rows):
    return pd.DataFrame(rows)


@st.cache_data(show_spinner=False)
def overview_stats():
    con = db()
    q = lambda s: con.execute(s).fetchall()
    return {
        "transactions": q(f"SELECT count(*) FROM {TABLE}")[0][0],
        "alerts": q("SELECT count(*) FROM alerts")[0][0],
        "severity": dict(q("SELECT severity, count(*) FROM alerts GROUP BY 1")),
        "rules": dict(q("SELECT rule, count(DISTINCT row_id) FROM alert_evidence GROUP BY 1")),
        "volume": q(f"SELECT type, count(*), sum(amount) FROM {TABLE} GROUP BY 1 ORDER BY 2 DESC"),
        "steps": q(f"SELECT min(step), max(step) FROM {TABLE}")[0],
        "cases": q("SELECT count(*) FROM investigation_cases")[0][0],
    }


@st.cache_data(show_spinner="Evaluating held-out period...")
def evaluation():
    return evaluate(db())


def goto(view, row_id=None):
    st.session_state["view"] = view
    if row_id is not None:
        st.session_state["row_id"] = int(row_id)


# ---------------------------------------------------------------- sidebar
st.sidebar.title("TRIS")
st.sidebar.caption("Transaction Risk Intelligence System")
view = st.sidebar.radio("View", VIEWS, key="view")
analyst = st.sidebar.text_input("Analyst ID", value="analyst", max_chars=64,
                                help="Local label recorded in the audit history. Not authentication.")
st.sidebar.info(SYNTHETIC)


# ---------------------------------------------------------------- views
def view_overview():
    st.header("Overview")
    s = overview_stats()
    st.subheader("Transaction monitoring")
    st.caption("Counts from the database. These describe alerts raised by the rules, not fraud outcomes.")
    c = st.columns(4)
    c[0].metric("Transactions", f"{s['transactions']:,}")
    c[1].metric("Alerts", f"{s['alerts']:,}")
    c[2].metric("Alert rate", f"{s['alerts'] / s['transactions']:.2%}")
    c[3].metric("Cases opened", f"{s['cases']:,}")
    left, mid, right = st.columns(3)
    with left:
        st.markdown("**Severity**")
        st.dataframe(df([{"severity": k, "alerts": s["severity"].get(k, 0)} for k in ("HIGH", "MEDIUM", "LOW")]),
                     hide_index=True, width="stretch")
        if s["severity"].get("HIGH", 0) == 0:
            st.caption("No alert reaches HIGH. Scoring thresholds were not changed to populate it.")
    with mid:
        st.markdown("**Rule triggers** (an alert can trigger several)")
        st.dataframe(df([{"rule": r, "alerts": s["rules"].get(r, 0)} for r in RULE_WEIGHTS]),
                     hide_index=True, width="stretch")
    with right:
        st.markdown("**Transaction volume by type**")
        st.dataframe(df([{"type": t, "transactions": n, "total amount": money(a)} for t, n, a in s["volume"]]),
                     hide_index=True, width="stretch")
    st.caption(f"Simulated steps {s['steps'][0]}-{s['steps'][1]} (hours, not timestamps).")

    st.divider()
    st.subheader("Fraud-detection evaluation")
    st.caption(f"Uses the simulated isFraud ground truth, held-out steps > {REFERENCE_END_STEP} only. "
               "A separate analysis from the monitoring figures above.")
    m = evaluation()["COMBINED"]
    c = st.columns(4)
    c[0].metric("Precision", f"{pct(m['precision'])}")
    c[1].metric("Recall", f"{pct(m['recall'])}")
    c[2].metric("F1", num(m['f1']))
    c[3].metric("Held-out transactions", f"{m['population']:,}")


def view_queue():
    st.header("Alert Queue")
    opt = filter_options(db())
    f = st.columns(5)
    rules = f[0].multiselect("Rule", opt["rules"])
    sev = f[1].multiselect("Severity", opt["severities"])
    types = f[2].multiselect("Type", opt["types"])
    status = f[3].multiselect("Status", ["NEW", *cases.STATUSES])
    steps = f[4].slider("Simulated step", opt["step_min"], opt["step_max"], (opt["step_min"], opt["step_max"]))
    step_range = None if steps == (opt["step_min"], opt["step_max"]) else steps
    g = st.columns(4)
    sort_by = g[0].selectbox("Sort by", ["risk_score", "amount", "step", "type", "row_id"])
    desc = g[1].selectbox("Order", ["Descending", "Ascending"]) == "Descending"
    size = g[2].selectbox("Page size", [25, 50, 100, 200], index=1)
    # Reset to page 1 whenever the filter set changes.
    sig = repr((rules, sev, types, status, step_range, sort_by, desc, size))
    if st.session_state.get("queue_sig") != sig:
        st.session_state["queue_sig"], st.session_state["queue_page"] = sig, 1
    try:
        _, total = query_alerts(db(), rules, sev, step_range, types, status, page=1, page_size=1)
        pages = max(1, -(-total // size))
        page = g[3].number_input("Page", 1, pages, key="queue_page")
        rows, _ = query_alerts(db(), rules, sev, step_range, types, status, sort_by, desc, page, size)
    except (duckdb.Error, ValueError) as e:
        st.error(f"Query failed: {e}")
        return
    st.caption(f"{total:,} matching alerts, page {page} of {pages}")
    if not rows:
        st.info("No alerts match these filters.")
        return
    table = df(rows).rename(columns={"row_id": "transaction", "nameOrig": "origin", "nameDest": "destination",
                                     "risk_score": "score"})
    event = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", column_config={"amount": st.column_config.NumberColumn(format="%.2f")})
    sel = event.selection.rows
    if sel:
        rid = int(table.iloc[sel[0]]["transaction"])
        st.button(f"Investigate transaction {rid}", on_click=goto, args=("Alert Investigation", rid), type="primary")
    else:
        st.caption("Select a row to investigate it.")


def view_investigation():
    st.header("Alert Investigation")
    st.session_state.setdefault("row_id", 0)
    rid = st.number_input("Transaction (alert) ID", min_value=0, step=1, key="row_id")
    con = db()
    d = alert_detail(con, int(rid))
    if d is None:
        st.info("No alert exists for that transaction ID. Pick one from the Alert Queue.")
        case = cases.get_case(con, int(rid))
        if case:
            st.warning("A case exists for this ID, but the current detection run no longer alerts on it. "
                       "Its history is preserved below.")
            render_case(con, int(rid), case)
        return
    a, t = d["alert"], d["transaction"]
    c = st.columns(5)
    c[0].metric("Risk score", a["risk_score"])
    c[1].metric("Severity", a["severity"])
    c[2].metric("Amount", money(t["amount"]))
    c[3].metric("Type", t["type"])
    c[4].metric("Simulated step", t["step"])
    st.markdown(f"**Origin** `{t['nameOrig']}` → **Destination** `{t['nameDest']}` · triggered: "
                + ", ".join(f"`{r}`" for r in a["rules"]))
    st.subheader("Why this alert triggered")
    for e in d["evidence"]:
        with st.expander(e["rule"], expanded=True):
            st.write(e["description"])
            st.write(f"**Evidence:** {e['detail']}")
            st.write(f"Threshold: `{e['threshold']:,.4f}` · Observed: `{e['observed']:,.4f}`"
                     + (f" · Linked inbound transaction: `{e['related_row_id']}`" if e["related_row_id"] else ""))
    if d["linked_transactions"]:
        st.markdown("**Transactions cited as evidence**")
        st.dataframe(df(d["linked_transactions"]), hide_index=True, width="stretch")
    with st.expander("Other transactions sharing an account ID (first 20)"):
        st.caption("A shared identifier is an observed link only; it does not show ownership, control or coordination.")
        rel = same_account_transactions(con, int(rid))
        st.dataframe(df(rel), hide_index=True, width="stretch") if rel else st.write("None.")
    st.button("View account network", on_click=goto, args=("Account Network", int(rid)))
    st.divider()
    st.subheader("Case")
    case = cases.get_case(con, int(rid))
    if case is None:
        if st.button("Open case", type="primary"):
            try:
                cases.open_case(con, int(rid), analyst)
                st.rerun()
            except cases.CaseError as e:
                st.error(str(e))
        return
    render_case(con, int(rid), case)


def render_case(con, rid, case):
    st.write(f"Case #{case['case_id']} · status **{case['status']}** · disposition **{case['disposition']}** · "
             f"last updated {case['updated_at']:%Y-%m-%d %H:%M} by {case['updated_by']}")
    with st.form("case_form"):
        c = st.columns(2)
        status = c[0].selectbox("Status", cases.STATUSES, index=cases.STATUSES.index(case["status"]))
        disp = c[1].selectbox("Disposition", cases.DISPOSITIONS, index=cases.DISPOSITIONS.index(case["disposition"]))
        finding = st.text_area("Finding (analyst's own conclusion)", value=case["finding"])
        if st.form_submit_button("Save changes"):
            try:
                cases.update_case(con, rid, analyst, status, disp, finding)
                st.rerun()
            except cases.CaseError as e:
                st.error(str(e))
    with st.form("note_form", clear_on_submit=True):
        note = st.text_area("Add note")
        if st.form_submit_button("Add note"):
            try:
                cases.add_note(con, rid, analyst, note)
                st.rerun()
            except cases.CaseError as e:
                st.error(str(e))
    notes = cases.list_notes(con, rid)
    st.markdown("**Notes**")
    st.dataframe(df(notes), hide_index=True, width="stretch") if notes else st.caption("No notes yet.")
    with st.expander("Audit history (append-only)"):
        st.dataframe(df(cases.list_audit(con, rid)), hide_index=True, width="stretch")


def view_network():
    st.header("Account Network")
    st.caption("Observed transactions between account IDs only. Edges do not show ownership, control or coordination.")
    con = db()
    rid = st.session_state.get("row_id")
    default = ""
    if rid:
        r = con.execute(f"SELECT nameOrig FROM {TABLE} WHERE row_id = ?", [int(rid)]).fetchone()
        default = r[0] if r else ""
    c = st.columns([2, 1, 1])
    account = c[0].text_input("Account ID", value=default, help="Defaults to the origin of the selected alert.")
    hops = c[1].slider("Hops", 1, 3, 2)
    max_edges = c[2].slider("Max edges", 10, 300, 100, step=10)
    if not account.strip():
        st.info("Enter an account ID, or select an alert in the Alert Queue first.")
        return
    g = ego_network(con, account.strip(), hops=hops, max_edges=max_edges)
    if g.number_of_edges() == 0:
        st.info("No transactions found for that account.")
        return
    if g.graph["truncated"]:
        st.warning(f"Graph truncated at {max_edges} edges (highest total amount kept).")
    st.caption(f"{g.number_of_nodes()} accounts, {g.number_of_edges()} observed edges. Red = queried account. Hover edge midpoints for details.")
    st.plotly_chart(build_figure(g), width="stretch")
    edges = [{"origin": o, "destination": d, "transactions": a["tx_count"], "total amount": a["total_amount"],
              "first step": a["first_step"], "last step": a["last_step"],
              "transaction IDs": ", ".join(map(str, a["row_ids"]))} for o, d, a in g.edges(data=True)]
    st.dataframe(df(edges).sort_values("total amount", ascending=False), hide_index=True, width="stretch",
                 column_config={"total amount": st.column_config.NumberColumn(format="%.2f")})
    ch = chains(g)
    if ch:
        with st.expander(f"Observed chains of 3-4 accounts ({len(ch)})"):
            st.write("\n".join(" → ".join(p) for p in ch[:50]))


def view_performance():
    st.header("Detection Performance")
    st.caption(f"Thresholds derived from steps 1-{REFERENCE_END_STEP} (no labels). Evaluated on held-out steps "
               f"> {REFERENCE_END_STEP} against the simulated isFraud label. Labels are used only on this page.")
    res = evaluation()
    m = res["COMBINED"]
    c = st.columns(6)
    c[0].metric("Precision", f"{pct(m['precision'])}")
    c[1].metric("Recall", f"{pct(m['recall'])}")
    c[2].metric("F1", num(m['f1']))
    c[3].metric("Alert rate", pct(m['alert_rate'], 2))
    c[4].metric("Fraud prevalence", pct(m['fraud_prevalence'], 2))
    c[5].metric("Held-out n", f"{m['population']:,}")
    st.subheader("Confusion matrix (combined system)")
    st.dataframe(pd.DataFrame(
        [[f"{m['tp']:,} (TP)", f"{m['fn']:,} (FN)"], [f"{m['fp']:,} (FP)", f"{m['tn']:,} (TN)"]],
        index=["Actually fraud", "Actually not fraud"], columns=["Alerted", "Not alerted"]),
        width="stretch")
    st.subheader("By rule")
    fmt = num
    st.dataframe(df([{
        "rule": k, "TP": v["tp"], "FP": v["fp"], "FN": v["fn"], "TN": v["tn"],
        "precision": fmt(v["precision"]), "recall": fmt(v["recall"]), "F1": fmt(v["f1"]),
        "alert rate": pct(v['alert_rate'], 4)} for k, v in res.items() if isinstance(v, dict)]),
        hide_index=True, width="stretch")
    high = overview_stats()["severity"].get("HIGH", 0)
    st.subheader("Interpretation")
    st.markdown(
        f"- **Rule A** produces essentially all held-out alerts and detections (precision {pct(res['A_LARGE_AMOUNT']['precision'])}), "
        "yet misses most simulated fraud.\n"
        "- **Rule B** primarily captures a simulator startup artifact: all its alerts fall in steps 1-99, so it "
        "contributes no held-out detection.\n"
        f"- **Rule C** has insufficient meaningful support in the held-out period: {res['C_RAPID_ONWARD']['tp'] + res['C_RAPID_ONWARD']['fp']} "
        f"alert(s), {res['C_RAPID_ONWARD']['tp']} true positive(s).\n"
        f"- **HIGH-severity alerts: {high}.** Scoring was not tuned to populate a severity level.\n"
        "- The data is synthetic; `isFraud` is a simulated label, not a confirmed money-laundering finding.")


{"Overview": view_overview, "Alert Queue": view_queue, "Alert Investigation": view_investigation,
 "Account Network": view_network, "Detection Performance": view_performance}[view]()
