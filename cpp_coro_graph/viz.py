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

# Four edge kinds only
EDGE_COLOR = {
    "calls": "#38bdf8",
    "await": "#ef4444",
    "contains": "#64748b",
    "inherits": "#22c55e",
}


def build_viz_payload(conn) -> dict:
    nodes = []
    for r in conn.execute("SELECT * FROM nodes").fetchall():
        qname = r["qualified_name"] or r["name"]
        short = r["name"] or qname.split("::")[-1]
        if r["kind"] == "file" and qname.startswith("file:"):
            short = qname.split("/")[-1].split("\\")[-1]
        nodes.append(
            {
                "id": r["id"],
                "label": short,
                "qname": qname,
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
        kind = r["kind"]
        # map legacy kinds if old DB
        if kind in ("seq",):
            kind = "await"
        elif kind in ("spawns", "handoff", "device_call"):
            kind = "calls"
        edges.append(
            {
                "from": r["source"],
                "to": r["target"],
                "label": kind,
                "kind": kind,
                "file": r["file_path"],
                "line": r["line"],
                "color": EDGE_COLOR.get(kind, "#94a3b8"),
                "dashes": kind in ("await", "inherits"),
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
    <option value="control">calls + await</option>
    <option value="">all edges</option>
    <option value="calls">calls (solid)</option>
    <option value="await">await (dashed)</option>
    <option value="contains">contains (file)</option>
    <option value="inherits">inherits</option>
  </select>
  <label class="chip"><input type="checkbox" id="hideUnresolved"/> hide unresolved</label>
  <label class="chip"><input type="checkbox" id="onlyLinked" checked/> only linked nodes</label>
  <span class="chip"><span class="dot" style="background:#38bdf8"></span>calls</span>
  <span class="chip"><span class="dot" style="background:#ef4444"></span>await</span>
  <span class="chip"><span class="dot" style="background:#64748b"></span>contains</span>
  <span class="chip"><span class="dot" style="background:#22c55e"></span>inherits</span>
  <span id="meta"></span>
</div>
<div id="net"></div>
<script>
const DATA = __DATA__;
const meta = document.getElementById('meta');
meta.textContent = `files ${DATA.stats.files} · nodes ${DATA.stats.nodes} · edges ${DATA.stats.edges} · kinds ${JSON.stringify(DATA.stats.edge_kinds||{})}`;

function edgeMatch(ekind, kind) {
  if (!ekind) return true;
  if (ekind === 'control') return kind === 'calls' || kind === 'await';
  return kind === ekind;
}

function linkedIds(ekind) {
  const ids = new Set();
  for (const e of DATA.edges) {
    if (!edgeMatch(ekind, e.kind)) continue;
    ids.add(e.from); ids.add(e.to);
  }
  return ids;
}

function visibleNodes() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const domain = document.getElementById('domain').value;
  const hideU = document.getElementById('hideUnresolved').checked;
  const onlyLinked = document.getElementById('onlyLinked').checked;
  const ekind = document.getElementById('ekind').value;
  const linked = onlyLinked ? linkedIds(ekind) : null;
  return DATA.nodes.filter(n => {
    if (hideU && (n.kind === 'unresolved' || String(n.id).startsWith('unresolved:'))) return false;
    // calls+await view: never show file nodes (those are contains-only)
    if ((ekind === 'control' || ekind === 'calls' || ekind === 'await') && (n.kind === 'file' || String(n.qname||'').startsWith('file:'))) return false;
    if (linked && !linked.has(n.id)) return false;
    if (domain && n.domain !== domain) return false;
    if (q && !(`${n.label} ${n.qname||''} ${n.file}`.toLowerCase().includes(q))) return false;
    return true;
  });
}

function render() {
  const nodes = visibleNodes();
  const ids = new Set(nodes.map(n => n.id));
  const ekind = document.getElementById('ekind').value;
  const edges = DATA.edges.filter(e => ids.has(e.from) && ids.has(e.to) && edgeMatch(ekind, e.kind));
  const nset = new vis.DataSet(nodes.map(n => ({
    id: n.id,
    label: n.label,
    shape: n.kind === 'class' ? 'box' : (n.kind === 'file' ? 'database' : 'ellipse'),
    color: { background: n.color, border: '#0f172a', highlight: { background: n.color, border: '#fff' } },
    font: { color: '#f8fafc', size: 13 },
    title: `${n.qname || n.label}\\n${n.kind} · ${n.domain}/${n.backend||'-'}\\n${n.file}:${n.line}`
  })));
  const eset = new vis.DataSet(edges.map((e,i) => ({
    id: i, from: e.from, to: e.to, arrows: 'to',
    color: { color: e.color },
    dashes: !!e.dashes,
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
['q','domain','ekind','hideUnresolved','onlyLinked'].forEach(id => document.getElementById(id).addEventListener('input', render));
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
