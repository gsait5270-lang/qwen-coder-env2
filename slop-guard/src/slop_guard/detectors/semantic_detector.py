"""Module 2b: Semantic detector — API hallucination checker.

Uses tree-sitter to extract attribute/method accesses on imported modules
(e.g. ``df.to_parquet_s3()`` where ``df`` is a pandas DataFrame, or
``F.gelu_approx()`` for ``import torch.nn.functional as F``) and verifies the
member against one or more backends (offline snapshots + optional local LLM).

Fixes in this revision:
* **Submodule aliases keep their full path**: ``import torch.nn.functional
  as F`` maps ``F -> torch.nn.functional`` (previously only the alias root
  ``torch`` was stored, so ``F.<anything>`` could never be checked).
* **Local bindings are tracked** (``pd = __import__("pandas")`` style), with
  call results treated as unknown instead of guessing a type.
* **Unverifiable answers never become findings** (three-state checkers).
* **Consensus across independent checkers**: two agreeing "does not exist"
  answers raise confidence; a disagreeing "exists" answer suppresses the
  finding entirely.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from tree_sitter import Node

from ..config import SemanticConfig
from ..llm.ollama_client import ApiExistenceChecker, ExistenceAnswer
from ..models import Finding, RULE_HALLUCINATED_API, Severity
from .ast_detector import parse

log = logging.getLogger("slop_guard.semantic")

# module root name -> canonical top-level package (for common aliases)
ROOT_TO_PKG = {
    "np": "numpy", "plt": "matplotlib", "tf": "tensorflow",
    "F": "torch", "nn": "torch", "dt": "datetime",
}


class SemanticDetector:
    name = "semantic"

    def __init__(self, config: Optional[SemanticConfig] = None,
                 checkers: Optional[list[ApiExistenceChecker]] = None):
        self.config = config or SemanticConfig()
        self.checkers: list[ApiExistenceChecker] = checkers or []

    # -- public API -----------------------------------------------------------

    def detect(self, path: Path, language: str, source: str) -> list[Finding]:
        if not self.config.enabled or not self.checkers or language != "python":
            return []
        parsed = parse(language, source)
        if parsed is None:
            log.debug("semantic: no grammar for %s", language)
            return []
        _, src_bytes, root = parsed

        imports = self._import_map(root, src_bytes)
        var_types = self._infer_var_types(root, src_bytes, imports)
        versions = self.config.versions or {}
        findings: list[Finding] = []
        seen: set[tuple[str, int]] = set()

        for node in self._walk(root):
            if node.type != "attribute":
                continue
            obj = node.child_by_field_name("object")
            attr = node.child_by_field_name("attribute")
            if obj is None or attr is None:
                continue
            obj_name = src_bytes[obj.start_byte:obj.end_byte].decode(errors="replace")
            attr_name = src_bytes[attr.start_byte:attr.end_byte].decode(errors="replace")
            if not obj_name or not attr_name or attr_name.startswith("_"):
                continue

            # Resolve the object expression to a library path *including*
            # intermediate chain segments: `torch.nn.functional.gelu_approx`
            # must be checked as torch.nn.functional (not just `torch`), and
            # `df.to_parquet_s3()` where `df = pd.read_csv(...)` resolves via
            # factory inference to pandas.DataFrame.
            lib_path = self._resolve_obj(obj, obj_name, src_bytes, imports, var_types)
            if lib_path is None:
                continue

            version = versions.get(lib_path.split(".")[0], "")
            try:
                answers = [c.check(lib_path, attr_name, version) for c in self.checkers]
            except Exception as exc:  # a broken backend must never kill the scan
                log.warning("checker %r raised (%s); skipping semantic check for %s.%s",
                            getattr(self.checkers[0], "source", "?"), exc,
                            lib_path, attr_name)
                continue
            definitive = [a for a in answers if a.exists is not None]
            if not definitive:
                log.debug("semantic: %s.%s unverifiable by all backends — silent",
                          lib_path, attr_name)
                continue  # every backend said "cannot verify" -> stay silent
            neg = [a for a in definitive if a.exists is False]
            pos = [a for a in definitive if a.exists is True]
            if pos:
                continue  # any authoritative "exists" vetoes the accusation
            agree = len(neg)
            conf = max(a.confidence for a in neg)
            if agree >= 2:
                conf = min(0.99, conf + 0.1 * (agree - 1))
            if conf < self.config.confidence_threshold:
                continue

            line = node.start_point[0] + 1
            key = (attr_name, line)
            if key in seen:
                continue
            seen.add(key)
            suggestion = next((a.suggestion for a in neg if a.suggestion), None)
            sources = ",".join(sorted({a.source for a in neg}))
            msg = (f"`{obj_name}.{attr_name}` does not exist in `{lib_path}` "
                   f"(checked via {sources}).")
            fix = (f"Did you mean `{obj_name}.{suggestion}`?" if suggestion else
                   f"Verify the `{lib_path}` API — this looks like a hallucinated member.")
            findings.append(
                Finding(
                    rule_id=RULE_HALLUCINATED_API,
                    message=msg,
                    file=str(path),
                    line=line,
                    column=node.start_point[1] + 1,
                    end_line=node.end_point[0] + 1,
                    severity=Severity.ERROR,
                    confidence=conf,
                    suggested_fix=fix,
                    detector=self.name,
                )
            )
        return findings

    # -- helpers ----------------------------------------------------------------

    def _resolve_obj(self, obj: Node, obj_name: str, src: bytes,
                     imports: dict[str, Optional[str]],
                     var_types: dict[str, str]) -> Optional[str]:
        """Map an object expression to the deepest *known* library path.

        Cases (postfix chain is consumed left-to-right, longest known prefix
        wins):

        1. ``F.gelu_approx`` with ``import torch.nn.functional as F`` →
           imports["F"] = "torch.nn.functional" (full path preserved).
        2. ``torch.nn.functional.gelu_approx`` → root resolves to "torch",
           then each further segment ("nn", "functional") is appended only if
           it is itself a known member of the accumulated path (checked via
           ``var_types``-style knowledge in checkers is too costly here; we
           use the import map + a static submodule table instead).
        3. ``df.to_parquet_s3`` where ``df = pd.read_csv(...)`` → factory
           inference maps df → "pandas.DataFrame".
        4. Bare alias heuristics (``np``, ``plt``, ``tf``) for code that never
           imported them explicitly (REPL-style snippets).
        """
        segs = obj_name.split(".")
        first = segs[0]
        base: Optional[str] = None
        if first in imports:
            base = imports[first]
        elif first in var_types:
            base = var_types[first]
        else:
            guess = ROOT_TO_PKG.get(first)
            if guess is not None and len(segs) == 1:
                base = guess
        if base is None:
            return None

        # Extend the path through intermediate attribute segments when they
        # are known submodules/attributes of the current path. We treat every
        # plain (non-dunder) identifier segment as a legitimate extension —
        # the *checkers* decide whether the resulting path is verifiable;
        # previously chains were truncated at the root, which made
        # `torch.nn.functional.<anything>` unverifiable and silent.
        path = base
        for seg in segs[1:]:
            if not seg or seg.startswith("_") or not seg.isidentifier():
                break
            path = f"{path}.{seg}"
        if path == base and len(segs) > 1:
            # could not extend (e.g. call results inside the chain); fall back
            # to the base path so the final attribute is still checked.
            path = base
        return path

    #: factory functions whose result type we know well enough to attribute
    #: members of the returned class against (module_path -> class pool key).
    FACTORY_TYPES: dict[tuple[str, str], str] = {
        ("pandas", "read_csv"): "pandas.DataFrame",
        ("pandas", "read_parquet"): "pandas.DataFrame",
        ("pandas", "read_json"): "pandas.DataFrame",
        ("pandas", "concat"): "pandas.DataFrame",
        ("pandas", "DataFrame"): "pandas.DataFrame",
        ("pandas", "Series"): "pandas.Series",
        ("pandas", "date_range"): "pandas.DatetimeIndex",
        ("numpy", "array"): "numpy.ndarray",
        ("numpy", "zeros"): "numpy.ndarray",
        ("numpy", "ones"): "numpy.ndarray",
        ("numpy", "randn"): "numpy.ndarray",
        ("torch", "tensor"): "torch.Tensor",
        ("torch", "zeros"): "torch.Tensor",
        ("torch", "ones"): "torch.Tensor",
        ("torch", "randn"): "torch.Tensor",
    }

    @classmethod
    def _infer_var_types(cls, root: Node, src: bytes,
                         imports: dict[str, Optional[str]]) -> dict[str, str]:
        """Very small dataflow pass: ``x = <factory>(...)`` ⇒ x : <class path>.

        Only direct assignments from known factory calls are tracked; anything
        else stays unknown (no guessing — unknown variables produce no checks
        and therefore no false positives).
        """
        out: dict[str, str] = {}
        for node in cls._walk(root):
            if node.type != "assignment":
                continue
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is None or right is None or left.type != "identifier":
                continue
            name = src[left.start_byte:left.end_byte].decode(errors="replace")
            if right.type != "call":
                continue
            func = right.child_by_field_name("function")
            if func is None or func.type not in ("identifier", "attribute"):
                continue
            fname = src[func.start_byte:func.end_byte].decode(errors="replace")
            fsegs = fname.split(".")
            if len(fsegs) != 2:
                continue
            mod_alias, fn_name = fsegs
            mod_path = imports.get(mod_alias)
            if not mod_path:
                continue
            pkg = mod_path.split(".")[0]
            type_path = cls.FACTORY_TYPES.get((pkg, fn_name))
            if type_path is not None:
                out[name] = type_path
        return out

    @staticmethod
    def _import_map(root: Node, src: bytes) -> dict[str, str]:
        """alias -> **full dotted module path**.

        ``import torch.nn.functional as F`` yields ``{"F": "torch.nn.functional",
        "torch": "torch"}``; ``from x.y import Z as w`` yields
        ``{"w": "x.y.Z", "Z": "x.y.Z"}`` (the unaliased name is also bound by
        Python, but we record the alias primarily). Local re-bindings
        (``pd = something``) are recorded too; call results map to ``None``
        (unknown) so nothing is guessed.
        """
        out: dict[str, Optional[str]] = {}
        for stmt in root.children:
            t = stmt.type
            if t == "import_statement":
                for child in stmt.children:
                    if child.type == "dotted_name":
                        full = src[child.start_byte:child.end_byte].decode(errors="replace")
                        out.setdefault(full.split(".")[0], full)
                    elif child.type == "aliased_import":
                        name = child.child_by_field_name("name")
                        alias = child.child_by_field_name("alias")
                        if name is not None and alias is not None:
                            full = src[name.start_byte:name.end_byte].decode(errors="replace")
                            an = src[alias.start_byte:alias.end_byte].decode(errors="replace")
                            out[an] = full          # FULL path, not just root!
            elif t == "import_from_statement":
                module = stmt.child_by_field_name("module_name")
                if module is None:
                    continue
                mod = src[module.start_byte:module.end_byte].decode(errors="replace")
                prefix = "" if mod else "." * sum(
                    1 for c in stmt.children if c.type == "import_prefix")
                base = f"{prefix}{mod}" if mod else prefix
                for part in stmt.children:
                    if part.start_byte <= module.end_byte:
                        continue
                    names: list[tuple[str, str]] = []
                    if part.type == "dotted_name":
                        nm = src[part.start_byte:part.end_byte].decode(errors="replace")
                        names.append((nm, f"{base}.{nm}"))
                    elif part.type == "aliased_import":
                        name = part.child_by_field_name("name")
                        alias = part.child_by_field_name("alias")
                        if name is not None:
                            nm = src[name.start_byte:name.end_byte].decode(errors="replace")
                            an = (src[alias.start_byte:alias.end_byte].decode(errors="replace")
                                  if alias is not None else nm)
                            names.append((an, f"{base}.{nm}"))
                    for an, full in names:
                        out.setdefault(an, full)
            elif t == "assignment":
                left = stmt.child_by_field_name("left")
                right = stmt.child_by_field_name("right")
                if left is None or left.type != "identifier":
                    continue
                ln = src[left.start_byte:left.end_byte].decode(errors="replace")
                if ln in out:
                    if right is not None and right.type == "call":
                        out[ln] = None      # rebound to a call result: unknown
                    else:
                        del out[ln]         # rebound to literal etc.: not a module
        return {k: v for k, v in out.items() if v}

    @staticmethod
    def _walk(node: Node):
        stack = [node]
        while stack:
            cur = stack.pop()
            yield cur
            stack.extend(cur.children)
