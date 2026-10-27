import duckdb
import pytest

from tris.ingest import ingest
from tris.network import account_for_row, chains, ego_network, export_graphml, export_html

from conftest import HEADER


def row(step, typ, amount, orig, dest):
    return f"{step},{typ},{amount},{orig},0,0,{dest},0,0,0,0"


@pytest.fixture
def con(make_csv, tmp_path):
    rows = [row(1, "TRANSFER", 100, "A", "B"), row(2, "TRANSFER", 50, "A", "B"),
            row(3, "CASH_OUT", 80, "B", "C"), row(4, "CASH_OUT", 70, "C", "D"),
            row(5, "PAYMENT", 9, "X", "Y")]
    db = tmp_path / "n.duckdb"
    ingest(make_csv(rows), db)
    c = duckdb.connect(str(db), read_only=True)
    yield c
    c.close()


def test_edges_aggregate_and_link_to_row_ids(con):
    g = ego_network(con, "B", hops=1)
    assert set(g.edges) == {("A", "B"), ("B", "C")}
    e = g["A"]["B"]
    assert e["tx_count"] == 2 and e["total_amount"] == 150 and e["row_ids"] == [1, 2]
    assert g["B"]["C"]["row_ids"] == [3]
    assert "X" not in g  # unrelated accounts excluded


def test_hops_bound_and_chains(con):
    assert "D" not in ego_network(con, "B", hops=1)
    g = ego_network(con, "B", hops=2)
    assert "D" in g and ["A", "B", "C"] in chains(g) and ["B", "C", "D"] in chains(g)


def test_max_edges_truncates(con):
    g = ego_network(con, "B", hops=2, max_edges=1)
    assert g.number_of_edges() == 1 and g.graph["truncated"] is True


def test_account_for_row_and_unknown(con):
    assert account_for_row(con, 3) == ("B", "C")
    with pytest.raises(KeyError):
        account_for_row(con, 999)


def test_unknown_account_gives_empty_graph_and_exports(con, tmp_path):
    g = ego_network(con, "NOPE")
    assert g.number_of_edges() == 0
    export_html(g, tmp_path / "e.html")
    g2 = ego_network(con, "B")
    export_graphml(g2, tmp_path / "g.graphml")
    export_html(g2, tmp_path / "g.html")
    assert "row_ids" in (tmp_path / "g.graphml").read_text()
    assert "B" in (tmp_path / "g.html").read_text()
