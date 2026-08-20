"""Read-only query helpers over the graph DB (callers / callees / explore)."""

from __future__ import annotations

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
            "s.domain, s.namespace "
            "FROM edges e JOIN nodes s ON s.id = e.source "
            "WHERE e.target = ?"
        )
    else:
        sql = (
            "SELECT e.kind AS edge_kind, e.line AS edge_line, e.file_path AS edge_file, "
            "t.id, t.name, t.qualified_name, t.kind, t.file_path, t.start_line, "
            "t.domain, t.namespace "
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
