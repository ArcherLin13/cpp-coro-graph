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


@dataclass
class SymbolIndex:
    by_qname: dict[str, str] = field(default_factory=dict)  # qname -> node_id
    by_name: dict[str, list[SymbolRef]] = field(default_factory=dict)
    by_file_qname: dict[tuple[str, str], str] = field(default_factory=dict)

    def add(self, ref: SymbolRef) -> None:
        self.by_qname[ref.qualified_name] = ref.node_id
        # Also index without leading ::
        q = ref.qualified_name.lstrip(":")
        self.by_qname[q] = ref.node_id
        self.by_name.setdefault(ref.name, []).append(ref)
        self.by_file_qname[(ref.file_path, ref.qualified_name)] = ref.node_id
        if ref.namespace:
            nested = f"{ref.namespace}::{ref.name}"
            self.by_qname.setdefault(nested, ref.node_id)

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
        # member call a->b / a.b → use last segment for lookup
        if "->" in t or "." in t:
            t = t.replace("->", ".").split(".")[-1]

        candidates: list[str] = []

        def consider(name: str) -> None:
            if name and name not in candidates:
                candidates.append(name)

        consider(t)
        consider(t.lstrip(":"))
        if from_namespace:
            consider(f"{from_namespace}::{t}" if "::" not in t else t)
            # walk parent namespaces
            parts = from_namespace.split("::")
            for i in range(len(parts) - 1, 0, -1):
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

        # Unique simple name globally → accept (cross-file)
        refs = self.by_name.get(simple) or []
        if len(refs) == 1:
            return refs[0].node_id

        # Prefer same file
        same_file = [r for r in refs if r.file_path == from_file]
        if len(same_file) == 1:
            return same_file[0].node_id

        # Prefer same namespace
        if from_namespace:
            same_ns = [
                r
                for r in refs
                if r.namespace == from_namespace
                or r.qualified_name.startswith(from_namespace + "::")
            ]
            if len(same_ns) == 1:
                return same_ns[0].node_id

        # Prefer same directory (weak cross-file module hint)
        if refs:
            from_dir = from_file.rsplit("/", 1)[0] if "/" in from_file else ""
            same_dir = [
                r
                for r in refs
                if (r.file_path.rsplit("/", 1)[0] if "/" in r.file_path else "")
                == from_dir
            ]
            if len(same_dir) == 1:
                return same_dir[0].node_id

        return None
