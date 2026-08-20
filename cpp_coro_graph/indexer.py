"""Walk a repo and build the syntax graph DB."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .extract import CPP_EXTS, FileExtract, load_device_rules, extract_file
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
    """Progress to stderr so JSON stats on stdout stay clean if piped."""
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
    log(f"loaded {len(rules)} device rules")
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
    store.upsert_meta(conn, "mode", "syntax-v1-no-compile-commands")
    store.upsert_meta(conn, "version", "0.1.0")

    extracts: list[FileExtract] = []
    total = len(files)
    log(f"parsing {total} files ...")
    for i, fp in enumerate(files, 1):
        rel = str(fp.relative_to(root)).replace("\\", "/")
        if i == 1 or i % 50 == 0 or i == total:
            log(f"  parse [{i}/{total}] {rel}")
        try:
            ex = extract_file(fp, rel, rules)
        except Exception as exc:  # noqa: BLE001
            log(f"  SKIP {rel}: {exc}")
            continue
        extracts.append(ex)
        conn.execute(
            "INSERT OR REPLACE INTO files(path, size, indexed_at) VALUES(?,?,?)",
            (rel, fp.stat().st_size, int(time.time())),
        )

    log("building nodes ...")
    by_qname: dict[str, str] = {}
    by_name: dict[str, list[str]] = {}
    node_count = 0
    for ex in extracts:
        for n in ex.nodes:
            nid = store.node_id(n.qualified_name, n.file_path, n.start_line)
            by_qname[n.qualified_name] = nid
            by_qname[n.name] = nid
            by_name.setdefault(n.name, []).append(nid)
            conn.execute(
                "INSERT OR REPLACE INTO nodes"
                "(id, name, qualified_name, kind, file_path, start_line, end_line, "
                "domain, backend, signature) VALUES(?,?,?,?,?,?,?,?,?,?)",
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
                ),
            )
            node_count += 1
    log(f"nodes inserted: {node_count}")

    log("building edges ...")
    stub_ids: set[str] = set()
    edge_count = 0
    for ex in extracts:
        for e in ex.edges:
            src = by_qname.get(e.source_qname)
            if not src:
                continue
            t = e.target_name.split("<", 1)[0]
            simple = t.split("->")[-1].split(".")[-1].split("::")[-1]
            tgt = None
            if t in by_qname:
                tgt = by_qname[t]
            elif simple in by_qname and len(by_name.get(simple, [])) == 1:
                tgt = by_qname[simple]
            elif simple in by_name and len(by_name[simple]) == 1:
                tgt = by_name[simple][0]
            elif simple in by_name and len(by_name[simple]) > 1:
                same = [
                    i
                    for i in by_name[simple]
                    if i.startswith(e.file_path + "::")
                ]
                tgt = same[0] if same else by_name[simple][0]
            else:
                stub_q = t
                stub_id = f"unresolved::{stub_q}"
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
                        "end_line, domain, backend, signature) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            stub_id,
                            simple or stub_q,
                            stub_q,
                            kind,
                            e.file_path,
                            e.line,
                            e.line,
                            domain,
                            backend,
                            "",
                        ),
                    )
                    by_qname[stub_q] = stub_id

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
                    json.dumps({"raw": e.raw_target}),
                ),
            )
            edge_count += 1
    log(f"edges inserted: {edge_count} (unresolved stubs: {len(stub_ids)})")

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
    conn.close()
    log(f"index done in {time.time() - t0:.1f}s -> {db_path}")
    return s
