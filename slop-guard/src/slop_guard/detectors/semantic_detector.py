"""Module 2b: Semantic detector — API hallucination checks.

For every attribute access like ``df.to_parquet_s3(...)`` where the object's
origin can be traced to an imported library (e.g. ``import pandas``), the
detector asks an :class:`ApiExistenceChecker` (offline snapshot and/or local
LLM via ollama) whether the member really exists. If several checkers agree,
their confidences are combined (consensus scoring).

Design note (spec §5 step 3): a ±5-line context window is attached to each
suspicious call for the LLM prompt; in offline mode the snapshot client is
deterministic and needs no context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from tree_sitter import Node

from ..config import SemanticConfig
from ..llm.ollama_client import ApiExistenceChecker, ExistenceAnswer
from ..models import Finding, RULE_HALLUCINATED_API, Severity
from .ast_detector import parse

# stdlib / names we never flag
_IGNORED_ROOTS = {
    "self", "cls", "super", "os", "sys", "json", "re", "math", "time",
    "datetime", "collections", "logging", "pathlib", "typing", "itertools",
    "functools", "abc", "enum", "io", "copy", "random", "string", "subprocess",
}


class SemanticDetector:
    name = "semantic"

    def __init__(self, config: Optional[SemanticConfig] = None,
                 checkers: Optional[list[ApiExistenceChecker]] = None):
        self.config = config or SemanticConfig()
        self.checkers: list[ApiExistenceChecker] = checkers or []

    # -- public API ---------------------------------------------------------

    def detect(self, path: Path, language: str, source: str) -> list[Finding]:
        if not self.checkers or language != "python":
            return []
        parsed = parse(language, source)
        if parsed is None:
            return []
        _, src_bytes, root = parsed
        imports = self._import_map(root, src_bytes)
        var_types = self._variable_type_map(root, src_bytes, imports)
        # combined namespace: import bindings + inferred instance variables
        namesp: dict[str, str] = {**imports, **var_types}
        if not namesp:
            return []
        lines = source.splitlines()
        findings: list[Finding] = []
        seen: set[tuple[int, str]] = set()
        for node in self._walk(root):
            if node.type != "attribute":
                continue
            chain = self._attr_chain(node, src_bytes)
            if len(chain) < 2:
                continue
            head = chain[0]
            lib = namesp.get(head)
            if lib is None or head in _IGNORED_ROOTS:
                continue
            member = chain[-1]
            # For `df.to_parquet_s3` (head inferred as pandas.DataFrame via
            # read_csv) the qualified name is the class itself; for direct
            # module access like `pd.read_csv` it stays package-level.
            qualified = ".".join(chain[1:-1]) or lib
            key = (node.start_byte, f"{qualified}.{member}")
            if key in seen:
                continue
            seen.add(key)
            answer = self._consensus(lib, qualified, member)
            if answer.exists is False and answer.confidence >= self.config.confidence_threshold:
                line = node.start_point[0]
                ctx = "\n".join(lines[max(0, line - 5):line + 6])
                msg = (f"`{qualified}.{member}` does not exist in {lib} "
                       "(possible API hallucination).")
                prefix = member[: len(member) - len(answer.suggestion)] if answer.suggestion and member.endswith(answer.suggestion) else ""
                fix = (f"Replace with `{prefix}{answer.suggestion}`."
                       if answer.suggestion else "Verify the API against the library documentation.")
                findings.append(
                    Finding(
                        rule_id=RULE_HALLUCINATED_API,
                        message=msg,
                        file=str(path),
                        line=node.start_point[0] + 1,
                        column=node.start_point[1] + 1,
                        end_line=node.end_point[0] + 1,
                        severity=Severity.ERROR,
                        confidence=min(0.99, answer.confidence),
                        suggested_fix=fix + f"\nContext:\n{ctx}",
                        detector=self.name,
                    )
                )
        return findings

    # -- internals ------------------------------------------------------------

    def _consensus(self, pkg: str, qualified: str, member: str) -> ExistenceAnswer:
        # Try the most specific class first (e.g. DataFrame), then fall back to
        # package-level resolution so that variables holding instances
        # (`df = pd.read_csv(...)` -> `df.to_parquet_s3`) are still checked.
        candidates: list[str] = []
        if qualified and qualified != pkg:
            candidates.append(qualified.split(".")[0])   # e.g. 'DataFrame' via lib='pandas.DataFrame'
        candidates.append(pkg)                            # plain package lookup ('_any')
        answers: list[ExistenceAnswer] = []
        for ch in self.checkers:
            ans: Optional[ExistenceAnswer] = None
            for cand in candidates:
                library = f"{pkg}.{cand}" if cand != pkg else pkg
                try:
                    ans = ch.check(library, member)
                except Exception:
                    continue
                if ans.exists is not None:
                    break
            if ans is not None and ans.exists is not None:
                answers.append(ans)
        if not answers:
            return ExistenceAnswer(None, 0.0)
        neg = [a for a in answers if a.exists is False]
        pos = [a for a in answers if a.exists is True]
        if pos and not neg:
            return ExistenceAnswer(True, max(a.confidence for a in pos))
        if neg and not pos:
            best = max(neg, key=lambda a: a.confidence)
            return ExistenceAnswer(False, best.confidence, best.suggestion, "+".join({a.source for a in neg}))
        # disagreement: keep highest-confidence negative but damp it
        best = max(answers, key=lambda a: a.confidence)
        return ExistenceAnswer(best.exists, best.confidence * 0.6, best.suggestion, "disagreement")

    # factory function -> class it returns (subset of common AI-slop targets)
    _FACTORY_CLASSES: dict[str, str] = {
        "read_csv": "DataFrame", "read_parquet": "DataFrame",
        "read_json": "DataFrame", "read_excel": "DataFrame",
        "concat": "DataFrame", "merge": "DataFrame",
        "date_range": "DatetimeIndex", "to_datetime": "Series",
        "tensor": "Tensor", "zeros": "Tensor", "ones": "Tensor",
        "randn": "Tensor",
    }

    @classmethod
    def _variable_type_map(cls, root: Node, src: bytes,
                           imports: dict[str, str]) -> dict[str, str]:
        """Very small dataflow heuristic: ``df = pd.read_csv(...)`` binds the
        *type path* ``pandas.DataFrame`` to variable ``df``.

        This lets the detector check instance-method hallucinations on
        variables (the most common AI pattern), not only direct module access.
        """
        out: dict[str, str] = {}
        for node in cls._walk(root):
            if node.type != "assignment":
                continue
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is None or right is None or left.type != "identifier":
                continue
            if right.type != "call":
                continue
            fn = right.child_by_field_name("function")
            if fn is None or fn.type != "attribute":
                continue
            chain = cls._attr_chain(fn, src)
            if len(chain) < 2:
                continue
            pkg = imports.get(chain[0])
            if pkg is None:
                continue
            callee = chain[-1]
            klass = cls._FACTORY_CLASSES.get(callee)
            if klass:
                var = src[left.start_byte:left.end_byte].decode()
                out[var] = f"{pkg}.{klass}"
        return out

    @staticmethod
    def _import_map(root: Node, src: bytes) -> dict[str, str]:
        """bound name -> top-level package, e.g. {'F': 'torch', 'pd': 'pandas'}."""
        out: dict[str, str] = {}
        for stmt in root.children:
            t = stmt.type
            if t == "import_statement":
                for alias in stmt.children:
                    text = src[alias.start_byte:alias.end_byte].decode(errors="replace")
                    if alias.type == "dotted_name":
                        out[text.split(".")[0]] = text.split(".")[0]
                    elif alias.type == "aliased_import":
                        n = alias.child_by_field_name("name")
                        a = alias.child_by_field_name("alias")
                        bound = src[a.start_byte:a.end_byte].decode() if a else text
                        pkg = src[n.start_byte:n.end_byte].decode().split(".")[0] if n else text.split(".")[0]
                        out[bound] = pkg
            elif t == "import_from_statement":
                mod = stmt.child_by_field_name("module_name")
                if mod is None:
                    continue
                module = src[mod.start_byte:mod.end_byte].decode()
                top = module.split(".")[0]
                for part in stmt.children:
                    if part.type == "dotted_name" and (
                        mod is not None and part.start_byte > mod.end_byte
                    ):
                        name = src[part.start_byte:part.end_byte].decode()
                        out[name] = top
                    elif part.type == "aliased_import":
                        a = part.child_by_field_name("alias")
                        n = part.child_by_field_name("name")
                        bound = src[a.start_byte:a.end_byte].decode() if a else src[n.start_byte:n.end_byte].decode()
                        out[bound] = top
        return out

    @staticmethod
    def _attr_chain(node: Node, src: bytes) -> list[str]:
        """Flatten a.b.c into ['a','b','c'] starting from the outermost attr."""
        parts: list[str] = []
        cur: Optional[Node] = node
        while cur is not None and cur.type == "attribute":
            attr = cur.child_by_field_name("attribute")
            parts.append(src[attr.start_byte:attr.end_byte].decode() if attr else "?")
            cur = cur.child_by_field_name("object")
        if cur is not None and cur.type == "identifier":
            parts.append(src[cur.start_byte:cur.end_byte].decode())
        return list(reversed(parts))

    @staticmethod
    def _walk(node: Node):
        stack = [node]
        while stack:
            cur = stack.pop()
            yield cur
            stack.extend(cur.children)
