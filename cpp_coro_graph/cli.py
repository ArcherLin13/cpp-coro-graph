"""CLI entry: index / viz / export / mcp / status."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__


def log(msg: str) -> None:
    print(f"[cpp-coro-graph] {msg}", file=sys.stderr, flush=True)


def _default_db(root: Path) -> Path:
    return root / ".cpp-coro-graph" / "graph.db"


def cmd_index(args: argparse.Namespace) -> int:
    from .indexer import index_repo

    root = Path(args.path).resolve()
    log(f"command=index path={args.path} -> {root}")
    if not root.is_dir():
        log(f"ERROR: not a directory: {root}")
        return 2
    db = Path(args.db).resolve() if args.db else _default_db(root)
    rules = Path(args.rules).resolve() if args.rules else None
    if rules is not None:
        log(f"rules={rules}")
    stats = index_repo(root, db, rules_path=rules, max_files=args.max_files)
    print(json.dumps(stats, indent=2, ensure_ascii=False), flush=True)
    print(f"\nDB: {db}", flush=True)
    return 0


def cmd_viz(args: argparse.Namespace) -> int:
    from . import store
    from .viz import write_html

    db = Path(args.db).resolve()
    log(f"command=viz db={db}")
    if not db.is_file():
        log(f"ERROR: db not found: {db}")
        return 2
    out = Path(args.out).resolve() if args.out else db.with_suffix(".html")
    path = write_html(db, out)
    log(f"wrote {path}")
    print(f"wrote {path}", flush=True)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from . import store

    db = Path(args.db).resolve()
    log(f"command=export db={db}")
    if not db.is_file():
        log(f"ERROR: db not found: {db}")
        return 2
    conn = store.connect(db)
    data = store.export_json(conn)
    conn.close()
    out = Path(args.out).resolve() if args.out else db.with_suffix(".json")
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"wrote {out}")
    print(f"wrote {out}", flush=True)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from . import store

    db = Path(args.db).resolve()
    log(f"command=status db={db}")
    if not db.is_file():
        log(f"ERROR: db not found: {db}")
        return 2
    conn = store.connect(db)
    print(json.dumps(store.stats(conn), indent=2, ensure_ascii=False), flush=True)
    conn.close()
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import serve

    db = Path(args.db).resolve()
    log(f"command=mcp db={db} (waiting on stdin JSON-RPC)")
    if not db.is_file():
        log(f"ERROR: db not found: {db}")
        return 2
    serve(db)
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
    sub = p.add_subparsers(dest="cmd", required=False)

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


def main(argv: list[str] | None = None) -> None:
    print(
        f"[cpp-coro-graph] starting v{__version__}  "
        f"argv={argv if argv is not None else sys.argv[1:]}",
        file=sys.stderr,
        flush=True,
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        print(
            "\n[cpp-coro-graph] tip: need a subcommand, e.g.\n"
            "  python3 -m cpp_coro_graph index /path/to/repo\n"
            "  python3 -m cpp_coro_graph viz --db /path/to/repo/.cpp-coro-graph/graph.db\n",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2)
    raise SystemExit(int(args.func(args)))


if __name__ == "__main__":
    main()
