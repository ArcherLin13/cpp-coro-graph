"""Syntax-level C++ extraction: function defs/decls, calls, co_await, device tags.

Goals:
- Every function definition AND declaration is a node (headers included)
- Class/struct scope qualifies members (Foo::bar)
- Detect call chains + co_await; stay linear-time
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
        # destructor: ~Name or Qual::~Name
        if c == "~" and chars:
            chars.append(c)
            i -= 1
            continue
        if c == ":" and i > 0 and text[i - 1] == ":":
            chars.append("::")
            i -= 2
            continue
        break
    return "".join(reversed(chars)), i


def _read_func_name_before_paren(text: str, paren_close: int) -> tuple[str, int] | None:
    """Given `)` of param list, return (qname, name_start) including destructors."""
    d = 0
    p = paren_close
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
    k = _skip_ws_back(text, p - 1)
    qname, name_start = _read_ident_back(text, k)
    if not qname:
        return None
    # Unqualified destructor: ~Name (tilde not consumed if chars was empty edge-case)
    pre = _skip_ws_back(text, name_start - 1)
    if pre >= 0 and text[pre] == "~" and not qname.startswith("~"):
        qname = "~" + qname
        name_start = pre
    if _looks_like_control_or_type(text, name_start, qname):
        return None
    return qname, name_start


_TRAILING_KW = ("override", "final", "const", "volatile", "mutable", "noexcept")


def _looks_like_control_or_type(text: str, name_start: int, qname: str) -> bool:
    name = qname.split("::")[-1].lstrip("~")
    if name in _CALL_KEYWORDS or name in {
        "else",
        "try",
        "do",
        "namespace",
        "class",
        "struct",
        "enum",
        "union",
    }:
        return True
    k = _skip_ws_back(text, name_start - 1)
    prev, _ = _read_ident_back(text, k)
    if prev in {"class", "struct", "enum", "union", "namespace", "concept"}:
        return True
    return False


def _skip_attr_back(text: str, j: int) -> int:
    """Skip [[...]] attributes ending at j."""
    j = _skip_ws_back(text, j)
    if j >= 1 and text[j - 1 : j + 1] == "]]":
        depth = 0
        p = j
        while p >= 1:
            if text[p - 1 : p + 1] == "]]":
                depth += 1
                p -= 2
                continue
            if text[p - 1 : p + 1] == "[[":
                depth -= 1
                p -= 2
                if depth == 0:
                    return _skip_ws_back(text, p)
                continue
            p -= 1
    return j


def _find_param_close_back(text: str, j: int) -> int | None:
    """From position just before `{` / `;` / `=`, find `)` that closes the param list."""
    j = _skip_ws_back(text, j)
    j = _skip_attr_back(text, j)
    for _ in range(50):
        if j < 0:
            return None
        matched = False
        for kw in _TRAILING_KW:
            L = len(kw)
            if j >= L - 1 and text[j - L + 1 : j + 1] == kw:
                b = j - L
                if b < 0 or not (text[b].isalnum() or text[b] == "_"):
                    j = _skip_ws_back(text, j - L)
                    matched = True
                    break
        if matched:
            continue
        if j >= 0 and text[j] == ")":
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
            ident, i2 = _read_ident_back(text, before)
            if ident == "noexcept":
                j = _skip_ws_back(text, i2)
                continue
            return j
        if j >= 1 and text[j - 1 : j + 1] == "->":
            j = _skip_ws_back(text, j - 2)
            while j >= 0 and text[j] != ")":
                j -= 1
            continue
        if j >= 0 and text[j] in "&*":
            j = _skip_ws_back(text, j - 1)
            continue
        return None
    return None


def _has_return_type_or_ctor(
    text: str, name_start: int, qname: str, class_stack: list[str]
) -> bool:
    """Reject bare call-statements like `bar();`; allow ctors/dtors in class."""
    simple = qname.split("::")[-1]
    bare = simple.lstrip("~")
    if class_stack and (bare == class_stack[-1] or simple == "~" + class_stack[-1]):
        return True
    # Out-of-line Class::Class / Class::~Class
    parts = qname.split("::")
    if len(parts) >= 2:
        if parts[-1] == parts[-2] or parts[-1] == "~" + parts[-2]:
            return True
    pre = _skip_ws_back(text, name_start - 1)
    if pre < 0:
        return False
    if text[pre] in {"=", "[", "]", "(", ",", ";", "{", "}"}:
        # decltype(auto) Name — pre is ')'
        if text[pre] == ")":
            d = 0
            p = pre
            while p >= 0:
                if text[p] == ")":
                    d += 1
                elif text[p] == "(":
                    d -= 1
                    if d == 0:
                        break
                p -= 1
            if p >= 0:
                ident, _ = _read_ident_back(text, _skip_ws_back(text, p - 1))
                if ident == "decltype":
                    return True
        return False
    if text[pre] in {"*", "&", ">", ":"}:
        return True
    ident, _ = _read_ident_back(text, pre)
    if not ident:
        return False
    if ident in {
        "if",
        "for",
        "while",
        "switch",
        "return",
        "sizeof",
        "case",
        "catch",
        "else",
    }:
        return False
    return True


def parse_function_at_brace(
    text: str, brace_open: int, class_stack: list[str] | None = None
) -> tuple[str, int, int, int] | None:
    """If `{` starts a function body, return (qname, name_start, body_lo, body_hi)."""
    class_stack = class_stack or []
    body_hi = find_matching_brace(text, brace_open)
    if body_hi < 0:
        return None
    paren_close = _find_param_close_back(text, brace_open - 1)
    if paren_close is None:
        return None
    parsed = _read_func_name_before_paren(text, paren_close)
    if not parsed:
        return None
    qname, name_start = parsed
    if not _has_return_type_or_ctor(text, name_start, qname, class_stack):
        return None
    return qname, name_start, brace_open, body_hi


def parse_function_at_semicolon(
    text: str, semi: int, class_stack: list[str] | None = None
) -> tuple[str, int] | None:
    """If `;` ends a function declaration / =default / =delete, return (qname, name_start)."""
    class_stack = class_stack or []
    j = _skip_ws_back(text, semi - 1)
    # = default / = delete
    if j >= 0 and text[j] in "tede":
        for kw in ("default", "delete"):
            L = len(kw)
            if j >= L - 1 and text[j - L + 1 : j + 1] == kw:
                b = j - L
                if b < 0 or not (text[b].isalnum() or text[b] == "_"):
                    j = _skip_ws_back(text, j - L)
                    if j >= 0 and text[j] == "=":
                        j = _skip_ws_back(text, j - 1)
                    break
    paren_close = _find_param_close_back(text, j)
    if paren_close is None:
        return None
    parsed = _read_func_name_before_paren(text, paren_close)
    if not parsed:
        return None
    qname, name_start = parsed
    if not _has_return_type_or_ctor(text, name_start, qname, class_stack):
        return None
    return qname, name_start


def _collect_brace_scopes(text: str) -> tuple[dict[int, str], dict[int, str]]:
    """Return (ns_push_at_brace, class_push_at_brace)."""
    ns_push: dict[int, str] = {}
    class_push: dict[int, str] = {}
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("namespace", i) and (
            i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")
        ):
            m = _NS_START.match(text, i)
            if m:
                brace = text.find("{", m.end() - 1)
                if brace > 0:
                    ns_push[brace] = m.group(1).replace(" ", "")
                    i = brace + 1
                    continue
            m2 = _NS_ANON.match(text, i)
            if m2:
                brace = text.find("{", m2.end() - 1)
                if brace > 0:
                    ns_push[brace] = ""
                    i = brace + 1
                    continue
        # skip enum class / enum struct — not a method container we care about
        if text.startswith("enum", i) and (
            i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")
        ):
            i += 4
            continue
        m = _CLASS_START.match(text, i)
        if m:
            brace = text.find("{", m.end() - 1)
            if brace > 0 and brace - m.start() < 220:
                class_push[brace] = m.group(1)
                i = brace + 1
                continue
        i += 1
    return ns_push, class_push


def _qualify_with_class(local_qname: str, class_stack: list[str]) -> str:
    if not class_stack:
        return local_qname
    if "::" in local_qname:
        return local_qname
    return f"{'::'.join(class_stack)}::{local_qname}"


def iter_functions(
    text: str, max_funcs: int = 12000
) -> list[tuple[str, int, int, int, str, str]]:
    """Return (local_qname, name_start, body_lo, body_hi, namespace, kind).

    kind is 'function' (definition with body) or 'declaration'.
    body_lo/body_hi are -1 for declarations.
    namespace includes enclosing namespaces only (class is baked into local_qname).
    """
    out: list[tuple[str, int, int, int, str, str]] = []
    ns_push, class_push = _collect_brace_scopes(text)
    n = len(text)
    ns_stack: list[str] = []
    class_stack: list[str] = []
    # stack of (kind, open_brace) for ns/class only
    scope_braces: list[tuple[str, int]] = []
    # function body ranges to skip when hunting declarations
    def_ranges: list[tuple[int, int]] = []

    def in_def_body(pos: int) -> bool:
        for lo, hi in def_ranges:
            if lo < pos < hi:
                return True
        return False

    i = 0
    while i < n and len(out) < max_funcs:
        if i in ns_push:
            ns_stack.append(ns_push[i])
            scope_braces.append(("ns", i))
            i += 1
            continue
        if i in class_push:
            class_stack.append(class_push[i])
            scope_braces.append(("class", i))
            i += 1
            continue
        if text[i] == "}" and scope_braces:
            kind, open_b = scope_braces[-1]
            close_b = find_matching_brace(text, open_b)
            if close_b == i:
                scope_braces.pop()
                if kind == "ns" and ns_stack:
                    ns_stack.pop()
                elif kind == "class" and class_stack:
                    class_stack.pop()
                i += 1
                continue

        if text[i] == "{":
            # probe: function definition?
            j = _skip_ws_back(text, i - 1)
            probe = j
            ok = False
            for _ in range(24):
                if probe < 0:
                    break
                if text[probe] == ")":
                    ok = True
                    break
                advanced = False
                for kw in _TRAILING_KW:
                    L = len(kw)
                    if probe >= L - 1 and text[probe - L + 1 : probe + 1] == kw:
                        b = probe - L
                        if b < 0 or not (text[b].isalnum() or text[b] == "_"):
                            probe = _skip_ws_back(text, probe - L)
                            advanced = True
                            break
                if advanced:
                    continue
                if probe >= 1 and text[probe - 1 : probe + 1] == "]]":
                    probe = _skip_attr_back(text, probe)
                    continue
                break
            if ok and i not in ns_push and i not in class_push:
                parsed = parse_function_at_brace(text, i, class_stack)
                if parsed:
                    local_qname, name_start, lo, hi = parsed
                    local_qname = _qualify_with_class(local_qname, class_stack)
                    ns = "::".join(x for x in ns_stack if x)
                    out.append((local_qname, name_start, lo, hi, ns, "function"))
                    def_ranges.append((lo, hi))
                    i = hi + 1
                    continue
            i += 1
            continue

        if text[i] == ";" and not in_def_body(i):
            parsed = parse_function_at_semicolon(text, i, class_stack)
            if parsed:
                local_qname, name_start = parsed
                local_qname = _qualify_with_class(local_qname, class_stack)
                ns = "::".join(x for x in ns_stack if x)
                # = default / = delete are definitions without a brace body
                window = text[max(0, i - 24) : i]
                kind = (
                    "function"
                    if re.search(r"=\s*(?:default|delete)\s*$", window)
                    else "declaration"
                )
                out.append((local_qname, name_start, -1, -1, ns, kind))
            i += 1
            continue

        i += 1
    return out


# Back-compat alias
def iter_function_defs(
    text: str, max_funcs: int = 8000
) -> list[tuple[str, int, int, int, str]]:
    return [
        (q, ns_start, lo, hi, ns)
        for q, ns_start, lo, hi, ns, kind in iter_functions(text, max_funcs)
        if kind == "function"
    ]


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
    funcs = iter_functions(clean)
    seen_qnames: set[str] = set()
    bodies: list[tuple[str, int, int, str]] = []
    # Definitions first so declarations don't steal the canonical qname
    ordered = [f for f in funcs if f[5] == 'function'] + [
        f for f in funcs if f[5] == 'declaration'
    ]

    for local_qname, name_start, body_lo, body_hi, ns, kind in ordered:
        qname = _qualify(local_qname, ns)
        leaf = local_qname.split('::')[-1]
        name = leaf
        uniq = qname
        if uniq in seen_qnames:
            # Same symbol already recorded (usually definition) — skip extra decl
            if kind == 'declaration':
                continue
            uniq = f'{qname}@{line_of(clean, name_start)}'
        seen_qnames.add(uniq)
        is_coro = False
        domain, backend = 'cpu', 'host'
        if kind == 'function' and body_lo >= 0:
            body_txt = clean[body_lo : body_hi + 1]
            is_coro = bool(_CORO_KW.search(body_txt))
            head = clean[max(0, name_start - 80) : name_start]
            if any(t in head for t in coro_types):
                is_coro = True
            hit = match_device(body_txt, rules) or match_device(name, rules)
            if hit:
                domain, backend = hit
        else:
            head = clean[max(0, name_start - 80) : name_start]
            if any(t in head for t in coro_types):
                is_coro = True
            hit = match_device(name, rules)
            if hit:
                domain, backend = hit
        node_kind = (
            'coroutine'
            if is_coro
            else ('function' if kind == 'function' else 'declaration')
        )
        out.nodes.append(
            RawNode(
                name=name,
                qualified_name=uniq,
                kind=node_kind,
                file_path=rel,
                start_line=line_of(clean, name_start),
                end_line=line_of(clean, body_hi if body_hi >= 0 else name_start),
                domain=domain,
                backend=backend,
                signature=uniq,
                body_lo=body_lo,
                body_hi=body_hi,
                namespace=ns,
            )
        )
        if kind == 'function' and body_lo >= 0:
            bodies.append((uniq, body_lo, body_hi, ns))

    body_triples = [(q, lo, hi) for q, lo, hi, _ in bodies]
    ns_of = {q: ns for q, _, _, ns in bodies}
    for n in out.nodes:
        ns_of.setdefault(n.qualified_name, n.namespace)

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
