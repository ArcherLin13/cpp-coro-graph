"""CLI entry: index / viz / query / callers / callees / explore / impact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__


def log(msg: str) -> None:
    print(f"[cpp-coro-graph] {msg}", file=sys.stderr, flush=True)


def _default_db(root: Path | None = None) -> Path:
    base = root or Path.cwd()
    return base / ".cpp-coro-graph" / "graph.db"


def resolve_db(db_arg: str) -> Path:
    if db_arg:
        p = Path(db_arg).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"db not found: {p}")
        return p
    # walk up from cwd looking for .cpp-coro-graph/graph.db
    cur = Path.cwd().resolve()
    for _ in range(8):
        cand = cur / ".cpp-coro-graph" / "graph.db"
        if cand.is_file():
            return cand
        if cur.parent == cur:
            break
        cur = cur.parent
    raise FileNotFoundError(
        "no --db given and no .cpp-coro-graph/graph.db found upward from cwd"
    )


def _parse_kinds(s: str, default: list[str] | None = None) -> list[str]:
    from .query import ALL_EDGES, CONTROL_EDGES

    default = list(default) if default is not None else list(CONTROL_EDGES)
    s = (s or "").strip().lower()
    if not s or s in {"control", "default"}:
        return default
    if s == "all":
        return list(ALL_EDGES)
    alias = {
        "seq": "await",
        "spawns": "calls",
        "handoff": "calls",
        "device_call": "calls",
    }
    out: list[str] = []
    for p in s.split(","):
        p = alias.get(p.strip(), p.strip())
        if p and p not in out:
            out.append(p)
    return out or default


def _print_nodes(nodes: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(nodes, indent=2, ensure_ascii=False), flush=True)
        return
    if not nodes:
        print("(no matches)", flush=True)
        return
    for n in nodes:
        loc = f"{n.get('file_path', '')}:{n.get('start_line', '')}"
        ns = n.get("namespace") or ""
        print(
            f"{n.get('qualified_name')}  [{n.get('kind')}]  "
            f"domain={n.get('domain', '-')}  ns={ns or '-'}  {loc}",
            flush=True,
        )


def _print_edges(rows: list[dict], *, direction: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False), flush=True)
        return
    if not rows:
        print("(none)", flush=True)
        return
    for r in rows:
        loc = f"{r.get('edge_file', r.get('file_path', ''))}:{r.get('edge_line', r.get('line', ''))}"
        sig = r.get("signature", "") or ""
        sig_str = (
            f"  {sig[:80]}"
            if sig and sig != r.get("qualified_name", "")
            else ""
        )
        print(
            f"  [{r.get('edge_kind')}]  {r.get('qualified_name')}  "
            f"({r.get('kind')})  {loc}{sig_str}",
            flush=True,
        )


def _count_hidden(
    conn, node_id: str, direction: str, edge_kinds: list[str]
) -> int:
    """Count unresolved neighbors hidden by hide_unresolved."""
    if direction == "callers":
        sql = (
            "SELECT COUNT(*) AS c FROM edges e JOIN nodes s ON s.id=e.source "
            "WHERE e.target=? AND s.kind='unresolved'"
        )
    else:
        sql = (
            "SELECT COUNT(*) AS c FROM edges e JOIN nodes t ON t.id=e.target "
            "WHERE e.source=? AND t.kind='unresolved'"
        )
    args: list = [node_id]
    if edge_kinds:
        placeholders = ",".join("?" * len(edge_kinds))
        sql += f" AND e.kind IN ({placeholders})"
        args.extend(edge_kinds)
    row = conn.execute(sql, args).fetchone()
    return int(row["c"]) if row else 0


def _print_hidden_hint(hidden: int, flag: str = "--show-unresolved") -> None:
    if hidden > 0:
        print(
            f"# ({hidden} unresolved neighbors hidden — use {flag} to show)",
            flush=True,
        )


def _print_no_match_hint(conn, keyword: str) -> None:
    """Show 'did you mean?' suggestions when no symbol matches."""
    from difflib import get_close_matches

    rows = conn.execute(
        "SELECT DISTINCT qualified_name, name FROM nodes "
        "WHERE kind IN ('coroutine','function','declaration') "
        "ORDER BY length(qualified_name) LIMIT 2000"
    ).fetchall()
    simple_to_qname: dict[str, str] = {}
    for r in rows:
        simple_to_qname.setdefault(r["name"], r["qualified_name"])
    close_simple = get_close_matches(
        keyword, list(simple_to_qname.keys()), n=5, cutoff=0.4
    )
    if close_simple:
        print(f"# no exact match for '{keyword}'. Did you mean:", flush=True)
        for s in close_simple:
            print(f"#   {simple_to_qname[s]}", flush=True)


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
    from .viz import write_html

    try:
        db = resolve_db(args.db)
    except FileNotFoundError as e:
        log(f"ERROR: {e}")
        return 2
    log(f"command=viz db={db}")
    out = Path(args.out).resolve() if args.out else db.with_suffix(".html")
    path = write_html(
        db,
        out,
        module=getattr(args, "module", "") or "",
        around=getattr(args, "around", "") or "",
        depth=getattr(args, "depth", 1) or 1,
        full=bool(getattr(args, "full", False)),
    )
    print(f"wrote {path}", flush=True)
    if not args.full:
        print(
            "open in browser: pick module / double-click node to expand await trunk",
            flush=True,
        )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from . import store

    try:
        db = resolve_db(args.db)
    except FileNotFoundError as e:
        log(f"ERROR: {e}")
        return 2
    conn = store.connect(db)
    data = store.export_json(conn)
    conn.close()
    out = Path(args.out).resolve() if args.out else db.with_suffix(".json")
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out}", flush=True)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from . import store

    try:
        db = resolve_db(args.db)
    except FileNotFoundError as e:
        log(f"ERROR: {e}")
        return 2
    conn = store.connect(db)
    print(json.dumps(store.stats(conn), indent=2, ensure_ascii=False), flush=True)
    conn.close()
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import serve

    try:
        db = resolve_db(args.db)
    except FileNotFoundError as e:
        log(f"ERROR: {e}")
        return 2
    log(f"command=mcp db={db} (waiting on stdin JSON-RPC)")
    serve(db)
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    from . import store
    from . import query as Q

    try:
        db = resolve_db(args.db)
    except FileNotFoundError as e:
        log(f"ERROR: {e}")
        return 2
    conn = store.connect(db)
    nodes = Q.find_nodes(
        conn, args.keyword, kind=args.kind or None, limit=args.limit
    )
    conn.close()
    _print_nodes(nodes, args.json)
    return 0 if nodes else 1


def cmd_callers(args: argparse.Namespace) -> int:
    from . import store
    from . import query as Q

    try:
        db = resolve_db(args.db)
    except FileNotFoundError as e:
        log(f"ERROR: {e}")
        return 2
    conn = store.connect(db)
    matches = Q.find_nodes(conn, args.keyword, limit=20)
    primary = Q.pick_primary(matches, args.keyword)
    if not primary:
        print("(no symbol match)", flush=True)
        _print_no_match_hint(conn, args.keyword)
        conn.close()
        return 1
    if not args.json:
        print(
            f"# callers of {primary['qualified_name']}  "
            f"({primary['file_path']}:{primary['start_line']})",
            flush=True,
        )
        if len(matches) > 1:
            print(f"# note: {len(matches)} matches; using best hit", flush=True)
    edge_kinds = _parse_kinds(args.edge_kind)
    rows = Q.dedupe_neighbors(
        Q.neighbors(
            conn,
            primary["id"],
            direction="callers",
            edge_kinds=edge_kinds,
            limit=args.limit,
            hide_unresolved=not args.show_unresolved,
        )
    )
    if not args.show_unresolved and not args.json and not rows:
        hidden = _count_hidden(conn, primary["id"], "callers", edge_kinds)
        _print_hidden_hint(hidden)
    conn.close()
    if args.json:
        print(
            json.dumps({"symbol": primary, "callers": rows}, indent=2, ensure_ascii=False),
            flush=True,
        )
    else:
        _print_edges(rows, direction="callers", as_json=False)
    return 0


def cmd_callees(args: argparse.Namespace) -> int:
    from . import store
    from . import query as Q

    try:
        db = resolve_db(args.db)
    except FileNotFoundError as e:
        log(f"ERROR: {e}")
        return 2
    conn = store.connect(db)
    matches = Q.find_nodes(conn, args.keyword, limit=20)
    primary = Q.pick_primary(matches, args.keyword)
    if not primary:
        print("(no symbol match)", flush=True)
        _print_no_match_hint(conn, args.keyword)
        conn.close()
        return 1
    if not args.json:
        print(
            f"# callees of {primary['qualified_name']}  "
            f"({primary['file_path']}:{primary['start_line']})",
            flush=True,
        )
    edge_kinds = _parse_kinds(args.edge_kind)
    rows = Q.dedupe_neighbors(
        Q.neighbors(
            conn,
            primary["id"],
            direction="callees",
            edge_kinds=edge_kinds,
            limit=args.limit,
            hide_unresolved=not args.show_unresolved,
        )
    )
    if not args.show_unresolved and not args.json:
        hidden = _count_hidden(conn, primary["id"], "callees", edge_kinds)
        _print_hidden_hint(hidden)
    conn.close()
    if args.json:
        print(
            json.dumps({"symbol": primary, "callees": rows}, indent=2, ensure_ascii=False),
            flush=True,
        )
    else:
        _print_edges(rows, direction="callees", as_json=False)
    return 0


def cmd_explore(args: argparse.Namespace) -> int:
    from . import store
    from . import query as Q

    try:
        db = resolve_db(args.db)
    except FileNotFoundError as e:
        log(f"ERROR: {e}")
        return 2
    conn = store.connect(db)
    result = Q.explore(
        conn,
        args.keyword,
        depth=args.depth,
        limit=args.limit,
        edge_kinds=_parse_kinds(args.edge_kind),
        hide_unresolved=not args.show_unresolved,
    )
    conn.close()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
        return 0 if result.get("match") else 1

    m = result.get("match")
    if not m:
        print("(no symbol match)", flush=True)
        return 1
    print(
        f"# explore {m['qualified_name']}  depth={result['depth']}  "
        f"nodes={len(result['nodes'])} edges={len(result['edges'])}",
        flush=True,
    )
    print(f"# file {m['file_path']}:{m['start_line']}  kind={m['kind']}", flush=True)
    print("\n## neighborhood edges", flush=True)
    id_to_q = {n["id"]: n["qualified_name"] for n in result["nodes"]}
    for e in result["edges"][: args.limit * 4]:
        print(
            f"  {id_to_q.get(e['source'], e['source'])}  "
            f"-[{e['kind']}]->  {id_to_q.get(e['target'], e['target'])}  "
            f"@{e['file_path']}:{e['line']}",
            flush=True,
        )
    return 0


def cmd_impact(args: argparse.Namespace) -> int:
    from . import store
    from . import query as Q

    try:
        db = resolve_db(args.db)
    except FileNotFoundError as e:
        log(f"ERROR: {e}")
        return 2
    conn = store.connect(db)
    result = Q.impact(
        conn,
        args.keyword,
        depth=args.depth,
        limit=args.limit,
        hide_unresolved=not args.show_unresolved,
    )
    conn.close()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
        return 0 if result.get("match") else 1
    m = result.get("match")
    if not m:
        print("(no symbol match)", flush=True)
        return 1
    print(f"# impact radius of {m['qualified_name']}  depth<={args.depth}", flush=True)
    for a in result["affected"]:
        print(
            f"  d{a['depth']} [{a['via_edge']}] {a['qualified_name']}  "
            f"{a['file_path']}:{a['start_line']}",
            flush=True,
        )
    if not result["affected"]:
        print("(no incoming callers found)", flush=True)
    return 0


def _add_db_arg(p: argparse.ArgumentParser, required: bool = False) -> None:
    p.add_argument(
        "--db",
        type=str,
        default="",
        required=required,
        help="Path to graph.db (default: search upward for .cpp-coro-graph/graph.db)",
    )


def _add_query_common(p: argparse.ArgumentParser) -> None:
    _add_db_arg(p)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument(
        "--show-unresolved",
        action="store_true",
        help="Include unresolved::* stubs",
    )
    p.add_argument(
        "--edge-kind",
        type=str,
        default="control",
        help="Edge filter: control (=calls,await) | all | calls,await,contains,inherits",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cpp-coro-graph",
        description=(
            "C++ coroutine + call/device graph. "
            "Index a tree, then query like codegraph (query/callers/callees/explore)."
        ),
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Less boot logging on stderr",
    )
    sub = p.add_subparsers(dest="cmd", required=False)

    i = sub.add_parser("index", help="Index a source tree into SQLite")
    i.add_argument("path", type=str, help="Repo / source root")
    i.add_argument("--db", type=str, default="", help="Output DB path")
    i.add_argument("--rules", type=str, default="", help="devices.json path")
    i.add_argument("--max-files", type=int, default=0, help="Cap files (debug)")
    i.set_defaults(func=cmd_index)

    v = sub.add_parser(
        "viz",
        help="Write interactive HTML: module entries + await trunk (not full graph)",
    )
    _add_db_arg(v)
    v.add_argument("--out", type=str, default="")
    v.add_argument(
        "--module",
        type=str,
        default="",
        help="Module directory prefix (e.g. camera/sos or . for repo-root files)",
    )
    v.add_argument(
        "--around",
        type=str,
        default="",
        help="Pin a single entry symbol (keyword)",
    )
    v.add_argument(
        "--depth",
        type=int,
        default=1,
        help="Initial await trunk depth from entries (default 1)",
    )
    v.add_argument(
        "--full",
        action="store_true",
        help="Embed entire DB graph (slow/unreadable on large repos)",
    )
    v.set_defaults(func=cmd_viz)

    e = sub.add_parser("export", help="Export graph JSON")
    _add_db_arg(e)
    e.add_argument("--out", type=str, default="")
    e.set_defaults(func=cmd_export)

    s = sub.add_parser("status", help="Print DB stats")
    _add_db_arg(s)
    s.set_defaults(func=cmd_status)

    m = sub.add_parser("mcp", help="Run MCP server on stdio")
    _add_db_arg(m)
    m.set_defaults(func=cmd_mcp)

    qq = sub.add_parser("query", help="Search symbols by keyword")
    qq.add_argument("keyword", type=str)
    qq.add_argument("--kind", type=str, default="", help="node kind filter")
    _add_query_common(qq)
    qq.set_defaults(func=cmd_query)

    ca = sub.add_parser("callers", help="Who calls / awaits / handoffs to this symbol")
    ca.add_argument("keyword", type=str)
    _add_query_common(ca)
    ca.set_defaults(func=cmd_callers)

    ce = sub.add_parser("callees", help="What this symbol calls / awaits / spawns")
    ce.add_argument("keyword", type=str)
    _add_query_common(ce)
    ce.set_defaults(func=cmd_callees)

    ex = sub.add_parser("explore", help="Neighborhood around a symbol (BFS)")
    ex.add_argument("keyword", type=str)
    ex.add_argument("--depth", type=int, default=1)
    _add_query_common(ex)
    ex.set_defaults(func=cmd_explore)

    im = sub.add_parser("impact", help="Incoming blast radius if symbol changes")
    im.add_argument("keyword", type=str)
    im.add_argument("--depth", type=int, default=2)
    _add_query_common(im)
    im.set_defaults(func=cmd_impact)

    return p


def main(argv: list[str] | None = None) -> None:
    # Pre-parse quiet without consuming subcommand
    quiet = False
    raw = list(argv) if argv is not None else sys.argv[1:]
    if "-q" in raw or "--quiet" in raw:
        quiet = True
    if not quiet:
        print(
            f"[cpp-coro-graph] starting v{__version__}  argv={raw}",
            file=sys.stderr,
            flush=True,
        )
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        print(
            "\n[cpp-coro-graph] examples:\n"
            "  python3 -m cpp_coro_graph index /path/to/repo\n"
            "  python3 -m cpp_coro_graph -q query OnSos --db .../graph.db\n"
            "  python3 -m cpp_coro_graph -q callers OnSos --db .../graph.db\n"
            "  python3 -m cpp_coro_graph -q callees OnSos --db .../graph.db\n"
            "  python3 -m cpp_coro_graph -q explore OnSos --depth 2 --db .../graph.db\n",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2)
    raise SystemExit(int(args.func(args)))


if __name__ == "__main__":
    main()
