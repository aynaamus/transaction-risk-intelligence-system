"""Command line: python -m tris.cli {ingest,validate}."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from .ingest import IngestionError, ingest
from .validate import format_summary, run_validation

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = ROOT / "data" / "raw" / "PS_20174392719_1491204439457_log.csv"
DEFAULT_DB = ROOT / "data" / "tris.duckdb"


def _in_workspace(p: Path) -> bool:
    return p.resolve().is_relative_to(ROOT)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tris")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("ingest", "validate", "detect", "evaluate", "network"):
        p = sub.add_parser(name)
        p.add_argument("--db", type=Path, default=DEFAULT_DB)
        if name == "network":
            p.add_argument("--row-id", type=int, required=True, help="alerted transaction row_id")
            p.add_argument("--hops", type=int, default=2)
            p.add_argument("--max-edges", type=int, default=200)
            p.add_argument("--out-dir", type=Path, default=ROOT / "data" / "exports")
        if name == "ingest":
            p.add_argument("--csv", type=Path, default=DEFAULT_CSV)
            p.add_argument("--replace", action="store_true",
                           help="rebuild the table even if data is already loaded")
            p.add_argument("--no-validate", action="store_true")
    a = ap.parse_args(argv)

    if not _in_workspace(a.db):
        print(f"Refusing: database must be inside {ROOT}", file=sys.stderr)
        return 2

    if a.cmd in ("detect", "evaluate", "network"):
        return _analytics(a)

    if a.cmd == "ingest":
        try:
            res = ingest(a.csv, a.db, replace=a.replace)
        except IngestionError as e:
            print(f"Ingestion failed: {e}", file=sys.stderr)
            return 1
        print(f"{res.status}: {res.rows:,} rows (sha256 {res.sha256[:16]}...)")
        if a.no_validate:
            return 0

    if not a.db.exists():
        print(f"Database not found: {a.db}", file=sys.stderr)
        return 1
    con = duckdb.connect(str(a.db), read_only=True)
    try:
        result = run_validation(con)
    finally:
        con.close()
    print(format_summary(result))
    return 1 if result["problems"] else 0


def _analytics(a) -> int:
    from .detect import run_detection
    from .evaluate import evaluate, format_evaluation
    from .network import account_for_row, ego_network, export_graphml, export_html

    if not a.db.exists():
        print(f"Database not found: {a.db}", file=sys.stderr)
        return 1
    con = duckdb.connect(str(a.db), read_only=(a.cmd != "detect"))
    try:
        if a.cmd == "detect":
            r = run_detection(con)
            print(f"Unique alerts: {r['total_alerts']:,}")
            print("By rule (a transaction can trigger several):", dict(r["by_rule"]))
            print("By severity:", dict(r["by_severity"]))
            for rule, key, value in r["thresholds"]:
                print(f"  threshold {rule}[{key}] = {value:,.4f}")
        elif a.cmd == "evaluate":
            print(format_evaluation(evaluate(con)))
        else:
            if not _in_workspace(a.out_dir):
                print(f"Refusing: output must be inside {ROOT}", file=sys.stderr)
                return 2
            try:
                accounts = account_for_row(con, a.row_id)
            except KeyError as e:
                print(e, file=sys.stderr)
                return 1
            g = ego_network(con, list(accounts), hops=a.hops, max_edges=a.max_edges)
            a.out_dir.mkdir(parents=True, exist_ok=True)
            html, gml = a.out_dir / f"network_{a.row_id}.html", a.out_dir / f"network_{a.row_id}.graphml"
            export_html(g, html)
            export_graphml(g, gml)
            print(f"{g.number_of_nodes()} accounts, {g.number_of_edges()} observed edges"
                  f"{' (truncated)' if g.graph['truncated'] else ''}\n{html}\n{gml}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
