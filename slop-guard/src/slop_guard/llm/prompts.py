"""Prompt templates for the semantic detector's local-LLM verification step."""

API_EXISTENCE_PROMPT = """\
You are a strict API verifier. In library `{lib_name}` version `{version}`, \
does the attribute/method `{method_name}` exist?

Answer ONLY with a JSON object:
{{"exists": "YES" or "NO", "confidence": 0.0-1.0, \
"suggestion": "correct alternative method name or empty string"}}
"""

COMMENT_CODE_SIMILARITY_NOTE = """\
Optional semantic mode: instead of token-subset heuristics, a cross-encoder \
(e.g. cross-encoder/ms-marco-MiniLM-L-6-v2 via sentence-transformers) can \
score (comment, code-line) pairs; score > 0.9 means the comment restates the \
code and is noise.
"""
