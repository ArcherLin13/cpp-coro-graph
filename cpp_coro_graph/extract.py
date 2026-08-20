"""Syntax-level C++ extraction: functions, co_await edges, device tags."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Strip // and /* */ comments (naive; good enough for V1 probes).
_COMMENT_LINE = re.compile(r"//.*?$", re.M)
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_STRING = re.compile(
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
)

# co_await Foo( / co_await Foo::Bar( / co_await x.foo( / co_await ptr->foo(
_CO_AWAIT = re.compile(
    r"\bco_await\s+"
    r"(?P<target>"
    r"(?:"
    r"[A-Za-z_][\w:]*(?:\s*(?:\.|->)\s*[A-Za-z_]\w*)*"  # id / a.b / a->b
    r"|[A-Za-z_][\w:]*"
    r")"
    r")"
    r"(?P<call>\s*\()?",
    re.M,
)

# Return-type style: exec::task<void> Name(  or task<T> Name(
_TASK_FN = re.compile(
    r"(?P<ret>(?:[\w:]+::)*task\s*<[^;{}]{0,200}?>)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*\(",
    re.M,
)

# Method: Ret Class::Name(  — only when body later has co_await we mark coroutine
_METHOD = re.compile(
    r"(?P<ret>[\w:<>,\s*&]+?)\s+"
    r"(?P<cls>[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)::"
    r"(?P<name>[A-Za-z_]\w*|operator\s*\(\))\s*\(",
    re.M,
)

# Free function rough: type Name( at column-ish start
_FREE_FN = re.compile(
    r"(?:^|\n)\s*(?:(?:inline|static|constexpr|virtual|explicit|friend)\s+)*"
    r"(?P<ret>(?:[\w:]+::)*[\w]+(?:\s*<[^;{}]*>)?(?:\s*[*&]+)?)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*\([^;]*?\)\s*(?:const\s*)?(?:override\s*)?\{",
    re.M,
)

_CO_RETURN = re.compile(r"\bco_return\b|\bco_yield\b|\bco_await\b")

CPP_EXTS = {".cpp", ".cc", ".cxx", ".hpp", ".hh", ".h", ".cuh", ".cu", ".inl", ".ipp"}


@dataclass
class RawNode:
    name: str
    qualified_name: str
    kind: str  # function | coroutine | call_site
    file_path: str
    start_line: int
    end_line: int
    domain: str = "cpu"
    backend: str = "host"
    signature: str = ""


@dataclass
class RawEdge:
    source_qname: str
    target_name: str  # unresolved name; indexer resolves
    kind: str  # await | calls | device_call
    file_path: str
    line: int
    domain: str = "unknown"
    backend: str = ""
    raw_target: str = ""


@dataclass
class FileExtract:
    path: str
    nodes: list[RawNode] = field(default_factory=list)
    edges: list[RawEdge] = field(default_factory=list)
    device_hits: list[dict[str, Any]] = field(default_factory=list)


def strip_noise(src: str) -> str:
    """Remove strings/comments so regexes don't false-hit inside them."""

    def _sp(m: re.Match[str]) -> str:
        return " " * (m.end() - m.start())

    src = _STRING.sub(_sp, src)
    src = _COMMENT_BLOCK.sub(_sp, src)
    src = _COMMENT_LINE.sub(_sp, src)
    return src


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def load_device_rules(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        # Prefer packaged rules (works after pip install), then repo-level rules/
        candidates = [
            Path(__file__).resolve().parent / "rules" / "devices.json",
            Path(__file__).resolve().parent.parent / "rules" / "devices.json",
        ]
        for candidate in candidates:
            if candidate.is_file():
                path = candidate
                break
        else:
            raise FileNotFoundError(
                "devices.json not found; pass --rules or install package data"
            )
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("patterns") or [])


def match_device(text: str, rules: list[dict[str, str]]) -> tuple[str, str] | None:
    for rule in rules:
        needle = rule.get("match") or ""
        if needle and needle in text:
            return rule.get("domain", "unknown"), rule.get("backend", "")
    return None


