"""Walk a repo and build the syntax graph DB."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .extract import CPP_EXTS, FileExtract, load_device_rules, extract_file
from . import store

SKIP_DIRS = {
    ".git",
    ".svn",
    ".hg",
    ".codegraph",
    "node_modules",
    "build",
    "out",
    "dist",
    "target",
    ".venv",
    "venv",
    "__pycache__",
    "third_party",
    "thirdparty",
    "ThirdParty",
    "external",
    ".idea",
    ".vs",
    "CMakeFiles",
}


def should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")


def iter_cpp_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in CPP_EXTS:
            continue
        # skip if any parent is blacklisted
        skip = False
        for parent in p.relative_to(root).parents:
            if parent.parts and should_skip_dir(parent.parts[-1] if parent.parts else ""):
                skip = True
                break
        parts = p.relative_to(root).parts
        if any(should_skip_dir(part) for part in parts[:-1]):
            skip = True
        if skip:
            continue
        files.append(p)
    return sorted(files)


def resolve_target(
    target_name: str,
    by_qname: dict[str, str],
    by_name: dict[str, list[str]],
    unresolved_prefix: str,
    file_path: str,
    line: int,
    kind: str,
    domain: str,
    backend: str,
) -> str:
    """Map await/call target text to a node id, creating unresolved stubs."""
    # strip template args leftovers
    t = target_name.split("<", 1)[0]
    # member call: keep last segment for name lookup, full for qname
    simple = t.split("->")[-1].split(".")[-1]
    simple = simple.split("::")[-1]

    if t in by_qname:
        return by_qname[t]
    if simple in by_qname:
        return by_qname[simple]
    # unique short name
    ids = by_name.get(simple) or by_name.get(t) or []
    if len(ids) == 1:
        return ids[0]
    if len(ids) > 1:
        # prefer same-file later; for now first
        return ids[0]

    # stub unresolved
    stub_q = f"unresolved:{t}"
    stub_id = f"{unresolved_prefix}:{stub_q}"
    return stub_id


def index_repo(
    root: Path,
    db_path: Path,
    rules_path: Path | None = None,
    max_files: int = 0,
) -> dict:
    root = root.resolve()
    rules = load_device_rules(rules_path)
    files = iter_cpp_files(root)
    if max_files > 0:
        files = files[:max_files]

    conn = store.connect(db_path)
    store.clear_graph(conn)
    store.upsert_meta(conn, "root", str(root))
    store.upsert_meta(conn, "mode", "syntax-v1-no-compile-commands")
    store.upsert_meta(conn, "version", "0.1.0")

    extracts: list[FileExtract] = []
    for fp in files:
        rel = str(fp.relative_to(root)).replace("\\", "/")
        ex = extract_file(fp, rel, rules)
        extracts.append(ex)
        conn.execute(
            "INSERT OR REPLACE INTO files(path, size, indexed_at) VALUES(?,?,?)",
            (rel, fp.stat().st_size, int(time.time())),
        )

    # Pass 1: insert all known nodes
    by_qname: dict[str, str] = {}
    by_name: dict[str, list[str]] = {}
    for ex in extracts:
        for n in ex.nodes:
            nid = store.node_id(n.qualified_name, n.file_path, n.start_line)
            # also allow lookup by bare qname (last writer wins for duplicates)
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

    # Pass 2: edges + unresolved stubs
    stub_ids: set[str] = set()
    for ex in extracts:
        for e in ex.edges:
            src = by_qname.get(e.source_qname)
            if not src:
                # try find node with that qname from DB keys
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
                # prefer same file
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

    # Promote node domain if it has device_call edges
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
    return s
