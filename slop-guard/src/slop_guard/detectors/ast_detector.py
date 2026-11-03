"""Module 2a: AST detector (static, fast) built on tree-sitter.

Finds structural AI-slop patterns:
* SG-AST-001 stub functions (TODO + empty/trivial body)
* SG-AST-002 dead imports (never referenced in the same file)
* SG-AST-003 noise comments that restate the adjacent line ("# increment i by 1")
* SG-AST-004 duplicated code blocks (local MinHash *vector* clone detector)

Notes on this revision:
* Markdown is explicitly not analysed here — the router sends ``.md`` files
  only to the provenance detector (license texts/tables are "duplicates").
* The clone detector keeps the full 64-slot minhash vector per window and
  compares pairs by slot agreement (the old single-hash collapse made
  ``duplicate_similarity`` dead code: only exact duplicates matched).
* Dead-import analysis honours ``__all__`` re-exports and string annotations.
"""

from __future__ import annotations

import hashlib
import logging
import re
import struct
from pathlib import Path
from typing import Iterable, Optional

import tree_sitter_python as tspython
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

log = logging.getLogger("slop_guard.ast")

# ---------------------------------------------------------------------------
# language helpers
# ---------------------------------------------------------------------------

_LANG_CACHE: dict[str, Language] = {}


def get_language(name: str) -> Optional[Language]:
    """Return a tree-sitter Language for *name*.

    Python uses the dedicated ``tree-sitter-python`` grammar (the minimal
    default; other grammars can be plugged in via tree-sitter-language-pack
    when installed). Non-Python languages are currently not analysed by this
    detector, so unknown names simply return ``None``.
    """
    if name in _LANG_CACHE:
        return _LANG_CACHE[name]
    lang: Optional[Language] = None
    if name == "python":
        try:
            lang = Language(tspython.language())
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("tree-sitter-python unavailable: %s", exc)
            lang = None
    else:
        try:  # optional extra grammars (only used if language-pack is present)
            import tree_sitter_language_pack as tslp
            lang = tslp.get_language(name)
        except Exception:
            lang = None
    _LANG_CACHE[name] = lang if lang is not None else None
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
    "converts", "convert", "checks", "check", "makes", "make", "adds", "add",
    "increments", "increment", "decrements", "decrement", "loops", "loop",
    "iterates", "iterate", "over", "for", "to", "of", "in", "by", "and", "with",
    "uses", "use", "new", "same", "just", "simply", "first", "each", "all",
}

#: Per-file ignore pragmas: ``# slop-guard: ignore[SG-AST-002]`` or bare
#: ``# slop-guard: ignore`` disables rules for the whole file (dogfooding
#: fixtures/tests need re-exported symbols on purpose).
_PERFILE_IGNORE_RE = re.compile(
    r"#\s*slop-guard:\s*ignore(?:\[(SG-[A-Z]+-\d+(?:,\s*SG-[A-Z]-\d+)*?)\])?",
    re.I,
)


def _perfile_ignored_rules(source: str) -> Optional[set[str]]:
    """None = nothing ignored; empty set = everything ignored."""
    m = _PERFILE_IGNORE_RE.search(source)
    if not m:
        return None
    codes = m.group(1)
    if not codes:
        return set()          # bare ignore -> all AST rules off for this file
    return {c.strip().upper() for c in codes.split(",")}


