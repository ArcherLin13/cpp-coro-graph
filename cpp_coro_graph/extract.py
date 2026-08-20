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
    namespace: str = ""


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
    source_namespace: str = ""


@dataclass
class FileExtract:
    path: str
    nodes: list[RawNode] = field(default_factory=list)
    edges: list[RawEdge] = field(default_factory=list)
    device_hits: list[dict[str, Any]] = field(default_factory=list)
    skipped: str = ""
    has_coro_kw: bool = False
    usings: list[str] = field(default_factory=list)


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


def load_thread_rules(path: Path | None = None) -> list[dict[str, str]]:
    candidates = []
    if path:
        candidates.append(path)
    candidates.extend(
        [
            Path(__file__).resolve().parent / "rules" / "thread_apis.json",
            Path(__file__).resolve().parent.parent / "rules" / "thread_apis.json",
        ]
    )
    for p in candidates:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            return list(data.get("patterns") or [])
    return []


def classify_call_kind(target: str, thread_rules: list[dict[str, str]]) -> str:
    """Return calls | handoff | spawns based on thread API rules."""
    for rule in thread_rules:
        needle = rule.get("match") or ""
        if needle and needle in target:
            return rule.get("kind") or "handoff"
    simple = target.split("::")[-1]
    for rule in thread_rules:
        needle = rule.get("match") or ""
        if needle and (needle == simple or needle.endswith("::" + simple)):
            return rule.get("kind") or "handoff"
    return "calls"


def extract_usings(text: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"\busing\s+namespace\s+([A-Za-z_][\w:]*)\s*;", text):
        out.append(m.group(1))
    for m in re.finditer(r"\busing\s+([A-Za-z_][\w:]*)\s*;", text):
        # using foo::Bar; → treat as alias namespace foo for Bar resolution
        q = m.group(1)
        if "::" in q:
            out.append(q.rsplit("::", 1)[0])
            out.append(q)  # full for exact
        else:
            out.append(q)
    # unique preserve order
    return list(dict.fromkeys(out))


_NS_START = re.compile(r"\bnamespace\s+([A-Za-z_][\w:]*)\s*\{")
_NS_ANON = re.compile(r"\bnamespace\s*\{")
_CLASS_START = re.compile(
    r"\b(?:class|struct)\s+([A-Za-z_]\w*)\b[^;{]{0,200}\{"
)


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


def iter_function_defs(
    text: str, max_funcs: int = 8000
) -> list[tuple[str, int, int, int, str]]:
    """Return (local_qname, name_start, body_lo, body_hi, namespace).

    namespace is A::B enclosing namespaces (not including class scope in v1.3 —
    class methods still appear as Class::Method when written that way in source).
    """
    out: list[tuple[str, int, int, int, str]] = []
    # Precompute namespace regions via brace stack of namespace opens
    ns_events: list[tuple[int, str, str]] = []  # (pos, 'push'|'pop', name)
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("namespace", i) and (
            i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")
        ):
            m = _NS_START.match(text, i)
            if m:
                # find {
                brace = text.find("{", m.end() - 1)
                if brace > 0:
                    ns_events.append((brace, "push", m.group(1).replace(" ", "")))
                    i = brace + 1
                    continue
            m2 = _NS_ANON.match(text, i)
            if m2:
                brace = text.find("{", m2.end() - 1)
                if brace > 0:
                    ns_events.append((brace, "push", ""))
                    i = brace + 1
                    continue
        i += 1

    # Map each namespace push brace to its matching close, build stack timeline
    # Simpler: while scanning for function braces, maintain ns_stack using events
    push_at = {pos: name for pos, kind, name in ns_events if kind == "push"}

    ns_stack: list[str] = []
    ns_brace_stack: list[int] = []  # brace positions that opened namespaces
    i = 0
    while i < n and len(out) < max_funcs:
        if i in push_at:
            ns_stack.append(push_at[i])
            ns_brace_stack.append(i)
            i += 1
            continue
        if text[i] == "}" and ns_brace_stack:
            # May close namespace — check if this closes the innermost ns brace
            open_b = ns_brace_stack[-1]
            close_b = find_matching_brace(text, open_b)
            if close_b == i:
                ns_brace_stack.pop()
                if ns_stack:
                    ns_stack.pop()
                i += 1
                continue
        if text[i] != "{":
            i += 1
            continue
        # skip namespace braces themselves
        if i in push_at:
            i += 1
            continue
        j = _skip_ws_back(text, i - 1)
        probe = j
        ok = False
        for _ in range(20):
            if probe < 0:
                break
            if text[probe] == ")":
                ok = True
                break
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
            local_qname, name_start, lo, hi = parsed
            ns = "::".join(x for x in ns_stack if x)
            out.append((local_qname, name_start, lo, hi, ns))
            i = hi + 1
            continue
        i += 1
    return out


