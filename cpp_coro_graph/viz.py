"""Interactive HTML viz: module entries + await trunk + click-to-expand."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import query as Q
from . import store

DOMAIN_COLOR = {
    "cpu": "#3b82f6",
    "gpu": "#22c55e",
    "npu": "#f59e0b",
    "dsp": "#a855f7",
    "unknown": "#64748b",
}

EDGE_COLOR = {
    "calls": "#38bdf8",
    "await": "#ef4444",
    "contains": "#64748b",
    "inherits": "#22c55e",
}


def _node_payload(n: dict[str, Any], *, is_entry: bool = False) -> dict[str, Any]:
    qname = n.get("qualified_name") or n.get("name") or ""
    short = n.get("name") or qname.split("::")[-1]
    return {
        "id": n["id"],
        "label": short,
        "qname": qname,
        "name": n.get("name") or short,
        "kind": n.get("kind") or "",
        "domain": n.get("domain") or "unknown",
        "file": n.get("file_path") or "",
        "line": n.get("start_line") or 0,
        "signature": n.get("signature") or "",
        "color": DOMAIN_COLOR.get(n.get("domain") or "unknown", DOMAIN_COLOR["unknown"]),
        "entry": bool(is_entry),
        "score": n.get("entry_score") or 0,
    }


def _edge_payload(e: dict[str, Any]) -> dict[str, Any]:
    kind = e.get("kind") or "calls"
    if kind in ("seq",):
        kind = "await"
    elif kind in ("spawns", "handoff", "device_call"):
        kind = "calls"
    return {
        "from": e.get("source") or e.get("from"),
        "to": e.get("target") or e.get("to"),
        "kind": kind,
        "label": kind,
        "file": e.get("file_path") or e.get("file") or "",
        "line": e.get("line") or 0,
        "color": EDGE_COLOR.get(kind, "#94a3b8"),
        "dashes": kind in ("await", "inherits"),
    }


def build_viz_payload(
    conn,
    *,
    module: str = "",
    around: str = "",
    depth: int = 1,
    full: bool = False,
    entry_limit: int = 8,
) -> dict[str, Any]:
    """Build ego/module payload (default) or legacy full graph."""
    if full:
        return _build_full_payload(conn)

    modules = Q.list_modules(conn)
    symbols = []
    for r in conn.execute(
        "SELECT id, name, qualified_name, kind, file_path, start_line, domain "
        "FROM nodes WHERE kind IN ('function','coroutine','declaration') "
        "ORDER BY qualified_name LIMIT 8000"
    ):
        symbols.append(
            {
                "id": r["id"],
                "name": r["name"],
                "qname": r["qualified_name"],
                "kind": r["kind"],
                "file": r["file_path"],
                "line": r["start_line"],
                "domain": r["domain"],
            }
        )

    adjacency = Q.control_adjacency(conn)
    # compact node lookup for expand
    by_id = {s["id"]: s for s in symbols}

    seed_ids: list[str] = []
    entries: list[dict[str, Any]] = []
    active_module = module.replace("\\", "/").rstrip("/")

    if around:
        matches = Q.find_nodes(conn, around, limit=20)
        primary = Q.pick_primary(matches, around)
        if primary:
            seed_ids = [primary["id"]]
            entries = [_node_payload(primary, is_entry=True)]
            if not active_module and primary.get("file_path"):
                parts = primary["file_path"].replace("\\", "/").split("/")
                if len(parts) >= 2:
                    active_module = "/".join(parts[:-1])
    elif active_module:
        entries_raw = Q.module_entries(conn, active_module, limit=entry_limit)
        entries = [_node_payload(e, is_entry=True) for e in entries_raw]
        seed_ids = [e["id"] for e in entries[:3]]  # top trunks
    elif modules:
        # default: smallest / first module with entries
        for m in modules:
            entries_raw = Q.module_entries(conn, m, limit=entry_limit)
            if entries_raw:
                active_module = m
                entries = [_node_payload(e, is_entry=True) for e in entries_raw]
                seed_ids = [e["id"] for e in entries[:3]]
                break

    trunk = Q.trunk_from_seeds(
        conn, seed_ids, depth=max(1, depth), edge_kinds=["await"]
    )
    entry_ids = {e["id"] for e in entries}
    nodes = [
        _node_payload(n, is_entry=n["id"] in entry_ids) for n in trunk["nodes"]
    ]
    # ensure entry nodes present even with no await edges
    have = {n["id"] for n in nodes}
    for e in entries:
        if e["id"] not in have:
            nodes.append(e)
            have.add(e["id"])

    edges = [_edge_payload(e) for e in trunk["edges"]]

    # entries_by_module for UI switching without re-query server
    entries_by_module: dict[str, list[str]] = {}
    for m in modules[:80]:
        ents = Q.module_entries(conn, m, limit=entry_limit)
        if ents:
            entries_by_module[m] = [x["id"] for x in ents]

    return {
        "mode": "ego",
        "module": active_module,
        "modules": modules,
        "entries": entries,
        "entries_by_module": entries_by_module,
        "seeds": seed_ids,
        "depth": depth,
        "nodes": nodes,
        "edges": edges,
        "symbols": symbols,
        "adjacency": adjacency,
        "by_id": by_id,
        "stats": store.stats(conn),
    }


def _build_full_payload(conn) -> dict[str, Any]:
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
                "file": r["file_path"],
                "line": r["start_line"],
                "color": DOMAIN_COLOR.get(r["domain"], DOMAIN_COLOR["unknown"]),
                "entry": False,
            }
        )
    edges = []
    for r in conn.execute("SELECT * FROM edges").fetchall():
        edges.append(
            _edge_payload(
                {
                    "source": r["source"],
                    "target": r["target"],
                    "kind": r["kind"],
                    "file_path": r["file_path"],
                    "line": r["line"],
                }
            )
        )
    return {
        "mode": "full",
        "module": "",
        "modules": [],
        "entries": [],
        "entries_by_module": {},
        "seeds": [],
        "depth": 0,
        "nodes": nodes,
        "edges": edges,
        "symbols": [],
        "adjacency": {},
        "by_id": {},
        "stats": store.stats(conn),
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>cpp-coro-graph</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  body { margin:0; font-family: ui-sans-serif, system-ui, sans-serif; background:#0f172a; color:#e2e8f0; }
  #bar { display:flex; gap:10px; align-items:center; padding:10px 12px; background:#111827; border-bottom:1px solid #1f2937; flex-wrap:wrap; }
  #bar input, #bar select, #bar button { background:#1f2937; color:#e2e8f0; border:1px solid #374151; border-radius:6px; padding:6px 8px; }
  #bar button { cursor:pointer; }
  #bar button:hover { border-color:#64748b; }
  #net { height: calc(100vh - 110px); }
  #side { display:flex; gap:8px; padding:8px 12px; background:#0b1220; border-bottom:1px solid #1f2937; flex-wrap:wrap; align-items:center; font-size:13px; }
  .chip { display:inline-flex; align-items:center; gap:6px; opacity:.9; }
  .entry-btn { font-size:12px; padding:4px 8px; border-radius:999px; border:1px solid #334155; background:#1e293b; color:#e2e8f0; cursor:pointer; }
  .entry-btn.active { border-color:#ef4444; background:#7f1d1d; }
  #hint { font-size:12px; opacity:.7; margin-left:auto; }
  #meta { font-size:12px; opacity:.65; }
</style>
</head>
<body>
<div id="bar">
  <strong>cpp-coro-graph</strong>
  <label>module
    <select id="module"></select>
  </label>
  <input id="q" placeholder="search symbol…" size="22"/>
  <button id="goSearch" type="button">Go</button>
  <label class="chip"><input type="checkbox" id="showCalls"/> calls</label>
  <button id="resetTrunk" type="button">Reset trunk</button>
  <span id="meta"></span>
</div>
<div id="side">
  <span>entries:</span>
  <span id="entryList"></span>
  <span id="hint">双击节点展开下一层 await · 默认只看主干</span>
</div>
<div id="net"></div>
<script>
const DATA = __DATA__;
const meta = document.getElementById('meta');
meta.textContent = `mode=${DATA.mode} · nodes ${DATA.stats.nodes} · edges ${DATA.stats.edges}`;

let activeModule = DATA.module || '';
let seedIds = new Set(DATA.seeds || []);
let visibleNodeIds = new Set((DATA.nodes || []).map(n => n.id));
let visibleEdges = (DATA.edges || []).map(e => ({...e}));
let nodeCache = {};
(DATA.nodes || []).forEach(n => { nodeCache[n.id] = n; });
Object.values(DATA.by_id || {}).forEach(s => {
  if (!nodeCache[s.id]) {
    nodeCache[s.id] = {
      id: s.id, label: s.name, qname: s.qname, name: s.name, kind: s.kind,
      domain: s.domain || 'unknown', file: s.file, line: s.line,
      color: ({cpu:'#3b82f6',gpu:'#22c55e',npu:'#f59e0b',dsp:'#a855f7'}[s.domain] || '#64748b'),
      entry: false
    };
  }
});
(DATA.entries || []).forEach(e => { nodeCache[e.id] = e; });

function fillModules() {
  const sel = document.getElementById('module');
  sel.innerHTML = '';
  const opts = DATA.modules || [];
  if (!opts.length) {
    const o = document.createElement('option');
    o.value = ''; o.textContent = '(no modules)';
    sel.appendChild(o);
    return;
  }
  opts.forEach(m => {
    const o = document.createElement('option');
    o.value = m; o.textContent = m;
    if (m === activeModule) o.selected = true;
    sel.appendChild(o);
  });
}

function entryIdsForModule(mod) {
  return new Set((DATA.entries_by_module || {})[mod] || []);
}

function renderEntryButtons() {
  const box = document.getElementById('entryList');
  box.innerHTML = '';
  const ids = entryIdsForModule(activeModule);
  const list = [...ids].map(id => nodeCache[id]).filter(Boolean);
  // prefer DATA.entries order if same module
  const ordered = (DATA.entries || []).filter(e => ids.has(e.id));
  const show = ordered.length ? ordered : list;
  show.forEach(e => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'entry-btn' + (seedIds.has(e.id) ? ' active' : '');
    b.textContent = e.label || e.name;
    b.title = e.qname || '';
    b.onclick = () => {
      seedIds = new Set([e.id]);
      resetTrunkFromSeeds();
      renderEntryButtons();
    };
    box.appendChild(b);
  });
  if (!show.length) box.textContent = '(no entries scored)';
}

function edgeAllowed(kind) {
  if (kind === 'await') return true;
  if (kind === 'calls') return document.getElementById('showCalls').checked;
  return false;
}

function resetTrunkFromSeeds() {
  visibleNodeIds = new Set(seedIds);
  visibleEdges = [];
  const depth = Math.max(1, DATA.depth || 1);
  let frontier = new Set(seedIds);
  for (let d = 0; d < depth; d++) {
    const nxt = new Set();
    frontier.forEach(id => {
      (DATA.adjacency[id] || []).forEach(e => {
        if (!edgeAllowed(e.kind)) return;
        visibleNodeIds.add(e.to);
        visibleEdges.push({
          from: id, to: e.to, kind: e.kind, label: e.kind, line: e.line,
          color: e.kind === 'await' ? '#ef4444' : '#38bdf8',
          dashes: e.kind === 'await'
        });
        nxt.add(e.to);
      });
    });
    frontier = nxt;
  }
  // dedupe edges
  const seen = new Set();
  visibleEdges = visibleEdges.filter(e => {
    const k = e.from + '|' + e.to + '|' + e.kind;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  render();
}

function expandNode(id) {
  const outs = DATA.adjacency[id] || [];
  let added = 0;
  outs.forEach(e => {
    if (!edgeAllowed(e.kind)) return;
    visibleNodeIds.add(e.to);
    const k = id + '|' + e.to + '|' + e.kind;
    if (!visibleEdges.some(x => x.from===id && x.to===e.to && x.kind===e.kind)) {
      visibleEdges.push({
        from: id, to: e.to, kind: e.kind, label: e.kind, line: e.line,
        color: e.kind === 'await' ? '#ef4444' : '#38bdf8',
        dashes: e.kind === 'await'
      });
      added++;
    }
  });
  if (added) render();
}

function selectModule(mod) {
  activeModule = mod;
  const ids = [...entryIdsForModule(mod)];
  seedIds = new Set(ids.slice(0, 3));
  // refresh entries display from by_id
  DATA.entries = ids.map(id => {
    const s = nodeCache[id] || DATA.by_id[id];
    if (!s) return null;
    return {
      id: s.id, label: s.label || s.name, qname: s.qname || s.qualified_name,
      name: s.name, kind: s.kind, domain: s.domain || 'unknown',
      file: s.file || s.file_path, line: s.line || s.start_line || 0,
      color: s.color || '#3b82f6', entry: true, score: s.score || 0
    };
  }).filter(Boolean);
  DATA.entries.forEach(e => { nodeCache[e.id] = e; });
  renderEntryButtons();
  resetTrunkFromSeeds();
}

function searchGo() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  if (!q) return;
  const hits = (DATA.symbols || []).filter(s =>
    (s.name || '').toLowerCase().includes(q) ||
    (s.qname || '').toLowerCase().includes(q)
  );
  if (!hits.length) { alert('no symbol match'); return; }
  // prefer exact name
  let hit = hits.find(s => s.name.toLowerCase() === q || s.qname.toLowerCase() === q) || hits[0];
  seedIds = new Set([hit.id]);
  if (hit.file) {
    const parts = hit.file.replace(/\\/g,'/').split('/');
    if (parts.length >= 2) {
      activeModule = parts.slice(0, -1).join('/');
      document.getElementById('module').value = activeModule;
    }
  }
  renderEntryButtons();
  resetTrunkFromSeeds();
}

let network = null;
function render() {
  const nodes = [...visibleNodeIds].map(id => {
    const n = nodeCache[id];
    if (!n) return null;
    const isEntry = seedIds.has(id) || n.entry;
    return {
      id: n.id,
      label: n.label || n.name,
      size: isEntry ? 36 : 28,
      font: { color: '#f8fafc', size: isEntry ? 16 : 14, face: 'Segoe UI, sans-serif' },
      color: {
        background: n.color || '#3b82f6',
        border: isEntry ? '#fef08a' : '#0f172a',
        highlight: { background: n.color || '#3b82f6', border: '#fff' }
      },
      borderWidth: isEntry ? 3 : 1,
      shape: 'ellipse',
      title: `${n.qname || n.label}\n${n.kind || ''} · ${n.file || ''}:${n.line || ''}\n${n.signature || ''}`
    };
  }).filter(Boolean);

  const edges = visibleEdges.filter(e => visibleNodeIds.has(e.from) && visibleNodeIds.has(e.to)).map((e,i) => ({
    id: i, from: e.from, to: e.to, arrows: 'to',
    color: { color: e.color },
    dashes: !!e.dashes,
    width: e.kind === 'await' ? 2.5 : 1.5,
    label: e.kind === 'await' ? '' : e.kind,
    font: { color: '#94a3b8', size: 10, strokeWidth: 0 },
    title: `${e.kind} @ ${e.file || ''}:${e.line || ''}`
  }));

  const nset = new vis.DataSet(nodes);
  const eset = new vis.DataSet(edges);
  const opts = {
    layout: {
      hierarchical: {
        enabled: true,
        direction: 'LR',
        sortMethod: 'directed',
        levelSeparation: 180,
        nodeSpacing: 90,
        treeSpacing: 120
      }
    },
    physics: { enabled: false },
    interaction: { hover: true, tooltipDelay: 60, multiselect: false },
    edges: { smooth: { type: 'cubicBezier', forceDirection: 'horizontal', roundness: 0.4 } }
  };
  const el = document.getElementById('net');
  if (network) network.destroy();
  network = new vis.Network(el, {nodes: nset, edges: eset}, opts);
  network.on('doubleClick', params => {
    if (params.nodes && params.nodes[0]) expandNode(params.nodes[0]);
  });
}

fillModules();
renderEntryButtons();
if (DATA.mode === 'full') {
  // legacy full dump
  visibleNodeIds = new Set((DATA.nodes||[]).map(n => n.id));
  visibleEdges = (DATA.edges||[]).map(e => ({...e}));
  (DATA.nodes||[]).forEach(n => nodeCache[n.id] = n);
  document.getElementById('hint').textContent = 'FULL GRAPH mode — may be unreadable';
  render();
} else {
  resetTrunkFromSeeds();
}

document.getElementById('module').addEventListener('change', e => selectModule(e.target.value));
document.getElementById('goSearch').addEventListener('click', searchGo);
document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') searchGo(); });
document.getElementById('resetTrunk').addEventListener('click', () => resetTrunkFromSeeds());
document.getElementById('showCalls').addEventListener('change', () => resetTrunkFromSeeds());
</script>
</body>
</html>
"""


def write_html(
    db_path: Path,
    out_path: Path,
    *,
    module: str = "",
    around: str = "",
    depth: int = 1,
    full: bool = False,
) -> Path:
    conn = store.connect(db_path)
    if full:
        print(
            "[cpp-coro-graph] WARNING: --full embeds the entire graph; prefer --module",
            file=sys.stderr,
            flush=True,
        )
    payload = build_viz_payload(
        conn, module=module, around=around, depth=depth, full=full
    )
    conn.close()
    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
