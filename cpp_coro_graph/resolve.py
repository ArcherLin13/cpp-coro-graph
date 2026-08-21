"""Cross-file / cross-namespace symbol resolution."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SymbolRef:
    node_id: str
    name: str
    qualified_name: str
    file_path: str
    namespace: str = ""
    kind: str = "function"


def _rank(kind: str) -> int:
    if kind in {"function", "coroutine"}:
        return 2
    if kind == "declaration":
        return 1
    return 0


@dataclass
class SymbolIndex:
    by_qname: dict[str, str] = field(default_factory=dict)  # qname -> node_id
    by_qname_rank: dict[str, int] = field(default_factory=dict)
    by_name: dict[str, list[SymbolRef]] = field(default_factory=dict)
    by_file_qname: dict[tuple[str, str], str] = field(default_factory=dict)
    # base qname (without @line suffix) -> list of (node_id, rank)
    _overloads: dict[str, list[tuple[str, int]]] = field(default_factory=dict)

    def _base_qname(self, qname: str) -> str:
        """Strip @line suffix to get the base qualified name."""
        at = qname.rfind("@")
        if at > 0 and qname[at + 1 :].isdigit():
            return qname[:at]
        return qname

    def _put_qname(self, key: str, ref: SymbolRef) -> None:
        r = _rank(ref.kind)
        prev = self.by_qname_rank.get(key, -1)
        if r >= prev:
            self.by_qname[key] = ref.node_id
            self.by_qname_rank[key] = r

    def add(self, ref: SymbolRef) -> None:
        self._put_qname(ref.qualified_name, ref)
        self._put_qname(ref.qualified_name.lstrip(":"), ref)
        self.by_name.setdefault(ref.name, []).append(ref)
        self.by_file_qname[(ref.file_path, ref.qualified_name)] = ref.node_id
        if ref.namespace:
            self._put_qname(f"{ref.namespace}::{ref.name}", ref)
        # Track overloads: base qname -> all variants
        base = self._base_qname(ref.qualified_name)
        if ref.qualified_name != base:
            self._overloads.setdefault(base, []).append(
                (ref.node_id, _rank(ref.kind))
            )

    def resolve(
        self,
        target: str,
        *,
        from_file: str,
        from_namespace: str = "",
        usings: list[str] | None = None,
    ) -> str | None:
        """Resolve a call/await target to a node id, or None if unresolved."""
        usings = usings or []
        t = target.split("<", 1)[0].strip()
        t = t.replace(" ", "")
        if "->" in t or "." in t:
            t = t.replace("->", ".").split(".")[-1]

        candidates: list[str] = []

        def consider(name: str) -> None:
            if name and name not in candidates:
                candidates.append(name)

        consider(t)
        consider(t.lstrip(":"))
        if from_namespace:
            consider(f"{from_namespace}::{t}")
            if "::" not in t:
                consider(f"{from_namespace}::{t}")
            parts = from_namespace.split("::")
            for i in range(len(parts) - 1, 0, -1):
                consider(f"{'::'.join(parts[:i])}::{t}")
                consider(f"{'::'.join(parts[:i])}::{t.split('::')[-1]}")
        for u in usings:
            if u.endswith("::"):
                consider(f"{u}{t.split('::')[-1]}")
            elif "::" not in t:
                consider(f"{u}::{t}")
            else:
                consider(t)

        simple = t.split("::")[-1]
        for c in candidates:
            if c in self.by_qname:
                return self.by_qname[c]

        refs = self.by_name.get(simple) or []
        if not refs:
            return None

        def pick(group: list[SymbolRef]) -> str | None:
            if not group:
                return None
            if len(group) == 1:
                return group[0].node_id
            defs = [r for r in group if _rank(r.kind) >= 2]
            if len(defs) == 1:
                return defs[0].node_id
            decls = [r for r in group if _rank(r.kind) == 1]
            if not defs and len(decls) == 1:
                return decls[0].node_id
            return None

        hit = pick(refs)
        if hit:
            return hit

        same_file = [r for r in refs if r.file_path == from_file]
        hit = pick(same_file)
        if hit:
            return hit

        if from_namespace:
            same_ns = [
                r
                for r in refs
                if r.namespace == from_namespace
                or r.qualified_name.startswith(from_namespace + "::")
            ]
            hit = pick(same_ns)
            if hit:
                return hit

        from_dir = from_file.rsplit("/", 1)[0] if "/" in from_file else ""
        same_dir = [
            r
            for r in refs
            if (r.file_path.rsplit("/", 1)[0] if "/" in r.file_path else "") == from_dir
        ]
        hit = pick(same_dir)
        if hit:
            return hit

        defs = [r for r in refs if _rank(r.kind) >= 2]
        if len(defs) == 1:
            return defs[0].node_id
        return None

    def resolve_overload(
        self,
        target: str,
        *,
        avoid_id: str,
        from_file: str,
        from_namespace: str = "",
        usings: list[str] | None = None,
    ) -> str | None:
        """Resolve target to a different node than avoid_id (self-ref avoidance).

        Used when a static entry co_awaits a same-named member overload (@line).
        """
        primary = self.resolve(
            target,
            from_file=from_file,
            from_namespace=from_namespace,
            usings=usings,
        )
        if primary is None:
            return None

        for qname, nid in self.by_qname.items():
            if nid != primary:
                continue
            base = self._base_qname(qname)
            if base != qname:
                overloads = self._overloads.get(base, [])
            else:
                overloads = self._overloads.get(qname, [])
            if not overloads:
                overloads = self._overloads.get(base, [])

            best = None
            best_rank = -1
            for oid, rank in overloads:
                if oid == avoid_id:
                    continue
                if rank > best_rank:
                    best = oid
                    best_rank = rank
            if best:
                return best
            break

        return None
