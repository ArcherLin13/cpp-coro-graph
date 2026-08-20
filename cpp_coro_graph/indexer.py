"""Walk a repo and build the syntax graph DB."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .extract import (
    CPP_EXTS,
    FileExtract,
    load_coro_types,
    load_device_rules,
    load_thread_rules,
    extract_file,
)
from .resolve import SymbolIndex, SymbolRef
from . import store

SKIP_DIRS = {
    ".git",
    ".svn",
    ".hg",
    ".codegraph",
    ".cpp-coro-graph",
    "node_modules",
    "build",
    "out",
    "output",
    "dist",
    "target",
    ".venv",
    "venv",
    "__pycache__",
    "third_party",
    "thirdparty",
    "ThirdParty",
    "external",
    "prebuilts",
    "out_dir",
    ".idea",
    ".vs",
    ".repo",
    "CMakeFiles",
}


def log(msg: str) -> None:
    print(f"[cpp-coro-graph] {msg}", file=sys.stderr, flush=True)


def should_skip_dir(name: str) -> bool:
    if name in SKIP_DIRS or name.startswith("."):
        return True
    if name.startswith("bazel-") or name.startswith("cmake-build"):
        return True
    return False


def iter_cpp_files(root: Path) -> list[Path]:
    files: list[Path] = []
    scanned_dirs = 0
    log(f"scanning under {root} ...")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
        scanned_dirs += 1
        if scanned_dirs == 1 or scanned_dirs % 200 == 0:
            log(
                f"  walking... dirs={scanned_dirs} cpp_files={len(files)} "
                f"current={dirpath}"
            )
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in CPP_EXTS:
                files.append(p)
    log(f"scan done: {len(files)} C/C++ files in {scanned_dirs} dirs")
    return sorted(files)


def index_repo(
    root: Path,
    db_path: Path,
    rules_path: Path | None = None,
    max_files: int = 0,
) -> dict:
    t0 = time.time()
    root = root.resolve()
    log(f"index start root={root}")
    log(f"db={db_path}")
    rules = load_device_rules(rules_path)
    coro_types = load_coro_types()
    thread_rules = load_thread_rules()
    log(
        f"loaded rules: devices={len(rules)} coro_types={len(coro_types)} "
        f"thread_apis={len(thread_rules)}"
    )
    files = iter_cpp_files(root)
    if max_files > 0:
        files = files[:max_files]
        log(f"--max-files={max_files}, using {len(files)} files")

    if not files:
        log("WARNING: no .cpp/.h/.cc/... files found (check path / skip rules)")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = store.connect(db_path)
    store.clear_graph(conn)
    store.upsert_meta(conn, "root", str(root))
    store.upsert_meta(conn, "mode", "syntax-v1.4-decls-class")
    store.upsert_meta(conn, "version", "0.3.2")

    extracts: list[FileExtract] = []
    total = len(files)
    log(f"parsing {total} files ...")
    skipped = 0
    for i, fp in enumerate(files, 1):
        rel = str(fp.relative_to(root)).replace("\\", "/")
        try:
            sz = fp.stat().st_size
        except OSError:
            sz = -1
        log(f"  parse start [{i}/{total}] {rel} ({sz} bytes)")
        t1 = time.time()
        try:
            ex = extract_file(
                fp, rel, rules, coro_types=coro_types, thread_rules=thread_rules
            )
        except Exception as exc:  # noqa: BLE001
            log(f"  SKIP {rel}: {exc}")
            skipped += 1
            continue
        dt = time.time() - t1
        if ex.skipped:
            skipped += 1
            log(f"  parse done  [{i}/{total}] skipped={ex.skipped} ({dt:.2f}s)")
        elif dt >= 0.5 or i == 1 or i % 25 == 0 or i == total:
            log(
                f"  parse done  [{i}/{total}] nodes={len(ex.nodes)} "
                f"edges={len(ex.edges)} ({dt:.2f}s)"
            )
        extracts.append(ex)
        conn.execute(
            "INSERT OR REPLACE INTO files(path, size, indexed_at) VALUES(?,?,?)",
            (rel, sz if sz >= 0 else 0, int(time.time())),
        )
    log(f"parse pass finished (skipped_or_light={skipped})")

    log("building symbol index (cross-file / cross-namespace) ...")
    index = SymbolIndex()
    node_count = 0
    # Map extract-local qname -> node id (per file uniqueness via store.node_id)
    local_to_id: dict[tuple[str, str], str] = {}

    for ex in extracts:
        for n in ex.nodes:
            nid = store.node_id(n.qualified_name, n.file_path, n.start_line)
            local_to_id[(ex.path, n.qualified_name)] = nid
            index.add(
                SymbolRef(
                    node_id=nid,
                    name=n.name,
                    qualified_name=n.qualified_name,
                    file_path=n.file_path,
                    namespace=n.namespace,
                    kind=n.kind,
                )
            )
            conn.execute(
                "INSERT OR REPLACE INTO nodes"
                "(id, name, qualified_name, kind, file_path, start_line, end_line, "
                "domain, backend, signature, namespace) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    nid,
                    n.name,
                    n.qualified_name,
                    n.kind,
                    n.file_path,
                    n.start_line,
                    n.end_line,
                    n.domain,
                    n.backend,
                    n.signature,
                    n.namespace,
                ),
            )
            node_count += 1
    log(f"nodes inserted: {node_count}")

    log("building edges with cross-file resolution ...")
    stub_ids: set[str] = set()
    edge_count = 0
    resolved_cross = 0
    unresolved = 0
    seen_edge_keys: set[tuple[str, str, str]] = set()

    for ex in extracts:
        for e in ex.edges:
            src = local_to_id.get((ex.path, e.source_qname))
            if not src:
                continue
            tgt = index.resolve(
                e.target_name,
                from_file=e.file_path,
                from_namespace=e.source_namespace,
                usings=ex.usings,
            )
            if tgt is not None:
                # count as cross-file if target file differs
                row = conn.execute(
                    "SELECT file_path FROM nodes WHERE id=?", (tgt,)
                ).fetchone()
                if row and row["file_path"] != e.file_path:
                    resolved_cross += 1
            else:
                unresolved += 1
                t = e.target_name.split("<", 1)[0]
                simple = t.split("->")[-1].split(".")[-1].split("::")[-1]
                stub_id = f"unresolved::{t}"
                tgt = stub_id
                if stub_id not in stub_ids:
                    stub_ids.add(stub_id)
                    domain = e.domain if e.domain != "unknown" else "unknown"
                    backend = e.backend
                    kind = "device_api" if e.kind == "device_call" else "unresolved"
                    if e.kind == "device_call":
                        domain = e.domain
                    conn.execute(
                        "INSERT OR REPLACE INTO nodes"
                        "(id, name, qualified_name, kind, file_path, start_line, "
                        "end_line, domain, backend, signature, namespace) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            stub_id,
                            simple or t,
                            t,
                            kind,
                            e.file_path,
                            e.line,
                            e.line,
                            domain,
                            backend,
                            "",
                            "",
                        ),
                    )

            key = (src, tgt, e.kind)
            if key in seen_edge_keys:
                continue
            seen_edge_keys.add(key)

            conn.execute(
                "INSERT INTO edges"
                "(source, target, kind, file_path, line, domain, backend, metadata) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    src,
                    tgt,
                    e.kind,
                    e.file_path,
                    e.line,
                    e.domain,
                    e.backend,
                    json.dumps(
                        {
                            "raw": e.raw_target,
                            "from_ns": e.source_namespace,
                            "usings": ex.usings[:8],
                        }
                    ),
                ),
            )
            edge_count += 1

    log(
        f"edges inserted: {edge_count} "
        f"(cross-file resolved hits~{resolved_cross}, "
        f"unresolved stubs={len(stub_ids)}, unresolved edges={unresolved})"
    )

    log("promoting device domains ...")
    for row in conn.execute(
        "SELECT source, domain FROM edges WHERE kind='device_call' AND domain!='unknown'"
    ).fetchall():
        conn.execute(
            "UPDATE nodes SET domain=?, backend=COALESCE(NULLIF(backend,''), ?) "
            "WHERE id=? AND domain='cpu'",
            (row["domain"], row["domain"], row["source"]),
        )

    conn.commit()
    s = store.stats(conn)
    s["root"] = str(root)
    s["db"] = str(db_path)
    s["cross_file_resolve_hits"] = resolved_cross
    s["unresolved_edge_attempts"] = unresolved
    conn.close()
    log(f"index done in {time.time() - t0:.1f}s -> {db_path}")
    return s