def _qualify(local_qname: str, namespace: str) -> str:
    if not namespace:
        return local_qname
    if local_qname.startswith(namespace + "::"):
        return local_qname
    # Class::Method already qualified — still nest under namespace
    return f"{namespace}::{local_qname}"


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
    thread_rules: list[dict[str, str]] | None = None,
) -> FileExtract:
    out = FileExtract(path=rel)
    coro_types = coro_types or DEFAULT_CORO_TYPES
    thread_rules = thread_rules if thread_rules is not None else load_thread_rules()
    try:
        size = path.stat().st_size
    except OSError:
        out.skipped = 'stat-failed'
        return out

    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        out.skipped = 'read-failed'
        return out

    out.has_coro_kw = bool(_CORO_KW.search(text))
    out.usings = extract_usings(text)
    has_device = any((r.get('match') or '') in text for r in rules)

    if size > MAX_FULL_PARSE_BYTES:
        out.skipped = f'too-large:{size}'
        return out
    if len(text) < 3:
        return out

    clean = strip_noise(text)
    defs = iter_function_defs(clean)
    seen_qnames: set[str] = set()
    bodies: list[tuple[str, int, int, str]] = []

    for local_qname, name_start, body_lo, body_hi, ns in defs:
        qname = _qualify(local_qname, ns)
        name = local_qname.split('::')[-1]
        uniq = qname
        if uniq in seen_qnames:
            uniq = f'{qname}@{line_of(clean, name_start)}'
        seen_qnames.add(uniq)
        body_txt = clean[body_lo : body_hi + 1]
        is_coro = bool(_CORO_KW.search(body_txt))
        head = clean[max(0, name_start - 80) : name_start]
        if any(t in head for t in coro_types):
            is_coro = True
        domain, backend = 'cpu', 'host'
        hit = match_device(body_txt, rules) or match_device(name, rules)
        if hit:
            domain, backend = hit
        out.nodes.append(
            RawNode(
                name=name,
                qualified_name=uniq,
                kind='coroutine' if is_coro else 'function',
                file_path=rel,
                start_line=line_of(clean, name_start),
                end_line=line_of(clean, body_hi),
                domain=domain,
                backend=backend,
                signature=uniq,
                body_lo=body_lo,
                body_hi=body_hi,
                namespace=ns,
            )
        )
        bodies.append((uniq, body_lo, body_hi, ns))

    body_triples = [(q, lo, hi) for q, lo, hi, _ in bodies]
    ns_of = {q: ns for q, _, _, ns in bodies}

    for rx in (_CO_AWAIT, _CO_AWAIT_MACRO):
        for m in rx.finditer(clean):
            idx = m.start()
            target = _normalize_target(m.group('target'))
            owner = _owner_at(body_triples, idx)
            if owner is None:
                file_node = f'file:{rel}'
                if file_node not in seen_qnames:
                    seen_qnames.add(file_node)
                    out.nodes.append(
                        RawNode(
                            name=Path(rel).name,
                            qualified_name=file_node,
                            kind='file',
                            file_path=rel,
                            start_line=1,
                            end_line=1,
                        )
                    )
                owner = file_node
            domain, backend = 'unknown', ''
            hit = match_device(target, rules)
            if hit:
                domain, backend = hit
            out.edges.append(
                RawEdge(
                    source_qname=owner,
                    target_name=target,
                    kind='await',
                    file_path=rel,
                    line=line_of(clean, idx),
                    domain=domain,
                    backend=backend,
                    raw_target=m.group(0)[:120],
                    source_namespace=ns_of.get(owner, ''),
                )
            )

    for qn, lo, hi, ns in bodies:
        chunk = clean[lo : hi + 1]
        for m in _CALL.finditer(chunk):
            name = m.group('name')
            simple = name.split('::')[-1]
            if simple in _CALL_KEYWORDS or name in _CALL_KEYWORDS:
                continue
            abs_idx = lo + m.start()
            target = _normalize_target(name)
            kind = classify_call_kind(target, thread_rules)
            out.edges.append(
                RawEdge(
                    source_qname=qn,
                    target_name=target,
                    kind=kind,
                    file_path=rel,
                    line=line_of(clean, abs_idx),
                    raw_target=m.group(0)[:80],
                    source_namespace=ns,
                )
            )
            if kind in {'spawns', 'handoff'}:
                rest = chunk[m.end() : m.end() + 120]
                cm = re.match(r'\s*(?:&)?([A-Za-z_][\w:]*)', rest)
                if cm:
                    worker = _normalize_target(cm.group(1))
                    if worker.split('::')[-1] not in _CALL_KEYWORDS and worker != target:
                        out.edges.append(
                            RawEdge(
                                source_qname=qn,
                                target_name=worker,
                                kind='spawns',
                                file_path=rel,
                                line=line_of(clean, abs_idx),
                                raw_target=f'via {target} -> {worker}',
                                source_namespace=ns,
                            )
                        )

    if has_device:
        ranked = sorted(rules, key=lambda r: len(r.get('match') or ''), reverse=True)
        covered_ranges: list[tuple[int, int]] = []
        for rule in ranked:
            needle = rule['match']
            start = 0
            while True:
                idx = clean.find(needle, start)
                if idx < 0:
                    break
                end = idx + len(needle)
                if any(not (end <= a or idx >= b) for a, b in covered_ranges):
                    start = idx + 1
                    continue
                covered_ranges.append((idx, end))
                owner = _owner_at(body_triples, idx)
                if owner is None:
                    file_node = f'file:{rel}'
                    if file_node not in seen_qnames:
                        seen_qnames.add(file_node)
                        out.nodes.append(
                            RawNode(
                                name=Path(rel).name,
                                qualified_name=file_node,
                                kind='file',
                                file_path=rel,
                                start_line=1,
                                end_line=1,
                                domain=rule.get('domain', 'unknown'),
                                backend=rule.get('backend', ''),
                            )
                        )
                    owner = file_node
                out.edges.append(
                    RawEdge(
                        source_qname=owner,
                        target_name=needle,
                        kind='device_call',
                        file_path=rel,
                        line=line_of(clean, idx),
                        domain=rule.get('domain', 'unknown'),
                        backend=rule.get('backend', ''),
                        raw_target=needle,
                        source_namespace=ns_of.get(owner, ''),
                    )
                )
                out.device_hits.append(
                    {
                        'match': needle,
                        'domain': rule.get('domain', 'unknown'),
                        'backend': rule.get('backend', ''),
                        'line': line_of(clean, idx),
                    }
                )
                start = end

    return out
