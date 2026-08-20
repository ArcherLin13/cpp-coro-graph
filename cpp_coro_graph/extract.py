"""Syntax-level C++ extraction: function defs, calls, co_await, device tags.

V1.2 goals:
- Detect normal call chains (Foo / Class::Bar / a->b), not only co_await
- Detect coroutine bodies via co_await/co_return/co_yield
- Stay linear-time; avoid catastrophic regexes on big headers
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_FULL_PARSE_BYTES = 512 * 1024

_COMMENT_LINE = re.compile(r"//.*?$", re.M)
_COMMENT_BLOCK = re.compile(r"/\*[^*]*\*+(?:[^/*][^*]*\*+)*/")
_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')

_CO_AWAIT = re.compile(
    r"\bco_await\s+"
    r"(?P<target>[A-Za-z_][\w:]*(?:\s*(?:\.|->)\s*[A-Za-z_]\w*)*)"
    r"(?:\s*\()?",
    re.M,
)
_CO_AWAIT_MACRO = re.compile(
    r"\b(?:CO_AWAIT|COAWAIT|CoAwait)\s*\(\s*(?P<target>[A-Za-z_][\w:]*)",
    re.M,
)
_CORO_KW = re.compile(r"\b(?:co_await|co_return|co_yield|CO_AWAIT|COAWAIT|CoAwait)\b")

# Call-like: Name( / Qual::Name( / obj.Name( / ptr->Name(
_CALL = re.compile(
    r"(?:(?:[A-Za-z_][\w:]*|\)|\])\s*(?:\.|->)\s*)?"
    r"(?P<name>[A-Za-z_][\w:]*)\s*\(",
    re.M,
)

_CALL_KEYWORDS = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "sizeof",
        "typeof",
        "alignof",
        "decltype",
        "static_assert",
        "new",
        "delete",
        "case",
        "throw",
        "noexcept",
        "requires",
        "concept",
        "co_await",
        "co_return",
        "co_yield",
        "CO_AWAIT",
        "COAWAIT",
        "CoAwait",
        "sizeof...",
        "typeid",
        "emits",
        "and",
        "or",
        "not",
        "xor",
        "compl",
        "bitand",
        "bitor",
    }
)

DEFAULT_CORO_TYPES = (
    "task",
    "Task",
    "Lazy",
    "Future",
    "future",
    "Awaitable",
    "AsyncGenerator",
    "async_generator",
    "generator",
    "Generator",
)

CPP_EXTS = {
    ".cpp",
    ".cc",
    ".cxx",
    ".c",
    ".hpp",
    ".hh",
    ".h",
    ".cuh",
    ".cu",
    ".inl",
    ".ipp",
}


@dataclass
class RawNode:
    name: str
    qualified_name: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    domain: str = "cpu"
    backend: str = "host"
    signature: str = ""
    body_lo: int = -1
    body_hi: int = -1


@dataclass
class RawEdge:
    source_qname: str
    target_name: str
    kind: str
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
    skipped: str = ""
    has_coro_kw: bool = False


def strip_noise(src: str) -> str:
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
        candidates = [
            Path(__file__).resolve().parent / "rules" / "devices.json",
            Path(__file__).resolve().parent.parent / "rules" / "devices.json",
        ]
        for candidate in candidates:
            if candidate.is_file():
                path = candidate
                break
        else:
            raise FileNotFoundError("devices.json not found")
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("patterns") or [])


def load_coro_types(path: Path | None = None) -> tuple[str, ...]:
    candidates = []
    if path:
        candidates.append(path)
    candidates.extend(
        [
            Path(__file__).resolve().parent / "rules" / "coro_types.json",
            Path(__file__).resolve().parent.parent / "rules" / "coro_types.json",
        ]
    )
    for p in candidates:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            names = data.get("return_type_names") or []
            return tuple(dict.fromkeys([*DEFAULT_CORO_TYPES, *names]))
    return DEFAULT_CORO_TYPES


def match_device(text: str, rules: list[dict[str, str]]) -> tuple[str, str] | None:
    for rule in rules:
        needle = rule.get("match") or ""
        if needle and needle in text:
            return rule.get("domain", "unknown"), rule.get("backend", "")
    return None


def find_matching_brace(text: str, open_idx: int, max_scan: int = 3_000_000) -> int:
    depth = 0
    i = open_idx
    n = min(len(text), open_idx + max_scan)
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


def _skip_ws_back(text: str, i: int) -> int:
    while i >= 0 and text[i] in " \t\r\n":
        i -= 1
    return i


def _read_ident_back(text: str, i: int) -> tuple[str, int]:
    if i < 0:
        return "", i
    chars: list[str] = []
    while i >= 0:
        c = text[i]
        if c.isalnum() or c == "_":
            chars.append(c)
            i -= 1
            continue
        if c == ":" and i > 0 and text[i - 1] == ":":
            chars.append("::")
            i -= 2
            continue
        break
    return "".join(reversed(chars)), i


def _looks_like_control_or_type(text: str, name_start: int, qname: str) -> bool:
    name = qname.split("::")[-1]
    if name in _CALL_KEYWORDS or name in {"else", "try", "do", "namespace", "class", "struct", "enum", "union"}:
        return True
    # enum/class/struct X { — name_start preceded by those keywords
    k = _skip_ws_back(text, name_start - 1)
    prev, _ = _read_ident_back(text, k)
    if prev in {"class", "struct", "enum", "union", "namespace", "concept"}:
        return True
    return False


def parse_function_at_brace(text: str, brace_open: int) -> tuple[str, int, int, int] | None:
    """If `{` at brace_open starts a function body, return (qname, name_start, body_lo, body_hi)."""
    body_hi = find_matching_brace(text, brace_open)
    if body_hi < 0:
        return None

    j = _skip_ws_back(text, brace_open - 1)
    # Strip trailing qualifiers after parameter list
    for _ in range(40):
        if j < 0:
            return None
        chunk_end = j
        matched = False
        for kw in ("override", "final", "const", "volatile", "mutable"):
            L = len(kw)
            if j >= L - 1 and text[j - L + 1 : j + 1] == kw:
                # boundary check
                b = j - L
                if b < 0 or not (text[b].isalnum() or text[b] == "_"):
                    j = _skip_ws_back(text, j - L)
                    matched = True
                    break
        if matched:
            continue
        if j >= 0 and text[j] == ")":
            # Could be end of param list OR noexcept(...)
            d = 0
            p = j
            while p >= 0:
                if text[p] == ")":
                    d += 1
                elif text[p] == "(":
                    d -= 1
                    if d == 0:
                        break
                p -= 1
            if p < 0:
                return None
            before = _skip_ws_back(text, p - 1)
            ident, _ = _read_ident_back(text, before)
            if ident == "noexcept" or ident.endswith("noexcept"):
                j = _skip_ws_back(text, before - (len("noexcept")))
                # actually before already at end of noexcept
                j = _skip_ws_back(text, p - 1)
                # re-read: p points to '(' of noexcept(
                ident2, i2 = _read_ident_back(text, _skip_ws_back(text, p - 1))
                if ident2 == "noexcept":
                    j = _skip_ws_back(text, i2)
                    continue
            # This ')' closes the parameter list
            break
        if j >= 1 and text[j - 1 : j + 1] == "->":
            j = _skip_ws_back(text, j - 2)
            while j >= 0 and text[j] != ")":
                j -= 1
            continue
        if j >= 0 and text[j] in "&*":
            j = _skip_ws_back(text, j - 1)
            continue
        return None

    if j < 0 or text[j] != ")":
        return None

    # Match '(' of parameter list
    d = 0
    p = j
    while p >= 0:
        if text[p] == ")":
            d += 1
        elif text[p] == "(":
            d -= 1
            if d == 0:
                break
        p -= 1
    if p < 0:
        return None
    paren_open = p

    k = _skip_ws_back(text, paren_open - 1)
    qname, name_start = _read_ident_back(text, k)
    if not qname:
        return None
    if _looks_like_control_or_type(text, name_start, qname):
        return None
    # Reject if immediately after '=' (lambda) : ](
    pre = _skip_ws_back(text, name_start - 1)
    if pre >= 0 and text[pre] in {"=", "[", "]", "(", ","}:
        # likely lambda or call, not a definition name — definitions have return type before name
        # Allow Class::Method where pre is letter from return type — OK
        if text[pre] in {"=", "["}:
            return None
    return qname, name_start, brace_open, body_hi


def iter_function_defs(text: str, max_funcs: int = 5000) -> list[tuple[str, int, int, int]]:
    """Linear scan for function-definition bodies."""
    out: list[tuple[str, int, int, int]] = []
    i = 0
    n = len(text)
    while i < n and len(out) < max_funcs:
        if text[i] != "{":
            i += 1
            continue
        # Skip brace init / compound that isn't a function: require a ')' not far before
        j = _skip_ws_back(text, i - 1)
        # Allow const/override/... between ) and {
        probe = j
        ok = False
        for _ in range(20):
            if probe < 0:
                break
            if text[probe] == ")":
                ok = True
                break
            # walk back over a keyword
            advanced = False
            for kw in ("override", "final", "const", "volatile", "noexcept"):
                L = len(kw)
                if probe >= L - 1 and text[probe - L + 1 : probe + 1] == kw:
                    b = probe - L
                    if b < 0 or not (text[b].isalnum() or text[b] == "_"):
                        probe = _skip_ws_back(text, probe - L)
                        advanced = True
                        break
            if advanced:
                continue
            if probe >= 0 and text[probe] == ")":
                # noexcept(
                d = 0
                p = probe
                while p >= 0:
                    if text[p] == ")":
                        d += 1
                    elif text[p] == "(":
                        d -= 1
                        if d == 0:
                            break
                    p -= 1
                probe = _skip_ws_back(text, p - 1) if p >= 0 else -1
                continue
            break
        if not ok:
            i += 1
            continue
        parsed = parse_function_at_brace(text, i)
        if parsed:
            qname, name_start, lo, hi = parsed
            out.append((qname, name_start, lo, hi))
            i = hi + 1
            continue
        i += 1
    return out


def _normalize_target(raw: str) -> str:
    return re.sub(r"\s+", "", raw)


def _owner_at(bodies: list[tuple[str, int, int]], idx: int) -> str | None:
    owner = None
    best = None
    for qn, lo, hi in bodies:
        if lo <= idx <= hi:
            span = hi - lo
            if best is None or span < best:
                best = span
                owner = qn
    return owner


def extract_file(
    path: Path,
    rel: str,
    rules: list[dict[str, str]],
    coro_types: tuple[str, ...] | None = None,
) -> FileExtract:
    out = FileExtract(path=rel)
    coro_types = coro_types or DEFAULT_CORO_TYPES
    try:
        size = path.stat().st_size
    except OSError:
        out.skipped = "stat-failed"
        return out

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        out.skipped = "read-failed"
        return out

    out.has_coro_kw = bool(_CORO_KW.search(text))
    has_device = any((r.get("match") or "") in text for r in rules)

    if size > MAX_FULL_PARSE_BYTES:
        out.skipped = f"too-large:{size}"
        return out

    # Always parse C/C++ for call graph (user expects normal call chains).
    # Skip empty-ish files.
    if len(text) < 3:
        return out

    clean = strip_noise(text)
    defs = iter_function_defs(clean)
    seen_qnames: set[str] = set()
    bodies: list[tuple[str, int, int]] = []

    for qname, name_start, body_lo, body_hi in defs:
        name = qname.split("::")[-1]
        uniq = qname
        if uniq in seen_qnames:
            uniq = f"{qname}@{line_of(clean, name_start)}"
        seen_qnames.add(uniq)
        body_txt = clean[body_lo : body_hi + 1]
        is_coro = bool(_CORO_KW.search(body_txt))
        # Also treat task-like return nearby as coroutine hint
        head = clean[max(0, name_start - 80) : name_start]
        if any(t in head for t in coro_types):
            is_coro = True
        domain, backend = "cpu", "host"
        hit = match_device(body_txt, rules) or match_device(name, rules)
        if hit:
            domain, backend = hit
        out.nodes.append(
            RawNode(
                name=name,
                qualified_name=uniq,
                kind="coroutine" if is_coro else "function",
                file_path=rel,
                start_line=line_of(clean, name_start),
                end_line=line_of(clean, body_hi),
                domain=domain,
                backend=backend,
                signature=uniq,
                body_lo=body_lo,
                body_hi=body_hi,
            )
        )
        bodies.append((uniq, body_lo, body_hi))

    # co_await edges
    for rx in (_CO_AWAIT, _CO_AWAIT_MACRO):
        for m in rx.finditer(clean):
            idx = m.start()
            target = _normalize_target(m.group("target"))
            owner = _owner_at(bodies, idx)
            if owner is None:
                file_node = f"file:{rel}"
                if file_node not in seen_qnames:
                    seen_qnames.add(file_node)
                    out.nodes.append(
                        RawNode(
                            name=Path(rel).name,
                            qualified_name=file_node,
                            kind="file",
                            file_path=rel,
                            start_line=1,
                            end_line=1,
                        )
                    )
                owner = file_node
            domain, backend = "unknown", ""
            hit = match_device(target, rules)
            if hit:
                domain, backend = hit
            out.edges.append(
                RawEdge(
                    source_qname=owner,
                    target_name=target,
                    kind="await",
                    file_path=rel,
                    line=line_of(clean, idx),
                    domain=domain,
                    backend=backend,
                    raw_target=m.group(0)[:120],
                )
            )

    # Normal call edges inside each function body
    for qn, lo, hi in bodies:
        chunk = clean[lo : hi + 1]
        # Avoid counting the function's own declarator — body starts at '{'
        for m in _CALL.finditer(chunk):
            name = m.group("name")
            simple = name.split("::")[-1]
            if simple in _CALL_KEYWORDS or name in _CALL_KEYWORDS:
                continue
            # skip placement that looks like a definition inside (rare)
            abs_idx = lo + m.start()
            # Don't emit call to self name at the very start (declarator already outside body)
            out.edges.append(
                RawEdge(
                    source_qname=qn,
                    target_name=_normalize_target(name),
                    kind="calls",
                    file_path=rel,
                    line=line_of(clean, abs_idx),
                    raw_target=m.group(0)[:80],
                )
            )

    # Device API edges
    if has_device:
        ranked = sorted(rules, key=lambda r: len(r.get("match") or ""), reverse=True)
        for rule in ranked:
            needle = rule["match"]
            start = 0
            while True:
                idx = clean.find(needle, start)
                if idx < 0:
                    break
                owner = _owner_at(bodies, idx)
                if owner is None:
                    file_node = f"file:{rel}"
                    if file_node not in seen_qnames:
                        seen_qnames.add(file_node)
                        out.nodes.append(
                            RawNode(
                                name=Path(rel).name,
                                qualified_name=file_node,
                                kind="file",
                                file_path=rel,
                                start_line=1,
                                end_line=1,
                                domain=rule.get("domain", "unknown"),
                                backend=rule.get("backend", ""),
                            )
                        )
                    owner = file_node
                out.edges.append(
                    RawEdge(
                        source_qname=owner,
                        target_name=needle,
                        kind="device_call",
                        file_path=rel,
                        line=line_of(clean, idx),
                        domain=rule.get("domain", "unknown"),
                        backend=rule.get("backend", ""),
                        raw_target=needle,
                    )
                )
                out.device_hits.append(
                    {
                        "match": needle,
                        "domain": rule.get("domain", "unknown"),
                        "backend": rule.get("backend", ""),
                        "line": line_of(clean, idx),
                    }
                )
                start = idx + max(1, len(needle))

    return out
