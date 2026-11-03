"""Module 2a: AST detector (static, fast) built on tree-sitter.

Finds structural AI-slop patterns:
* SG-AST-001 stub functions (TODO + empty/trivial body)
* SG-AST-002 dead imports (never referenced in the same file)
* SG-AST-003 noise comments that restate the adjacent line ("# increment i by 1")
* SG-AST-004 duplicated code blocks (local MinHash clone detector)
"""

from __future__ import annotations

import hashlib
import re
import struct
from pathlib import Path
from typing import Iterable, Optional

import tree_sitter_language_pack as tslp
from tree_sitter import Language, Node, Parser

from ..config import AstConfig
from ..models import (
    Finding,
    RULE_DEAD_IMPORT,
    RULE_DUPLICATED_BLOCK,
    RULE_NOISE_COMMENT,
    RULE_STUB_FUNCTION,
    Severity,
    normalize_code,
    normalize_comment,
)

# ---------------------------------------------------------------------------
# language helpers
# ---------------------------------------------------------------------------

_LANG_CACHE: dict[str, Language] = {}


def get_language(name: str) -> Optional[Language]:
    if name not in _LANG_CACHE:
        try:
            _LANG_CACHE[name] = tslp.get_language(name)
        except Exception:
            return None
    return _LANG_CACHE[name]


def parse(language: str, source: str) -> Optional[tuple[Parser, bytes, Node]]:
    lang = get_language(language)
    if lang is None:
        return None
    parser = Parser(lang)
    src_bytes = source.encode("utf-8")
    tree = parser.parse(src_bytes)
    return parser, src_bytes, tree.root_node


COMMENT_KINDS = {"comment"}
STUB_TODO_RE = re.compile(r"\b(TODO|FIXME|implement(ation)?\s+(later|first)?|not\s+implemented|placeholder)\b", re.I)
TRIVIAL_BODIES = {"pass", "...", "return", "return None", "raise NotImplementedError", "TODO", "todo"}

# Common verbs in "obvious" AI comments; a comment is noise when after removing
# these + filler words its remaining tokens are a subset of the code tokens.
_COMMENT_FILLER = {
    "the", "a", "an", "this", "that", "it", "we", "you", "variable", "value",
    "line", "code", "next", "then", "here", "below", "above", "creates",
    "create", "initializes", "initialize", "defines", "define", "sets", "set",
    "gets", "get", "calls", "call", "returns", "return", "prints", "print",
    "adds", "add", "increments", "increment", "decrements", "decrement",
    "loops", "loop", "iterates", "iterate", "checks", "check", "makes", "make",
    "used", "use", "using", "to", "for", "of", "in", "on", "by", "and", "or",
    "is", "are", "be", "will", "would", "with", "from", "as", "new",
}