def find_matching_brace(text: str, open_idx: int) -> int:
    """open_idx points at '{'. Return index of matching '}' or -1."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _body_after_paren(text: str, paren_open: int) -> tuple[int, int] | None:
    """From '(' of a function decl, find '{'..'}' body range indices."""
    i = paren_open
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                # skip trailing qualifiers
                while True:
                    if text.startswith("const", j):
                        j += 5
                    elif text.startswith("override", j):
                        j += 8
                    elif text.startswith("final", j):
                        j += 5
                    elif text.startswith("noexcept", j):
                        j += 8
                        if j < n and text[j] == "(":
                            d = 0
                            while j < n:
                                if text[j] == "(":
                                    d += 1
                                elif text[j] == ")":
                                    d -= 1
                                    if d == 0:
                                        j += 1
                                        break
                                j += 1
                        continue
                    elif text.startswith("->", j):
                        j += 2
                        while j < n and text[j] not in "{;":
                            j += 1
                        continue
                    else:
                        break
                    while j < n and text[j] in " \t\r\n":
                        j += 1
                if j < n and text[j] == "{":
                    end = find_matching_brace(text, j)
                    if end > 0:
                        return j, end
                return None
        i += 1
    return None


def _normalize_await_target(raw: str) -> str:
    raw = re.sub(r"\s+", "", raw)
    # a->b / a.b → keep last segment as primary call name hint, keep full as qname-ish
    return raw


def extract_file(path: Path, rel: str, rules: list[dict[str, str]]) -> FileExtract:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return FileExtract(path=rel)

    clean = strip_noise(text)
    out = FileExtract(path=rel)
    seen_qnames: set[str] = set()

    # Device hits anywhere in file (for tagging + edges from nearest function)
    for rule in rules:
        needle = rule["match"]
        start = 0
        while True:
            idx = clean.find(needle, start)
            if idx < 0:
                break
            out.device_hits.append(
                {
                    "match": needle,
                    "domain": rule.get("domain", "unknown"),
                    "backend": rule.get("backend", ""),
                    "line": line_of(clean, idx),
                }
            )
            start = idx + max(1, len(needle))

    # task<...> Name( → coroutine definitions (must have a body)
    for m in _TASK_FN.finditer(clean):
        name = m.group("name")
        paren = m.end() - 1  # at '('
        if clean[paren] != "(":
            paren = clean.find("(", m.start())
        body = _body_after_paren(clean, paren) if paren >= 0 else None
        if not body:
            continue  # declaration only — skip in V1
        start_line = line_of(clean, m.start())
        end_line = line_of(clean, body[1])
        qname = name
        if qname in seen_qnames:
            qname = f"{rel}::{name}@{start_line}"
        seen_qnames.add(qname)
        domain, backend = "cpu", "host"
        body_txt = clean[body[0] : body[1] + 1]
        hit = match_device(body_txt, rules)
        if hit:
            domain, backend = hit
        # Name itself is a device API (e.g. RunOnNpu)
        hit2 = match_device(name, rules)
        if hit2:
            domain, backend = hit2
        out.nodes.append(
            RawNode(
                name=name,
                qualified_name=qname,
                kind="coroutine",
                file_path=rel,
                start_line=start_line,
                end_line=end_line,
                domain=domain,
                backend=backend,
                signature=m.group("ret").strip() + " " + name,
            )
        )
        _extract_awaits_in_body(
            out, clean, body[0], body[1], qname, rel, rules
        )
        _extract_device_calls_in_body(
            out, clean, body[0], body[1], qname, rel, rules
        )

    # Free / method functions whose body contains co_await
    for rx in (_FREE_FN,):
        for m in rx.finditer(clean):
            name = m.group("name")
            if name in {"if", "for", "while", "switch", "catch", "return"}:
                continue
            # find '(' of this match — last ( before {
            brace = clean.find("{", m.start())
            if brace < 0:
                continue
            paren = clean.rfind("(", m.start(), brace)
            if paren < 0:
                continue
            body = _body_after_paren(clean, paren)
            if not body:
                continue
            body_txt = clean[body[0] : body[1] + 1]
            if not _CO_RETURN.search(body_txt):
                continue
            start_line = line_of(clean, m.start())
            end_line = line_of(clean, body[1])
            qname = name
            if qname in seen_qnames:
                continue  # already from task<> pattern
            seen_qnames.add(qname)
            domain, backend = "cpu", "host"
            hit = match_device(body_txt, rules)
            if hit:
                domain, backend = hit
            out.nodes.append(
                RawNode(
                    name=name,
                    qualified_name=qname,
                    kind="coroutine",
                    file_path=rel,
                    start_line=start_line,
                    end_line=end_line,
                    domain=domain,
                    backend=backend,
                    signature=(m.group("ret") or "").strip() + " " + name,
                )
            )
            _extract_awaits_in_body(
                out, clean, body[0], body[1], qname, rel, rules
            )
            _extract_device_calls_in_body(
                out, clean, body[0], body[1], qname, rel, rules
            )

    # Class::Method with co_await in body
    for m in _METHOD.finditer(clean):
        cls = m.group("cls")
        name = m.group("name").replace(" ", "")
        paren = m.end() - 1
        if clean[paren] != "(":
            paren = clean.find("(", m.start())
        body = _body_after_paren(clean, paren) if paren >= 0 else None
        if not body:
            continue
        body_txt = clean[body[0] : body[1] + 1]
        is_coro = bool(_CO_RETURN.search(body_txt))
        is_task_ret = "task<" in (m.group("ret") or "").replace(" ", "")
        if not (is_coro or is_task_ret):
            continue
        start_line = line_of(clean, m.start())
        end_line = line_of(clean, body[1])
        qname = f"{cls}::{name}"
        if qname in seen_qnames:
            continue
        seen_qnames.add(qname)
        domain, backend = "cpu", "host"
        hit = match_device(body_txt, rules)
        if hit:
            domain, backend = hit
        out.nodes.append(
            RawNode(
                name=name,
                qualified_name=qname,
                kind="coroutine" if (is_coro or is_task_ret) else "function",
                file_path=rel,
                start_line=start_line,
                end_line=end_line,
                domain=domain,
                backend=backend,
                signature=f"{(m.group('ret') or '').strip()} {qname}",
            )
        )
        _extract_awaits_in_body(out, clean, body[0], body[1], qname, rel, rules)
        _extract_device_calls_in_body(
            out, clean, body[0], body[1], qname, rel, rules
        )

    # Non-coroutine functions that still call device APIs (host glue)
    for m in _FREE_FN.finditer(clean):
        name = m.group("name")
        if name in {"if", "for", "while", "switch", "catch", "return"}:
            continue
        brace = clean.find("{", m.start())
        if brace < 0:
            continue
        paren = clean.rfind("(", m.start(), brace)
        if paren < 0:
            continue
        body = _body_after_paren(clean, paren)
        if not body:
            continue
        body_txt = clean[body[0] : body[1] + 1]
        hit = match_device(body_txt, rules)
        if not hit:
            continue
        if name in seen_qnames or any(
            n.qualified_name == name for n in out.nodes
        ):
            continue
        start_line = line_of(clean, m.start())
        end_line = line_of(clean, body[1])
        domain, backend = hit
        seen_qnames.add(name)
        out.nodes.append(
            RawNode(
                name=name,
                qualified_name=name,
                kind="function",
                file_path=rel,
                start_line=start_line,
                end_line=end_line,
                domain=domain,
                backend=backend,
                signature=(m.group("ret") or "").strip() + " " + name,
            )
        )
        _extract_device_calls_in_body(
            out, clean, body[0], body[1], name, rel, rules
        )

    # File-level co_await not inside a detected function → synthetic node
    for m in _CO_AWAIT.finditer(clean):
        line = line_of(clean, m.start())
        # skip if already captured as edge on same line from some function
        if any(e.line == line and e.kind == "await" for e in out.edges):
            continue
        # orphan: attach to file scope
        file_node = f"file:{rel}"
        if file_node not in seen_qnames:
            seen_qnames.add(file_node)
            out.nodes.append(
                RawNode(
                    name=Path(rel).name,
                    qualified_name=file_node,
                    kind="function",
                    file_path=rel,
                    start_line=1,
                    end_line=line_of(clean, len(clean) - 1) if clean else 1,
                    domain="cpu",
                    backend="host",
                )
            )
        target = _normalize_await_target(m.group("target"))
        out.edges.append(
            RawEdge(
                source_qname=file_node,
                target_name=target,
                kind="await",
                file_path=rel,
                line=line,
                raw_target=m.group(0)[:80],
            )
        )

    return out


def _extract_awaits_in_body(
    out: FileExtract,
    clean: str,
    body_lo: int,
    body_hi: int,
    source_qname: str,
    rel: str,
    rules: list[dict[str, str]],
) -> None:
    chunk = clean[body_lo : body_hi + 1]
    for m in _CO_AWAIT.finditer(chunk):
        abs_idx = body_lo + m.start()
        target = _normalize_await_target(m.group("target"))
        domain, backend = "unknown", ""
        hit = match_device(target, rules) or match_device(m.group(0), rules)
        if hit:
            domain, backend = hit
        out.edges.append(
            RawEdge(
                source_qname=source_qname,
                target_name=target,
                kind="await",
                file_path=rel,
                line=line_of(clean, abs_idx),
                domain=domain,
                backend=backend,
                raw_target=m.group(0)[:120],
            )
        )


def _extract_device_calls_in_body(
    out: FileExtract,
    clean: str,
    body_lo: int,
    body_hi: int,
    source_qname: str,
    rel: str,
    rules: list[dict[str, str]],
) -> None:
    chunk = clean[body_lo : body_hi + 1]
    # Longest match first; skip positions already covered (clEnqueue vs clEnqueueNDRange…)
    ranked = sorted(rules, key=lambda r: len(r.get("match") or ""), reverse=True)
    covered: list[tuple[int, int]] = []
    await_targets = {
        e.target_name.split("::")[-1]
        for e in out.edges
        if e.source_qname == source_qname and e.kind == "await"
    }
    for rule in ranked:
        needle = rule["match"]
        start = 0
        while True:
            idx = chunk.find(needle, start)
            if idx < 0:
                break
            end = idx + len(needle)
            if any(not (end <= a or idx >= b) for a, b in covered):
                start = idx + 1
                continue
            # If this API is already the target of co_await from same function, skip
            if needle.split("::")[-1] in await_targets or any(
                needle in t or t in needle for t in await_targets
            ):
                start = end
                continue
            covered.append((idx, end))
            abs_idx = body_lo + idx
            out.edges.append(
                RawEdge(
                    source_qname=source_qname,
                    target_name=needle,
                    kind="device_call",
                    file_path=rel,
                    line=line_of(clean, abs_idx),
                    domain=rule.get("domain", "unknown"),
                    backend=rule.get("backend", ""),
                    raw_target=needle,
                )
            )
            start = end
