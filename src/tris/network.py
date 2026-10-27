"""Bounded account-network retrieval from observed transactions.

Edges are *observed* origin->destination transactions between account ids.
They do not show ownership, control or coordination. Every edge keeps the
row_ids of the transactions behind it. The graph is built only from a
bounded neighbourhood query, never from the whole dataset.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import networkx as nx

from .schema import TABLE


def account_for_row(con, row_id: int) -> tuple[str, str]:
    r = con.execute(f"SELECT nameOrig, nameDest FROM {TABLE} WHERE row_id = ?", [row_id]).fetchone()
    if r is None:
        raise KeyError(f"No transaction with row_id {row_id}")
    return r


def ego_network(
    con: duckdb.DuckDBPyConnection,
    accounts: str | list[str],
    hops: int = 2,
    max_edges: int = 200,
    max_rows_per_edge: int = 20,
) -> nx.DiGraph:
    """Aggregated directed graph within `hops` of the given account(s).

    Total edges are capped at max_edges (highest total amount kept first);
    `graph.graph['truncated']` is True if the cap cut anything off.
    """
    frontier = {accounts} if isinstance(accounts, str) else set(accounts)
    seen = set(frontier)
    edges: dict[tuple[str, str], tuple] = {}
    truncated = False
    for _ in range(hops):
        if not frontier:
            break
        rows = con.execute(
            f"""SELECT nameOrig, nameDest, count(*), sum(amount), min(step), max(step),
                       list(row_id ORDER BY row_id)[:{int(max_rows_per_edge)}]
                FROM {TABLE}
                WHERE nameOrig IN (SELECT unnest(?)) OR nameDest IN (SELECT unnest(?))
                GROUP BY 1, 2 ORDER BY sum(amount) DESC LIMIT {int(max_edges) + 1}""",
            [list(frontier), list(frontier)],
        ).fetchall()
        nxt = set()
        for o, d, n, total, lo, hi, ids in rows:
            if (o, d) in edges:
                continue
            if len(edges) >= max_edges:
                truncated = True
                break
            edges[(o, d)] = (n, total, lo, hi, ids)
            nxt |= {o, d} - seen
        seen |= nxt
        frontier = nxt
    g = nx.DiGraph(roots=sorted({accounts} if isinstance(accounts, str) else accounts),
                   truncated=truncated,
                   note="Observed transactions only; no ownership or coordination implied.")
    for (o, d), (n, total, lo, hi, ids) in edges.items():
        g.add_edge(o, d, tx_count=n, total_amount=total, first_step=lo, last_step=hi, row_ids=ids)
    return g


def chains(g: nx.DiGraph, max_len: int = 4) -> list[list[str]]:
    """Simple directed paths of 3..max_len accounts (observed hops only)."""
    out = []
    for s in g.nodes:
        for t in g.nodes:
            if s != t:
                out += [p for p in nx.all_simple_paths(g, s, t, cutoff=max_len - 1) if len(p) >= 3]
    return sorted(out)


def export_graphml(g: nx.DiGraph, path: str | Path) -> None:
    h = nx.DiGraph(**{k: str(v) for k, v in g.graph.items()})
    for o, d, a in g.edges(data=True):
        h.add_edge(o, d, **{k: (",".join(map(str, v)) if k == "row_ids" else v) for k, v in a.items()})
    nx.write_graphml(h, str(path))


def _layout(g: nx.DiGraph) -> dict:
    """Pure-Python layout (no NumPy): root accounts in the middle, rest on rings by hop distance."""
    import math

    roots = [n for n in g.graph.get("roots", []) if n in g]
    dist = {r: 0 for r in roots}
    for r in roots:
        for n, d in nx.single_source_shortest_path_length(g.to_undirected(as_view=True), r).items():
            dist[n] = min(dist.get(n, d), d)
    rings: dict[int, list] = {}
    for n in sorted(g.nodes):
        rings.setdefault(dist.get(n, 1), []).append(n)
    pos = {}
    for d, members in rings.items():
        for i, n in enumerate(members):
            if d == 0 and len(members) == 1:
                pos[n] = (0.0, 0.0)
            else:
                ang = 2 * math.pi * i / len(members)
                r = max(d, 1) if d else 0.3
                pos[n] = (r * math.cos(ang), r * math.sin(ang))
    return pos


def build_figure(g: nx.DiGraph):
    """Plotly figure: directed edges (arrows), hover shows count, amount, steps, row_ids."""
    import plotly.graph_objects as go

    pos = _layout(g)
    ex, ey, mx, my, mt = [], [], [], [], []
    for o, d, a in g.edges(data=True):
        ex += [pos[o][0], pos[d][0], None]
        ey += [pos[o][1], pos[d][1], None]
        mx.append((pos[o][0] + pos[d][0]) / 2)
        my.append((pos[o][1] + pos[d][1]) / 2)
        mt.append(f"{o} → {d}<br>{a['tx_count']} tx, total {a['total_amount']:,.2f}<br>"
                  f"steps {a['first_step']}-{a['last_step']}<br>row_ids {a['row_ids']}")
    roots = set(g.graph.get("roots", []))
    nodes = list(g.nodes)
    fig = go.Figure([
        go.Scatter(x=ex, y=ey, mode="lines", line=dict(width=1, color="#888"), hoverinfo="none"),
        go.Scatter(x=mx, y=my, mode="markers", marker=dict(size=6, color="#888"),
                   text=mt, hoverinfo="text", name="edges"),
        go.Scatter(x=[pos[n][0] for n in nodes], y=[pos[n][1] for n in nodes],
                   mode="markers+text", text=nodes, textposition="top center",
                   marker=dict(size=[16 if n in roots else 10 for n in nodes],
                               color=["#d62728" if n in roots else "#1f77b4" for n in nodes]),
                   name="accounts"),
    ])
    fig.update_layout(
        title="Observed transaction network (no ownership or coordination implied)"
              + (" — truncated" if g.graph.get("truncated") else ""),
        showlegend=False, xaxis=dict(visible=False), yaxis=dict(visible=False))
    for o, d in g.edges:
        fig.add_annotation(x=pos[d][0], y=pos[d][1], ax=pos[o][0], ay=pos[o][1],
                           xref="x", yref="y", axref="x", ayref="y", showarrow=True,
                           arrowhead=3, arrowsize=1.2, arrowwidth=1, arrowcolor="#888",
                           standoff=8, startstandoff=8)
    return fig


def export_html(g: nx.DiGraph, path: str | Path, offline: bool = False) -> None:
    """Interactive Plotly HTML. offline=True embeds plotly.js (no CDN, ~4 MB)."""
    build_figure(g).write_html(str(path), include_plotlyjs=True if offline else "cdn")
