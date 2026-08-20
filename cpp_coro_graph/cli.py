"""CLI entry: index / viz / export / mcp / status."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, store
from .indexer import index_repo
from .viz import write_html
from .mcp_server import serve


def _default_db(root: Path) -> Path:
    return root / ".cpp-coro-graph" / "graph.db"


def cmd_index(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    db = Path(args.db).resolve() if args.db else _default_db(root)
    rules = Path(args.rules).resolve() if args.rules else None
    stats = index_repo(root, db, rules_path=rules, max_files=args.max_files)
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"\nDB: {db}")
    return 0


def cmd_viz(args: argparse.Namespace) -> int:
    db = Path(args.db).resolve()
    out = Path(args.out).resolve() if args.out else db.with_suffix(".html")
    path = write_html(db, out)
    print(f"wrote {path}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    db = Path(args.db).resolve()
    conn = store.connect(db)
    data = store.export_json(conn)
    conn.close()
    out = Path(args.out).resolve() if args.out else db.with_suffix(".json")
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    db = Path(args.db).resolve()
    conn = store.connect(db)
    print(json.dumps(store.stats(conn), indent=2, ensure_ascii=False))
    conn.close()
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    serve(Path(args.db).resolve())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cpp-coro-graph",
        description=(
            "V1 syntax graph for C++17 coroutines + device domains "
            "(no compile_commands)."
        ),
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("index", help="Index a source tree into SQLite")
    i.add_argument("path", type=str, help="Repo / source root")
    i.add_argument("--db", type=str, default="", help="Output DB path")
    i.add_argument("--rules", type=str, default="", help="devices.json path")
    i.add_argument("--max-files", type=int, default=0, help="Cap files (debug)")
    i.set_defaults(func=cmd_index)

    v = sub.add_parser("viz", help="Write interactive HTML graph")
    v.add_argument("--db", type=str, required=True)
    v.add_argument("--out", type=str, default="")
    v.set_defaults(func=cmd_viz)

    e = sub.add_parser("export", help="Export graph JSON")
    e.add_argument("--db", type=str, required=True)
    e.add_argument("--out", type=str, default="")
    e.set_defaults(func=cmd_export)

    s = sub.add_parser("status", help="Print DB stats")
    s.add_argument("--db", type=str, required=True)
    s.set_defaults(func=cmd_status)

    m = sub.add_parser("mcp", help="Run MCP server on stdio")
    m.add_argument("--db", type=str, required=True)
    m.set_defaults(func=cmd_mcp)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