class AstDetector:
    name = "ast"

    def __init__(self, config: Optional[AstConfig] = None):
        self.config = config or AstConfig()

    # -- public API ---------------------------------------------------------

    def detect(self, path: Path, language: str, source: str) -> list[Finding]:
        parsed = parse(language, source)
        if parsed is None:
            return []
        _, src_bytes, root = parsed
        findings: list[Finding] = []
        cfg = self.config
        if cfg.stub_threshold >= 0:
            findings += self._stub_functions(root, src_bytes, path)
        if cfg.dead_imports:
            findings += self._dead_imports(root, src_bytes, path, language)
        if cfg.noise_comments:
            findings += self._noise_comments(root, src_bytes, path)
        if cfg.duplicate_blocks:
            findings += self._duplicate_blocks(source, path)
        return findings

    # -- SG-AST-001: stub functions ----------------------------------------

    def _node_text(self, node: Node, src: bytes) -> str:
        return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def _stub_functions(self, root: Node, src: bytes, path: Path) -> list[Finding]:
        out: list[Finding] = []
        for fn in self._walk(root):
            if fn.type not in ("function_definition", "function_declaration",
                               "method_definition", "arrow_function"):
                continue
            body = fn.child_by_field_name("body")
            if body is None:
                body = next((c for c in fn.children if c.type in ("body", "block", "statement_block")), None)
            if body is None:
                continue
            # statements inside the body (comments excluded automatically)
            stmts = [c for c in body.children
                     if c.is_named and c.type != "comment"]
            body_text = self._node_text(body, src)
            trivial_stmts = bool(stmts) and all(
                self._node_text(s, src).strip() in TRIVIAL_BODIES or s.type == "pass_statement"
                for s in stmts
            )
            text_trivial = body_text.strip().rstrip(":").strip("{}").strip() in TRIVIAL_BODIES
            if not (trivial_stmts or text_trivial):
                continue
            # TODO marker: any comment inside the function, preceding comments,
            # or a docstring mentioning it
            has_todo = False
            prev = fn.prev_sibling
            while prev is not None and prev.type == "comment":
                if STUB_TODO_RE.search(self._node_text(prev, src)):
                    has_todo = True
                    break
                prev = prev.prev_sibling
            if not has_todo:
                for c in self._walk(fn):
                    if c.type in ("comment", "string"):
                        if STUB_TODO_RE.search(self._node_text(c, src)):
                            has_todo = True
                            break
            eff_lines = [l for l in body_text.splitlines() if l.strip()]
            short_body = len(eff_lines) <= max(1, self.config.stub_threshold)
            if has_todo and short_body:
                out.append(
                    Finding(
                        rule_id=RULE_STUB_FUNCTION,
                        message=f"Stub function `{self._fn_name(fn, src)}` has no real implementation (TODO placeholder).",
                        file=str(path),
                        line=fn.start_point[0] + 1,
                        column=fn.start_point[1] + 1,
                        end_line=fn.end_point[0] + 1,
                        severity=Severity.WARNING,
                        confidence=0.9,
                        suggested_fix="Implement the function or remove it until needed.",
                        detector=self.name,
                    )
                )
        return out

    def _fn_name(self, fn: Node, src: bytes) -> str:
        name_node = fn.child_by_field_name("name")
        if name_node is not None:
            return self._node_text(name_node, src)
        parent = fn.parent
        if parent is not None and parent.type == "variable_declarator":
            n = parent.child_by_field_name("name")
            if n is not None:
                return self._node_text(n, src)
        return "<anonymous>"

    # -- SG-AST-002: dead imports --------------------------------------------

    def _dead_imports(self, root: Node, src: bytes, path: Path, language: str) -> list[Finding]:
        if language != "python":
            return []
        # (bound_name, node_to_report) — we report the *binding* node so that
        # `from pathlib import Path` flags `Path`, not the word `pathlib`.
        imported: list[tuple[str, Node]] = []
        for stmt in root.children:
            if stmt.type == "import_statement":
                for alias in stmt.children:
                    if alias.type == "dotted_name":
                        # `import x.y` binds the top-level name `x`
                        imported.append((self._node_text(alias, src).split(".")[0], alias))
                    elif alias.type == "aliased_import":
                        name_n = alias.child_by_field_name("name")
                        alias_n = alias.child_by_field_name("alias")
                        full = self._node_text(name_n, src) if name_n else ""
                        bound = (self._node_text(alias_n, src) if alias_n
                                 else full.split(".")[0])
                        imported.append((bound, alias))
            elif stmt.type == "import_from_statement":
                rel = self._node_text(stmt, src)
                if "*" in rel:
                    continue  # star imports bind unknown names; never flag
                module_n = stmt.child_by_field_name("module_name")
                for part in stmt.children:
                    if part.type == "dotted_name" and (
                        module_n is not None
                        and part.start_byte > module_n.end_byte
                    ):
                        imported.append((self._node_text(part, src), part))
                    elif part.type == "aliased_import":
                        alias_n = part.child_by_field_name("alias")
                        name_n = part.child_by_field_name("name")
                        imported.append(
                            (self._node_text(alias_n or name_n, src), part))

        used_names = self._all_identifiers(root, src)
        findings: list[Finding] = []
        reported_nodes: set[int] = set()
        for bound, node in imported:
            key = id(node)
            if key in reported_nodes:
                continue
            if bound in used_names or bound == "__future__":
                continue
            reported_nodes.add(key)
            findings.append(
                Finding(
                    rule_id=RULE_DEAD_IMPORT,
                    message=f"Imported name `{bound}` is never used in this file.",
                    file=str(path),
                    line=node.start_point[0] + 1,
                    column=node.start_point[1] + 1,
                    end_line=node.end_point[0] + 1,
                    severity=Severity.WARNING,
                    confidence=0.95,
                    suggested_fix=f"Remove the unused import `{bound}`.",
                    detector=self.name,
                )
            )
        return findings

    def _all_identifiers(self, root: Node, src: bytes) -> set[str]:
        names: set[str] = set()
        import_nodes = ("import_statement", "import_from_statement")
        for n in self._walk(root):
            if n.type == "identifier":
                # skip identifiers that are part of the import statement itself
                p = n.parent
                in_import = False
                while p is not None:
                    if p.type in import_nodes:
                        in_import = True
                        break
                    p = p.parent
                if not in_import:
                    names.add(src[n.start_byte:n.end_byte].decode())
        return names

    # -- SG-AST-003: noise comments ------------------------------------------

    def _noise_comments(self, root: Node, src: bytes, path: Path) -> list[Finding]:
        findings: list[Finding] = []
        lines = src.decode("utf-8", errors="replace").splitlines()
        for n in self._walk(root):
            if n.type not in COMMENT_KINDS:
                continue
            text = src[n.start_byte:n.end_byte].decode("utf-8", errors="replace")
            body = re.sub(r"^(\s*#|/\*|\*+//)+", "", text).strip()
            if not body or body.startswith(("type:", "noqa", "pylint", "pragma", "nosec")):
                continue
            if len(body) > 120:
                continue  # long explanatory comments are fine
            c_tokens = {t for t in normalize_comment(body).split()} - _COMMENT_FILLER
            if not c_tokens:
                continue
            # compare with the following non-empty code line (typical placement)
            idx = n.end_point[0]
            target = None
            for j in range(idx, min(idx + 2, len(lines))):
                stripped = normalize_code(lines[j]) if j < len(lines) else ""
                if stripped:
                    target = stripped
                    break
            if target is None:
                continue
            k_tokens = {t for t in re.findall(r"[a-z0-9_]+", target.lower())}
            if c_tokens and c_tokens <= k_tokens:
                findings.append(
                    Finding(
                        rule_id=RULE_NOISE_COMMENT,
                        message=f"Comment `{body}` restates the adjacent code (AI noise comment).",
                        file=str(path),
                        line=n.start_point[0] + 1,
                        column=n.start_point[1] + 1,
                        severity=Severity.INFO,
                        confidence=0.75,
                        suggested_fix="Delete the comment or explain *why*, not *what*.",
                        detector=self.name,
                    )
                )
        return findings

    # -- SG-AST-004: duplicated blocks (MinHash) --------------------------------

    def _duplicate_blocks(self, source: str, path: Path) -> list[Finding]:
        cfg = self.config
        # Build windows over *content* lines only (blank lines removed): AI
        # clones are usually separated by blank lines and should still match.
        content: list[tuple[int, str]] = [
            (i, normalize_code(l)) for i, l in enumerate(source.splitlines())
            if normalize_code(l)
        ]
        n = len(content)
        win = cfg.duplicate_min_lines
        if n < win * 2:
            return []
        blocks: dict[int, list[int]] = {}  # signature -> window start indices
        for j in range(n - win + 1):
            chunk = [t for _, t in content[j:j + win]]
            shingles = self._shingles(chunk)
            sig = self._minhash(shingles)
            blocks.setdefault(sig, []).append(j)
        findings: list[Finding] = []
        reported: set[int] = set()
        for sig, starts in blocks.items():
            if len(starts) < 2:
                continue
            first_orig = content[starts[0]][0]
            for s in starts[1:]:
                orig_line = content[s][0]
                # avoid reporting overlapping windows repeatedly
                if any(abs(orig_line - r) < win for r in reported):
                    continue
                reported.add(orig_line)
                snippet = content[s][1][:60]
                findings.append(
                    Finding(
                        rule_id=RULE_DUPLICATED_BLOCK,
                        message=(
                            f"Duplicated block of ≥{win} lines starting at line {orig_line + 1} "
                            f"(identical to block at line {first_orig + 1}): `{snippet}…`"
                        ),
                        file=str(path),
                        line=orig_line + 1,
                        end_line=orig_line + win,
                        severity=Severity.INFO,
                        confidence=0.7,
                        suggested_fix="Extract the repeated logic into a helper function.",
                        detector=self.name,
                    )
                )
        return findings

    @staticmethod
    def _shingles(lines: list[str], k: int = 3) -> set[str]:
        toks = " ".join(lines).split()
        if len(toks) < k:
            return {" ".join(toks)}
        return {" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1)}

    @staticmethod
    def _minhash(shingles: set[str], num_hashes: int = 16) -> int:
        """Compact MinHash signature (packed into one int for exact grouping)."""
        vals = []
        for seed in range(num_hashes):
            best = None
            for sh in shingles:
                h = struct.unpack("<I", hashlib.md5(f"{seed}:{sh}".encode()).digest()[:4])[0]
                if best is None or h < best:
                    best = h
            vals.append(best if best is not None else 0xFFFFFFFF)
        sig = b"".join(struct.pack("<I", v) for v in vals)
        return int.from_bytes(hashlib.sha1(sig).digest()[:8], "big")

    # -- utils -----------------------------------------------------------------

    @staticmethod
    def _walk(node: Node) -> Iterable[Node]:
        stack = [node]
        while stack:
            cur = stack.pop()
            yield cur
            stack.extend(cur.children)
