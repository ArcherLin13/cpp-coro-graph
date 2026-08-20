"""Emit a self-contained HTML visualization of the graph."""

from __future__ import annotations

import json
from pathlib import Path

from . import store

DOMAIN_COLOR = {
    "cpu": "#3b82f6",
    "gpu": "#22c55e",
    "npu": "#f59e0b",
    "dsp": "#a855f7",
    "unknown": "#94a3b8",
}

EDGE_COLOR = {
    "await": "#ef4444",
    "device_call": "#f59e0b",
    "calls": "#64748b",
}


def build_viz_payload(conn) -> dict:
    nodes = []
    for r in conn.execute("SELECT * FROM nodes").fetchall():
        nodes.append(
            {
                "id": r["id"],
                "label": r["qualified_name"] or r["name"],
                "name": r["name"],
                "kind": r["kind"],
                "domain": r["domain"],
                "backend": r["backend"],
                "file": r["file_path"],
                "line": r["start_line"],
                "color": DOMAIN_COLOR.get(r["domain"], DOMAIN_COLOR["unknown"]),
            }
        )
    edges = []
    for r in conn.execute("SELECT * FROM edges").fetchall():
        edges.append(
            {
                "from": r["source"],
                "to": r["target"],
                "label": r["kind"],
                "kind": r["kind"],
                "file": r["file_path"],
                "line": r["line"],
                "color": EDGE_COLOR.get(r["kind"], "#94a3b8"),
            }
        )
    return {"nodes": nodes, "edges": edges, "stats": store.stats(conn)}


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>cpp-coro-graph</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  :root { color-scheme: light; }
  body { margin:0; font-family: ui-sans-serif, system-ui, sans-serif; background:#0f172a; color:#e2e8f0; }
  #bar { display:flex; gap:12px; align-items:center; padding:10px 14px; background:#111827; border-bottom:1px solid #1f2937; flex-wrap:wrap; }
  #bar input, #bar select { background:#1f2937; color:#e2e8f0; border:1px solid #374151; border-radius:6px; padding:6px 8px; }
  #net { height: calc(100vh - 52px); }
  .chip { display:inline-flex; align-items:center; gap:6px; font-size:12px; opacity:.9; }
  .dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
  #meta { font-size:12px; opacity:.75; margin-left:auto; }
</style>
</head>
<body>
<div id="bar">
  <strong>cpp-coro-graph</strong>
  <input id="q" placeholder="filter name…" size="24"/>
  <select id="domain">
    <option value="">all domains</option>
    <option>cpu</option><option>gpu</option><option>npu</option><option>dsp</option><option>unknown</option>
  </select>
  <select id="ekind">
    <option value="">all edges</option>
    <option>await</option><option>device_call</option>
  </select>
  <label class="chip"><input type="checkbox" id="hideUnresolved" checked/> hide unresolved</label>
  <span class="chip"><span class="dot" style="background:#3b82f6"></span>cpu</span>
  <span class="chip"><span class="dot" style="background:#22c55e"></span>gpu</span>
  <span class="chip"><span class="dot" style="background:#f59e0b"></span>npu</span>
  <span class="chip"><span class="dot" style="background:#ef4444"></span>await edge</span>
  <span id="meta"></span>
</div>
<div id="net"></div>
<script>
const DATA = __DATA__;
const meta = document.getElementById('meta');
meta.textContent = `nodes ${DATA.stats.nodes} · edges ${DATA.stats.edges} · files ${DATA.stats.files}`;

function visibleNodes() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const domain = document.getElementById('domain').value;
  const hideU = document.getElementById('hideUnresolved').checked;
  return DATA.nodes.filter(n => {
    if (hideU && (n.kind === 'unresolved' || n.kind === 'device_api' && n.id.startsWith('unresolved:'))) {
      // keep device_api stubs if they have domain, but hide plain unresolved
      if (n.kind === 'unresolved') return false;
    }
    if (hideU && n.id.startsWith('unresolved:') && n.kind === 'unresolved') return false;
    if (domain && n.domain !== domain) return false;
    if (q && !(`${n.label} ${n.file}`.toLowerCase().includes(q))) return false;
    return true;
  });
}

function render() {
  const nodes = visibleNodes();
  const ids = new Set(nodes.map(n => n.id));
  const ekind = document.getElementById('ekind').value;
  const edges = DATA.edges.filter(e => ids.has(e.from) && ids.has(e.to) && (!ekind || e.kind === ekind));
  const nset = new vis.DataSet(nodes.map(n => ({
    id: n.id,
    label: n.label.length > 40 ? n.label.slice(0,37)+'…' : n.label,
    color: { background: n.color, border: '#0f172a', highlight: { background: n.color, border: '#fff' } },
    font: { color: '#f8fafc', size: 12 },
    title: `${n.label}\\n${n.kind} · ${n.domain}/${n.backend||'-'}\\n${n.file}:${n.line}`
  })));
  const eset = new vis.DataSet(edges.map((e,i) => ({
    id: i, from: e.from, to: e.to, arrows: 'to',
    color: { color: e.color },
    label: e.label,
    font: { color: '#cbd5e1', size: 10, strokeWidth: 0 },
    title: `${e.kind} @ ${e.file}:${e.line}`
  })));
  const network = new vis.Network(document.getElementById('net'), {nodes: nset, edges: eset}, {
    physics: { stabilization: { iterations: 80 }, barnesHut: { gravitationalConstant: -12000, springLength: 140 } },
    interaction: { hover: true, tooltipDelay: 80 },
    edges: { smooth: { type: 'dynamic' } }
  });
  window.__net = network;
}
['q','domain','ekind','hideUnresolved'].forEach(id => document.getElementById(id).addEventListener('input', render));
render();
</script>
</body>
</html>
"""


def write_html(db_path: Path, out_path: Path) -> Path:
    conn = store.connect(db_path)
    payload = build_viz_payload(conn)
    conn.close()
    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
