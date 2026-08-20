"""SQLite store for the syntax graph."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  indexed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  qualified_name TEXT NOT NULL,
  kind TEXT NOT NULL,
  file_path TEXT NOT NULL,
  start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL,
  domain TEXT NOT NULL,
  backend TEXT NOT NULL,
  signature TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  target TEXT NOT NULL,
  kind TEXT NOT NULL,
  file_path TEXT NOT NULL,
  line INTEGER NOT NULL,
  domain TEXT NOT NULL DEFAULT 'unknown',
  backend TEXT NOT NULL DEFAULT '',
  metadata TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY (source) REFERENCES nodes(id),
  FOREIGN KEY (target) REFERENCES nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_qname ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_nodes_domain ON nodes(domain);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def clear_graph(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM nodes")
    conn.execute("DELETE FROM files")
    conn.commit()


def upsert_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def node_id(qname: str, file_path: str, start_line: int) -> str:
    return f"{file_path}::{qname}#{start_line}"


def export_json(conn: sqlite3.Connection) -> dict[str, Any]:
    nodes = [dict(r) for r in conn.execute("SELECT * FROM nodes").fetchall()]
    edges = [dict(r) for r in conn.execute("SELECT * FROM edges").fetchall()]
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM meta")}
    return {"meta": meta, "nodes": nodes, "edges": edges}


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def count(table: str) -> int:
        return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])

    domains = {
        r["domain"]: r["c"]
        for r in conn.execute(
            "SELECT domain, COUNT(*) AS c FROM nodes GROUP BY domain"
        )
    }
    kinds = {
        r["kind"]: r["c"]
        for r in conn.execute(
            "SELECT kind, COUNT(*) AS c FROM edges GROUP BY kind"
        )
    }
    return {
        "files": count("files"),
        "nodes": count("nodes"),
        "edges": count("edges"),
        "node_domains": domains,
        "edge_kinds": kinds,
        "generated_at": int(time.time()),
    }
