"""Read-only query helpers over the graph DB (callers / callees / explore)."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

# Canonical edge model (v0.4+)
CONTROL_EDGES = ["calls", "await"]
STRUCT_EDGES = ["contains", "inherits"]
ALL_EDGES = CONTROL_EDGES + STRUCT_EDGES


def row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


def dedupe_neighbors(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per neighbor id; prefer await over calls."""
    rank = {"await": 0, "calls": 1, "inherits": 2, "contains": 3}
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        nid = r["id"]
        prev = best.get(nid)
        if prev is None or rank.get(r.get("edge_kind"), 9) < rank.get(
            prev.get("edge_kind"), 9
        ):
            best[nid] = r
    return sorted(
        best.values(),
        key=lambda x: (x.get("edge_kind") or "", x.get("qualified_name") or ""),
    )


def find_nodes(
    conn: sqlite3.Connection,
    keyword: str,
    *,
    kind: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    q = f"%{keyword}%"
    sql = (
        "SELECT id, name, qualified_name, kind, file_path, start_line, end_line, "
        "domain, backend, namespace, signature "
        "FROM nodes WHERE (name LIKE ? OR qualified_name LIKE ? OR file_path LIKE ?)"
    )
    args: list[Any] = [q, q, q]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY length(qualified_name), qualified_name LIMIT ?"
    args.append(limit)
    return [row_to_dict(r) for r in conn.execute(sql, args).fetchall()]


def pick_primary(nodes: list[dict[str, Any]], keyword: str) -> dict[str, Any] | None:
    if not nodes:
        return None
    kw = keyword.lower()
    for n in nodes:
        if n["qualified_name"].lower() == kw or n["name"].lower() == kw:
            return n
    for n in nodes:
        if n["name"].lower() == kw.split("::")[-1]:
            return n
    return nodes[0]


def neighbors(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    direction: str,
    edge_kinds: list[str] | None = None,
    limit: int = 100,
    hide_unresolved: bool = False,
) -> list[dict[str, Any]]:
    """direction: callers (incoming) | callees (outgoing)."""
    if direction == "callers":
        sql = (
            "SELECT e.kind AS edge_kind, e.line AS edge_line, e.file_path AS edge_file, "
            "s.id, s.name, s.qualified_name, s.kind, s.file_path, s.start_line, "
            "s.domain, s.namespace, s.signature "
            "FROM edges e JOIN nodes s ON s.id = e.source "
            "WHERE e.target = ?"
        )
    else:
        sql = (
            "SELECT e.kind AS edge_kind, e.line AS edge_line, e.file_path AS edge_file, "
            "t.id, t.name, t.qualified_name, t.kind, t.file_path, t.start_line, "
            "t.domain, t.namespace, t.signature "
            "FROM edges e JOIN nodes t ON t.id = e.target "
            "WHERE e.source = ?"
        )
    args: list[Any] = [node_id]
    if edge_kinds:
        placeholders = ",".join("?" * len(edge_kinds))
        sql += f" AND e.kind IN ({placeholders})"
        args.extend(edge_kinds)
    if hide_unresolved:
        col = "s" if direction == "callers" else "t"
        sql += f" AND {col}.kind != 'unresolved'"
    # Control-flow queries should never surface file: nodes
    if edge_kinds is not None and set(edge_kinds) <= {"calls", "await"}:
        col = "s" if direction == "callers" else "t"
        sql += f" AND {col}.kind != 'file' AND {col}.qualified_name NOT LIKE 'file:%'"
    sql += " ORDER BY e.kind, qualified_name LIMIT ?"
    args.append(limit)
    return [row_to_dict(r) for r in conn.execute(sql, args).fetchall()]


def explore(
    conn: sqlite3.Connection,
    keyword: str,
    *,
    depth: int = 1,
    limit: int = 50,
    edge_kinds: list[str] | None = None,
    hide_unresolved: bool = True,
) -> dict[str, Any]:
    """BFS neighborhood around the best-matching symbol."""
    matches = find_nodes(conn, keyword, limit=20)
    primary = pick_primary(matches, keyword)
    if not primary:
        return {"query": keyword, "match": None, "matches": [], "nodes": [], "edges": []}

    edge_kinds = edge_kinds or list(CONTROL_EDGES)
    seen = {primary["id"]}
    frontier = {primary["id"]}
    nodes = {primary["id"]: primary}
    edges: list[dict[str, Any]] = []

    for _ in range(max(1, depth)):
        nxt: set[str] = set()
        for nid in frontier:
            for direction in ("callees", "callers"):
                for nb in neighbors(
                    conn,
                    nid,
                    direction=direction,
                    edge_kinds=edge_kinds,
                    limit=limit,
                    hide_unresolved=hide_unresolved,
                ):
                    other_id = nb["id"]
                    if hide_unresolved and nb.get("kind") == "unresolved":
                        continue
                    nodes[other_id] = {
                        "id": nb["id"],
                        "name": nb["name"],
                        "qualified_name": nb["qualified_name"],
                        "kind": nb["kind"],
                        "file_path": nb["file_path"],
                        "start_line": nb["start_line"],
                        "domain": nb["domain"],
                        "namespace": nb.get("namespace", ""),
                    }
                    if direction == "callees":
                        edges.append(
                            {
                                "source": nid,
                                "target": other_id,
                                "kind": nb["edge_kind"],
                                "file_path": nb["edge_file"],
                                "line": nb["edge_line"],
                            }
                        )
                    else:
                        edges.append(
                            {
                                "source": other_id,
                                "target": nid,
                                "kind": nb["edge_kind"],
                                "file_path": nb["edge_file"],
                                "line": nb["edge_line"],
                            }
                        )
                    if other_id not in seen:
                        seen.add(other_id)
                        nxt.add(other_id)
        frontier = nxt

    # dedupe edges
    uniq = []
    seen_e = set()
    for e in edges:
        key = (e["source"], e["target"], e["kind"])
        if key in seen_e:
            continue
        seen_e.add(key)
        uniq.append(e)

    return {
        "query": keyword,
        "match": primary,
        "matches": matches,
        "depth": depth,
        "nodes": list(nodes.values()),
        "edges": uniq[: limit * 20],
    }


def impact(
    conn: sqlite3.Connection,
    keyword: str,
    *,
    depth: int = 2,
    limit: int = 80,
    hide_unresolved: bool = True,
) -> dict[str, Any]:
    """Who would be affected if this symbol changes (incoming BFS)."""
    matches = find_nodes(conn, keyword, limit=20)
    primary = pick_primary(matches, keyword)
    if not primary:
        return {"query": keyword, "match": None, "affected": []}

    seen = {primary["id"]}
    frontier = {primary["id"]}
    affected: list[dict[str, Any]] = []
    for d in range(max(1, depth)):
        nxt: set[str] = set()
        for nid in frontier:
            for nb in neighbors(
                conn,
                nid,
                direction="callers",
                edge_kinds=list(CONTROL_EDGES),
                limit=limit,
                hide_unresolved=hide_unresolved,
            ):
                if nb["id"] in seen:
                    continue
                seen.add(nb["id"])
                nxt.add(nb["id"])
                item = {
                    "id": nb["id"],
                    "qualified_name": nb["qualified_name"],
                    "kind": nb["kind"],
                    "file_path": nb["file_path"],
                    "start_line": nb["start_line"],
                    "via_edge": nb["edge_kind"],
                    "depth": d + 1,
                }
                affected.append(item)
        frontier = nxt
    return {"query": keyword, "match": primary, "affected": affected}


# --- Module / trunk helpers for ego viz ---------------------------------

_ENTRY_NAME_RE = re.compile(
    r"^(?:Call|Entry|Main|Process|Start|Execute|On[A-Z]\w*|Run[A-Z]\w*)$"
)


def list_modules(conn: sqlite3.Connection, *, max_depth: int = 3) -> list[str]:
    """Directory prefixes from node file_path (forward-slash)."""
    dirs: set[str] = set()
    has_root_files = False
    for (fp,) in conn.execute(
        "SELECT DISTINCT file_path FROM nodes "
        "WHERE kind IN ('function','coroutine','declaration') AND file_path != ''"
    ):
        p = str(fp).replace("\\", "/")
        if p.startswith("file:"):
            continue
        parts = [x for x in p.split("/") if x]
        if len(parts) <= 1:
            has_root_files = True
            continue
        for i in range(1, min(len(parts), max_depth + 1)):
            dirs.add("/".join(parts[:i]))
        dirs.add("/".join(parts[:-1]))
    if has_root_files:
        dirs.add(".")
    return sorted(dirs)


def _in_module(file_path: str, module: str) -> bool:
    fp = (file_path or "").replace("\\", "/")
    mod = (module or "").replace("\\", "/").rstrip("/")
    if not mod:
        return True
    if mod == ".":
        return "/" not in fp and not fp.startswith("file:")
    return fp == mod or fp.startswith(mod + "/")


def control_adjacency(
    conn: sqlite3.Connection,
    *,
    edge_kinds: list[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """node_id -> outgoing control edges [{to, kind, line}]."""
    kinds = edge_kinds or list(CONTROL_EDGES)
    placeholders = ",".join("?" * len(kinds))
    adj: dict[str, list[dict[str, Any]]] = {}
    sql = (
        "SELECT e.source, e.target, e.kind, e.line "
        "FROM edges e "
        "JOIN nodes t ON t.id = e.target "
        f"WHERE e.kind IN ({placeholders}) "
        "AND t.kind NOT IN ('file','unresolved') "
        "AND t.qualified_name NOT LIKE 'file:%'"
    )
    for r in conn.execute(sql, kinds):
        adj.setdefault(r["source"], []).append(
            {"to": r["target"], "kind": r["kind"], "line": r["line"]}
        )
    return adj


def module_nodes(
    conn: sqlite3.Connection,
    module: str,
    *,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, qualified_name, kind, file_path, start_line, end_line, "
        "domain, backend, namespace, signature FROM nodes "
        "WHERE kind IN ('function','coroutine','declaration') "
        "ORDER BY qualified_name LIMIT ?",
        (limit * 4,),
    ).fetchall()
    out = [row_to_dict(r) for r in rows if _in_module(r["file_path"], module)]
    return out[:limit]


def module_entries(
    conn: sqlite3.Connection,
    module: str,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Rank likely entry points inside a module directory."""
    nodes = module_nodes(conn, module)
    if not nodes:
        return []

    ids = {n["id"] for n in nodes}
    out_count: dict[str, int] = {n["id"]: 0 for n in nodes}
    out_await: dict[str, int] = {n["id"]: 0 for n in nodes}
    external_in: dict[str, int] = {n["id"]: 0 for n in nodes}
    internal_in: dict[str, int] = {n["id"]: 0 for n in nodes}
    all_files = {
        r["id"]: r["file_path"]
        for r in conn.execute("SELECT id, file_path FROM nodes")
    }
    for r in conn.execute(
        "SELECT source, target, kind FROM edges WHERE kind IN ('calls','await')"
    ):
        if r["source"] in out_count:
            out_count[r["source"]] += 1
            if r["kind"] == "await":
                out_await[r["source"]] += 1
        if r["target"] not in ids:
            continue
        src_fp = all_files.get(r["source"], "")
        if _in_module(src_fp, module):
            internal_in[r["target"]] += 1
        else:
            external_in[r["target"]] += 1

    scored: list[tuple[int, dict[str, Any]]] = []
    for n in nodes:
        if n["kind"] == "declaration" and out_count.get(n["id"], 0) == 0:
            continue
        name = n["name"] or ""
        qn = n["qualified_name"] or ""
        score = 0
        if _ENTRY_NAME_RE.search(name):
            score += 50
        if name in {"Call", "Start", "Entry", "Main"}:
            score += 30
        if "::Call" in qn or qn.endswith("::Call"):
            score += 40
        if "static" in (n.get("signature") or "").lower():
            score += 15
        score += min(out_await.get(n["id"], 0) * 8, 40)
        score += min(out_count.get(n["id"], 0) * 2, 20)
        # few internal callers + has outgoing = likely entry
        if internal_in.get(n["id"], 0) == 0 and out_count.get(n["id"], 0) > 0:
            score += 35
        if external_in.get(n["id"], 0) > 0:
            score += 25
        if score <= 0:
            continue
        item = dict(n)
        item["entry_score"] = score
        scored.append((score, item))

    scored.sort(key=lambda x: (-x[0], x[1]["qualified_name"]))
    return [x[1] for x in scored[:limit]]


def trunk_from_seeds(
    conn: sqlite3.Connection,
    seed_ids: list[str],
    *,
    depth: int = 1,
    edge_kinds: list[str] | None = None,
    limit: int = 80,
) -> dict[str, Any]:
    """Outgoing-only BFS trunk from seed entry points."""
    edge_kinds = edge_kinds or ["await"]
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    seen_e: set[tuple[str, str, str]] = set()
    visited: set[str] = set()
    frontier: set[str] = set()

    def load_node(nid: str) -> dict[str, Any] | None:
        r = conn.execute(
            "SELECT id, name, qualified_name, kind, file_path, start_line, "
            "domain, namespace, signature FROM nodes WHERE id=?",
            (nid,),
        ).fetchone()
        return row_to_dict(r) if r else None

    for sid in seed_ids:
        n = load_node(sid)
        if not n or n["kind"] in {"file", "unresolved"}:
            continue
        nodes[sid] = n
        frontier.add(sid)
        visited.add(sid)

    for _ in range(max(1, depth)):
        nxt: set[str] = set()
        for nid in frontier:
            for nb in neighbors(
                conn,
                nid,
                direction="callees",
                edge_kinds=edge_kinds,
                limit=limit,
                hide_unresolved=True,
            ):
                oid = nb["id"]
                if nb.get("kind") in {"file", "unresolved"}:
                    continue
                nodes[oid] = {
                    "id": nb["id"],
                    "name": nb["name"],
                    "qualified_name": nb["qualified_name"],
                    "kind": nb["kind"],
                    "file_path": nb["file_path"],
                    "start_line": nb["start_line"],
                    "domain": nb["domain"],
                    "namespace": nb.get("namespace", ""),
                }
                key = (nid, oid, nb["edge_kind"])
                if key not in seen_e:
                    seen_e.add(key)
                    edges.append(
                        {
                            "source": nid,
                            "target": oid,
                            "kind": nb["edge_kind"],
                            "file_path": nb["edge_file"],
                            "line": nb["edge_line"],
                        }
                    )
                if oid not in visited:
                    visited.add(oid)
                    nxt.add(oid)
        frontier = nxt

    return {
        "seeds": [s for s in seed_ids if s in nodes],
        "depth": depth,
        "nodes": list(nodes.values()),
        "edges": edges,
    }