class AstDetector:
    name = "ast"

    def __init__(self, config: Optional[AstConfig] = None):
        self.config = config or AstConfig()

    # -- public API ---------------------------------------------------------

    def detect(self, path: Path, language: str, source: str) -> list[Finding]:
        if not self.config.enabled or language != "python":
            return []
        parsed = parse(language, source)
        if parsed is None:
            return []
        _, src_bytes, root = parsed

        ignored = _perfile_ignored_rules(source)
        findings: list[Finding] = []
        checks: list[tuple[str, list[Finding]]] = []
        if self.config.dead_imports:
            checks.append((RULE_DEAD_IMPORT, self._dead_imports(root, src_bytes, path, source)))
        if self.config.noise_comments:
            checks.append((RULE_NOISE_COMMENT, self._noise_comments(root, src_bytes, path)))
        if self.config.stub_threshold >= 0:
            checks.append((RULE_STUB_FUNCTION, self._stub_functions(root, src_bytes, path)))
        if self.config.duplicate_blocks:
            checks.append((RULE_DUPLICATED_BLOCK, self._duplicated_blocks(path, source)))
        for rule, fs in checks:
            if ignored is not None and rule in ignored:
                continue
            findings.extend(fs)
        return findings

    # -- SG-AST-002 dead imports ---------------------------------------------

    def _dead_imports(self, root: Node, src: bytes, path: Path, source: str) -> list[Finding]:
        bound: dict[str, Node] = {}
        self._collect_imports(root, src, bound)
        if not bound:
            return []

        used: set[str] = set()
        for node in self._walk(root):
            if node.type == "identifier":
                parent = node.parent
                # skip the binding occurrences themselves
                if parent is not None and parent.type in (
                    "import_statement", "import_from_statement", "aliased_import",
                    "dotted_name",
                ):
                    continue
                used.add(src[node.start_byte:node.end_byte].decode(errors="replace"))

        # Re-export support: names listed in `__all__` (as strings) are used.
        exported = self._dunder_all_names(root, src)
        # String *annotations* count as usage (`def f(x: "Thing")`). We do NOT
        # treat every identifier appearing in any string literal as used —
        # that heuristic silenced dead imports whenever a docstring merely
        # mentioned the name (regression on the bad fixture).
        ann_strings = self._annotation_string_names(root, src)

        findings: list[Finding] = []
        for name, node in sorted(bound.items(), key=lambda kv: kv[1].start_byte):
            if name in used or name in exported or name in ann_strings:
                continue
            findings.append(
                Finding(
                    rule_id=RULE_DEAD_IMPORT,
                    message=f"Import `{name}` is never used in this file.",
                    file=str(path),
                    line=node.start_point[0] + 1,
                    column=node.start_point[1] + 1,
                    end_line=node.end_point[0] + 1,
                    severity=Severity.WARNING,
                    confidence=0.95,
                    suggested_fix=f"Remove the unused import `{name}`.",
                    detector=self.name,
                )
            )
        return findings

    #: AST node types whose *string children* are type annotations.
    _ANNOTATION_HOSTS = {"type", "annotation"}

    @classmethod
    def _annotation_string_names(cls, root: Node, src: bytes) -> set[str]:
        """Identifiers appearing inside string-literal annotations only.

        ``def f(x: "Thing") -> "Other":`` and variable annotations like
        ``items: list["Item"]`` mark those names as used; docstrings and
        arbitrary strings do NOT (they caused false negatives for dead
        imports).
        """
        out: set[str] = set()
        for node in cls._walk(root):
            if node.type not in cls._ANNOTATION_HOSTS:
                continue
            for s in cls._walk(node):
                if s.type == "string":
                    val = src[s.start_byte:s.end_byte].decode(errors="replace")
                    # strip quotes so we don't match the literal prefix itself
                    out.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*",
                                          re.sub(r"^[rbfRBFuU]*['\"]|['\"]$", "", val)))
        return out

    @staticmethod
    def _dunder_all_names(root: Node, src: bytes) -> set[str]:
        out: set[str] = set()
        for node in AstDetector._walk(root):
            if node.type != "expression_statement":
                continue
            child = node.children[0] if node.children else None
            if child is None or child.type != "assignment":
                continue
            left = child.child_by_field_name("left")
            right = child.child_by_field_name("right")
            if left is None or right is None:
                continue
            if src[left.start_byte:left.end_byte].decode(errors="replace") != "__all__":
                continue
            for s in AstDetector._walk(right):
                if s.type == "string":
                    val = src[s.start_byte:s.end_byte].decode(errors="replace")
                    out.add(val.strip("\"'"))
        return out

    @staticmethod
    def _collect_imports(node: Node, src: bytes, out: dict[str, Node]) -> None:
        """Map every top-level *binding* introduced by an import statement.

        Handles ``import a.b`` (binds ``a``), ``import a.b as x`` (binds
        ``x``), ``from m import n [as o]``, parenthesised lists and star
        imports (which disable dead-import reasoning for the file).
        """
        for stmt in node.children:
            t = stmt.type
            if t == "import_statement":
                for child in stmt.children:
                    AstDetector._bind_import_name(child, src, out)
            elif t == "import_from_statement":
                module = stmt.child_by_field_name("module_name")
                for part in stmt.children:
                    if part.type == "wildcard_import":
                        return          # `from x import *` -> cannot reason
                    if module is not None and part.start_byte <= module.end_byte:
                        continue        # skip the dotted module path itself
                    if part.type == "dotted_name":
                        nm = src[part.start_byte:part.end_byte].decode(errors="replace")
                        out.setdefault(nm, part)
                    elif part.type == "aliased_import":
                        alias = part.child_by_field_name("alias")
                        target = alias if alias is not None else \
                            part.child_by_field_name("name")
                        if target is not None:
                            nm = src[target.start_byte:target.end_byte].decode(errors="replace")
                            out.setdefault(nm, part)
                    elif part.type in ("parenthesized_form", "argument_list"):
                        AstDetector._collect_from_parens(part, src, out)
            elif t == "future_import_statement":
                continue  # __future__ imports are consumed by the interpreter

    @staticmethod
    def _collect_from_parens(part: Node, src: bytes, out: dict[str, Node]) -> None:
        for sub in part.children:
            if sub.type == "dotted_name":
                nm = src[sub.start_byte:sub.end_byte].decode(errors="replace")
                out.setdefault(nm, sub)
            elif sub.type == "aliased_import":
                alias = sub.child_by_field_name("alias")
                target = alias if alias is not None else sub.child_by_field_name("name")
                if target is not None:
                    nm = src[target.start_byte:target.end_byte].decode(errors="replace")
                    out.setdefault(nm, sub)

    @staticmethod
    def _bind_import_name(child: Node, src: bytes, out: dict[str, Node]) -> None:
        """For `import x.y [as z]` children of an import_statement."""
        if child.type == "dotted_name":
            first = src[child.start_byte:child.end_byte].decode(errors="replace").split(".")[0]
            out[first] = child
        elif child.type == "aliased_import":
            alias = child.child_by_field_name("alias")
            target = alias if alias is not None else child.child_by_field_name("name")
            if target is not None:
                nm = src[target.start_byte:target.end_byte].decode(errors="replace")
                out[nm] = child

    # -- SG-AST-003 noise comments ---------------------------------------------

    def _noise_comments(self, root: Node, src: bytes, path: Path) -> list[Finding]:
        findings: list[Finding] = []
        text_src = src.decode(errors="replace")
        lines = text_src.splitlines()

        def flag(comment_node: Node, comment_text: str, code_line_idx: int,
                 code_text: str) -> None:
            c_norm = normalize_comment(comment_text)
            if len(c_norm) < 4:
                return
            c_tokens = {t for t in re.split(r"\W+", c_norm.lower()) if t} - _COMMENT_FILLER
            if not c_tokens:
                return
            t_tokens = set(re.split(r"\W+", normalize_code(code_text).lower())) - _COMMENT_FILLER
            if c_tokens and c_tokens <= t_tokens:
                findings.append(
                    Finding(
                        rule_id=RULE_NOISE_COMMENT,
                        message=(f"Comment merely restates the code: \"{c_norm}\". "
                                 "Explain *why*, not *what*."),
                        file=str(path),
                        line=comment_node.start_point[0] + 1,
                        column=comment_node.start_point[1] + 1,
                        severity=Severity.INFO,
                        confidence=0.75,
                        suggested_fix="Delete the comment or document intent/edge cases instead.",
                        detector=self.name,
                    )
                )

        # (a) standalone comments — compare against next non-blank line, else prev
        for node in self._walk(root):
            if node.type not in COMMENT_KINDS:
                continue
            text = src[node.start_byte:node.end_byte].decode(errors="replace")
            target_idx = None
            for candidate in (node.end_point[0] + 1, node.start_point[0] - 1):
                if 0 <= candidate < len(lines):
                    norm = normalize_code(lines[candidate])
                    if norm and "#" not in norm:   # not a trailing-comment line
                        target_idx = candidate
                        break
            if target_idx is None:
                continue
            flag(node, text, target_idx, lines[target_idx])

        # (b) trailing comments: `i += 1  # increment i by 1` — tree-sitter
        # attaches these inside the statement; compare with the SAME line's
        # code portion only (previous regression: same-line pairs were missed).
        for node in self._walk(root):
            if node.type != "comment":
                continue
            row = node.start_point[0]
            raw = lines[row] if 0 <= row < len(lines) else ""
            before = raw[:node.start_point[1]]
            after = raw[node.end_point[1]:]
            if before.strip() and not after.strip():     # genuine trailing comment
                flag(node, src[node.start_byte:node.end_byte].decode(errors="replace"),
                     row, before)
        return findings

    # -- SG-AST-001 stub functions ----------------------------------------------

    def _stub_functions(self, root: Node, src: bytes, path: Path) -> list[Finding]:
        findings: list[Finding] = []
        for node in self._walk(root):
            if node.type not in ("function_definition",):
                continue
            body = node.child_by_field_name("body")
            if body is None:
                continue
            body_text = src[body.start_byte:body.end_byte].decode(errors="replace").strip()
            effective = [ln for ln in body_text.splitlines()
                         if normalize_code(ln) and not normalize_comment(ln)]
            trivial = all(normalize_code(ln).rstrip(":").strip().lower() in TRIVIAL_BODIES
                          or not normalize_code(ln)
                          for ln in body_text.splitlines())
            if not trivial or len(effective) > self.config.stub_threshold:
                continue
            # look for TODO-ish marker in body or the preceding comment/docstring
            doc = self._leading_context(node, src)
            if not STUB_TODO_RE.search(body_text) and not STUB_TODO_RE.search(doc):
                continue
            fn_name_node = node.child_by_field_name("name")
            fn_name = src[fn_name_node.start_byte:fn_name_node.end_byte].decode() if fn_name_node else "?"
            findings.append(
                Finding(
                    rule_id=RULE_STUB_FUNCTION,
                    message=f"`{fn_name}` looks like an unimplemented stub (TODO marker + trivial body).",
                    file=str(path),
                    line=node.start_point[0] + 1,
                    column=node.start_point[1] + 1,
                    end_line=node.end_point[0] + 1,
                    severity=Severity.WARNING,
                    confidence=0.9,
                    suggested_fix=f"Implement `{fn_name}` or raise NotImplementedError explicitly with a tracking issue.",
                    detector=self.name,
                )
            )
        return findings

    @staticmethod
    def _leading_context(node: Node, src: bytes) -> str:
        """Comments directly above a definition (heuristic for TODO placement)."""
        lines = src.decode(errors="replace").splitlines()
        start = node.start_point[0] - 1
        ctx: list[str] = []
        while start >= 0:
            ln = lines[start].strip()
            if ln.startswith("#"):
                ctx.insert(0, ln)
                start -= 1
            elif not ln:
                if ctx:
                    break
                start -= 1
            else:
                break
        return "\n".join(ctx)

    # -- SG-AST-004 duplicated blocks (near-clone detection) ---------------------

    #: number of minhash slots; 16 slots give ±0.075 error which cannot
    #: distinguish a 0.9 threshold — 64 slots reduce it to ~±0.04.
    NUM_HASHES = 64
    #: pairwise comparison cap before we bail to bucketing-only mode
    MAX_WINDOWS_FOR_PAIRWISE = 2000

    def _duplicated_blocks(self, path: Path, source: str) -> list[Finding]:
        win = self.config.duplicate_min_lines
        threshold = max(0.0, min(1.0, self.config.duplicate_similarity))
        raw_lines = source.splitlines()
        content: list[tuple[int, str]] = []  # (orig index, normalized line)
        for idx, ln in enumerate(raw_lines):
            norm = normalize_code(ln)
            if norm and not normalize_comment(ln):
                content.append((idx, norm))
        if len(content) < win * 2:
            return []

        # sliding windows over *normalized* lines.
        # step=1 (was win//2): with a stride, two aligned clones starting at
        # offsets not divisible by the stride never produce comparable windows;
        # unit stride guarantees every clone pair shares an aligned window.
        windows: list[tuple[int, tuple[str, ...], tuple[int, ...]]] = []
        sigs: list[tuple[int, ...]] = []
        for s in range(0, len(content) - win + 1):
            chunk = content[s:s + win]
            key = tuple(c for _, c in chunk)
            if len(set(key)) < max(3, win // 2):   # too repetitive (blank-ish block)
                continue
            windows.append((chunk[0][0], key, tuple(ci for ci, _ in chunk)))
            sigs.append(self._minhash_vector(self._shingles(list(key))))

        findings: list[Finding] = []
        reported: set[int] = set()
        n = len(windows)
        if n > self.MAX_WINDOWS_FOR_PAIRWISE:
            log.info("duplicate scan: %d windows > cap %d — grouping by exact "
                     "signature only for this large file", n, self.MAX_WINDOWS_FOR_PAIRWISE)
        # group candidate pairs cheaply: identical shingle-key first (exact dupes),
        # then near-dupes via slot agreement within LSH-style bands.
        seen_pairs: set[tuple[int, int]] = set()
        for i in range(n):
            for j in range(i + 1, n):
                if windows[i][2][0] == windows[j][2][0]:
                    continue
                if abs(windows[i][0] - windows[j][0]) < win:
                    continue  # overlapping windows
                if windows[i][1] == windows[j][1]:
                    sim = 1.0
                elif n <= self.MAX_WINDOWS_FOR_PAIRWISE:
                    agree = sum(1 for a, b in zip(sigs[i], sigs[j]) if a == b)
                    sim = agree / self.NUM_HASHES
                else:
                    continue
                if sim < threshold:
                    continue
                pair = (i, j)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                orig_line, (first_orig) = windows[j][0], windows[i][0]
                # avoid reporting overlapping windows repeatedly
                if any(abs(orig_line - r) < win for r in reported):
                    continue
                reported.add(orig_line)
                snippet = windows[j][1][0][:60]
                findings.append(
                    Finding(
                        rule_id=RULE_DUPLICATED_BLOCK,
                        message=(
                            f"Duplicated block of ≥{win} lines starting at line {orig_line + 1} "
                            f"(~{sim:.0%} similar to block at line {first_orig + 1}): `{snippet}…`"
                        ),
                        file=str(path),
                        line=orig_line + 1,
                        end_line=orig_line + win,
                        severity=Severity.INFO,
                        confidence=min(0.95, 0.5 + sim / 2),
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

    @classmethod
    def _minhash_vector(cls, shingles: set[str],
                        num_hashes: Optional[int] = None) -> tuple[int, ...]:
        """Full MinHash signature (one min-hash per permutation slot).

        blake2b keyed hashing per seed; returns the vector itself — comparing
        two vectors slot-wise estimates Jaccard similarity (≈ agreement rate).
        """
        nh = num_hashes or cls.NUM_HASHES
        vals: list[int] = []
        sh_list = list(shingles)
        for seed in range(nh):
            best = 0xFFFFFFFF
            sb = struct.pack("<I", seed)
            for sh in sh_list:
                h = int.from_bytes(
                    hashlib.blake2b(sh.encode(), key=sb, digest_size=4).digest(),
                    "big")
                if h < best:
                    best = h
            vals.append(best)
        return tuple(vals)

    # -- utils -----------------------------------------------------------------

    @staticmethod
    def _walk(node: Node) -> Iterable[Node]:
        stack = [node]
        while stack:
            cur = stack.pop()
            yield cur
            stack.extend(cur.children)
