"""slop-guard: CI/CD detector for AI hallucinations and "AI slop".

Static analyzer that works not with syntax but with *semantics and
provenance* of code/text, flagging characteristic AI-generation patterns:
hallucinated APIs, dead imports, obvious-noise comments, template stubs
and phantom sources.
"""

__version__ = "0.1.0"
