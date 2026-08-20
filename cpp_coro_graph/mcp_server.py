"""Minimal MCP stdio server (JSON-RPC 2.0 subset) — no third-party deps."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import store


def _read_message() -> dict[str, Any] | None:
    """Read one LSP-style Content-Length framed message, or one JSON line."""
    # Prefer Content-Length framing (MCP). Fall back to NDJSON for simple tests.
    header = b""
    while True:
        ch = sys.stdin.buffer.read(1)
        if not ch:
            return None
        header += ch
        if header.endswith(b"\r\n\r\n"):
            break
        # NDJSON fallback: if we get a bare '{' start without headers
        if header.startswith(b"{") and b"\n" in header and b"Content-Length" not in header:
            line = header.split(b"\n", 1)[0]
            return json.loads(line.decode("utf-8"))
    length = 0
    for line in header.decode("utf-8", errors="replace").split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def _write_message(msg: dict[str, Any]) -> None:
    data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(
        f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data
    )
    sys.stdout.buffer.flush()


def _tool_list() -> list[dict[str, Any]]:
    return [
        {
            "name": "coro_explore",
            "description": (
                "Explore the C++ coroutine/device syntax graph. "
                "Pass a symbol name, file path fragment, or domain (cpu/gpu/npu)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "domain": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["query"],
            },
        },
        {
            "name": "coro_stats",
            "description": "Graph statistics (node/edge counts by domain and kind).",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _explore(conn, query: str, domain: str | None, limit: int) -> str:
    q = f"%{query}%"
    sql = (
        "SELECT * FROM nodes WHERE "
        "(name LIKE ? OR qualified_name LIKE ? OR file_path LIKE ?)"
    )
    args: list[Any] = [q, q, q]
    if domain:
        sql += " AND domain=?"
        args.append(domain)
    sql += " LIMIT ?"
    args.append(limit)
    nodes = [dict(r) for r in conn.execute(sql, args).fetchall()]
    if not nodes and query in {"cpu", "gpu", "npu", "dsp", "unknown"}:
        nodes = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM nodes WHERE domain=? LIMIT ?", (query, limit)
            )
        ]
    lines = [f"# explore `{query}`", f"matched_nodes: {len(nodes)}", ""]
    for n in nodes:
        lines.append(
            f"## {n['qualified_name']}  ({n['kind']}, {n['domain']}/{n['backend'] or '-'})"
        )
        lines.append(f"- file: `{n['file_path']}:{n['start_line']}`")
        outs = conn.execute(
            "SELECT * FROM edges WHERE source=? LIMIT 30", (n["id"],)
        ).fetchall()
        inns = conn.execute(
            "SELECT * FROM edges WHERE target=? LIMIT 30", (n["id"],)
        ).fetchall()
        if outs:
            lines.append("- outgoing:")
            for e in outs:
                tgt = conn.execute(
                    "SELECT qualified_name, domain FROM nodes WHERE id=?",
                    (e["target"],),
                ).fetchone()
                tname = tgt["qualified_name"] if tgt else e["target"]
                tdom = tgt["domain"] if tgt else "?"
                lines.append(
                    f"  - [{e['kind']}] → {tname} ({tdom}) @ {e['file_path']}:{e['line']}"
                )
        if inns:
            lines.append("- incoming:")
            for e in inns:
                src = conn.execute(
                    "SELECT qualified_name, domain FROM nodes WHERE id=?",
                    (e["source"],),
                ).fetchone()
                sname = src["qualified_name"] if src else e["source"]
                lines.append(
                    f"  - [{e['kind']}] ← {sname} @ {e['file_path']}:{e['line']}"
                )
        lines.append("")
    return "\n".join(lines)


def serve(db_path: Path) -> None:
    conn = store.connect(db_path)
    while True:
        msg = _read_message()
        if msg is None:
            break
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "cpp-coro-graph", "version": "0.1.0"},
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _write_message(
                {"jsonrpc": "2.0", "id": mid, "result": {"tools": _tool_list()}}
            )
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                if name == "coro_stats":
                    text = json.dumps(store.stats(conn), indent=2, ensure_ascii=False)
                elif name == "coro_explore":
                    text = _explore(
                        conn,
                        str(args.get("query") or ""),
                        args.get("domain"),
                        int(args.get("limit") or 20),
                    )
                else:
                    text = f"unknown tool: {name}"
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "result": {
                            "content": [{"type": "text", "text": text}],
                            "isError": False,
                        },
                    }
                )
            except Exception as exc:  # noqa: BLE001
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "result": {
                            "content": [{"type": "text", "text": str(exc)}],
                            "isError": True,
                        },
                    }
                )
        elif method == "ping":
            _write_message({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )
    conn.close()
