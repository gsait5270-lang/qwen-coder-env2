"""Offline API-snapshot generator (ships inside the package).

Builds the JSON snapshot format consumed by
:class:`~slop_guard.llm.ollama_client.OfflineSnapshotClient` from an
*installed* package, using ``dir()``/``inspect`` on the live interpreter::

    {"package": ..., "version": ..., "source": "generated ...",
     "members": {"_any": [...], "<Class>": [...], "<sub.module>": [...]}}

Generating from the installed version closes two long-standing gaps:

* ``_any`` used to be a hand-written *subset*, so absence from it was weak
  evidence and top-level typos (``pd.read_csvv``) went unflagged; ``dir()``
  makes the top level exhaustive, so absence becomes meaningful.
* snapshots are version-stamped (``pandas@2.2.3.json``), which the snapshot
  client's version cascade already understands.

CLI: ``slop-guard snapshot pandas==2.2.3 [--out DIR] [--max-members N]``.
Kept importable as ``python -m slop_guard.snapshot_gen`` too; the historical
entry point ``tools/gen_snapshot.py`` is a thin wrapper around this module.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path


def _pip_install_if_needed(spec: str) -> str:
    """Return canonical ``pkg==ver``; pip-install the spec if not importable.

    Only used by the standalone ``python -m`` entry point — the in-process
    CLI command never installs packages implicitly.
    """
    pkg = re.split(r"[=<>!~]", spec, maxsplit=1)[0].strip()
    try:
        mod = importlib.import_module(pkg)
        ver = getattr(mod, "__version__", "")
        if ver and f"=={ver}" in spec.replace(" ", ""):
            return f"{pkg}=={ver}"
        if not ver:
            return pkg
        if f"=={ver}" != spec.replace(" ", "") and "==" in spec:
            print(f"note: installed {pkg}=={ver} differs from {spec}", file=sys.stderr)
        return f"{pkg}=={ver}"
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", spec])
        mod = importlib.import_module(pkg)
        return f"{pkg}=={getattr(mod, '__version__', '')}"


def _public_dir(obj) -> list[str]:
    return sorted(n for n in dir(obj) if not n.startswith("_"))


def _class_members(cls) -> list[str]:
    """Public members of *cls* including inherited ones (MRO), minus builtins."""
    out: set[str] = set()
    try:
        for mro_cls in inspect.getmro(cls):
            if getattr(mro_cls, "__module__", "").split(".")[0] != "builtins":
                out.update(_public_dir(mro_cls))
    except Exception:
        pass
    return sorted(out)


def generate_snapshot(spec: str, out_dir: Path, max_members: int = 400,
                      submodules: list[str] | None = None) -> Path:
    """Snapshot one installed package; returns the written JSON path.

    ``spec`` is ``pkg`` or ``pkg==version`` (the pinned form selects the
    version-stamped filename ``pkg@version.json``). ``submodules`` adds deep
    dotted paths that plain ``dir(package)`` does not expose
    (e.g. ``torch.nn.functional``).
    """
    pkg, _, version = spec.partition("==")
    mod = importlib.import_module(pkg)
    members: dict[str, list[str]] = {}

    top = _public_dir(mod)
    members["_any"] = top[:max_members]

    for name in top:
        try:
            obj = getattr(mod, name)
        except AttributeError:
            continue
        if inspect.isclass(obj) and getattr(obj, "__module__", "").startswith(pkg):
            members[name] = _class_members(obj)[:max_members]
        elif inspect.ismodule(obj) and obj.__name__.startswith(pkg + "."):
            members[obj.__name__[len(pkg) + 1:]] = _public_dir(obj)[:max_members]

    for sm in submodules or []:
        try:
            sub = importlib.import_module(sm)
            key = sm[len(pkg) + 1:] if sm.startswith(pkg + ".") else sm
            members[key] = _public_dir(sub)[:max_members]
        except Exception as exc:
            print(f"warning: cannot snapshot submodule {sm}: {exc}", file=sys.stderr)

    payload = {
        "package": pkg,
        "version": version or getattr(mod, "__version__", ""),
        "source": f"generated from {spec} public API via dir()/inspect",
        "members": members,
    }
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{pkg}@{version}.json" if version else f"{pkg}.json"
    target = out_dir / fname
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate a slop-guard API snapshot.")
    ap.add_argument("spec", help="package spec, e.g. pandas==2.2.3")
    ap.add_argument("--out", default=None,
                    help="output dir (default: the bundled api_snapshots/)")
    ap.add_argument("--max-members", type=int, default=400)
    ap.add_argument("--submodule", action="append", default=[],
                    help="extra dotted submodule to snapshot (repeatable)")
    ap.add_argument("--install", action="store_true",
                    help="pip-install the spec if it is not importable")
    ns = ap.parse_args(argv)
    if ns.install:
        _pip_install_if_needed(ns.spec)
    default_out = Path(__file__).resolve().parent / "api_snapshots"
    out = Path(ns.out) if ns.out else default_out
    path = generate_snapshot(ns.spec, out, ns.max_members, ns.submodule)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
