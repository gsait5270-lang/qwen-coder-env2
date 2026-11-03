"""Generate offline API snapshots for *installed* packages (P2-21).

Usage:
    python -m tools.gen_snapshot pandas==2.2.3 [--out DIR] [--max-members N]

The command walks the live interpreter's package with ``dir()``/``inspect``
and emits the JSON snapshot format consumed by OfflineSnapshotClient:

    {"package": ..., "version": ..., "source": "generated ...",
     "members": {"_any": [...], "<Class>": [...], "<sub.module>": [...]}}

Because the snapshot is generated from the *installed version*, it closes
both the "_any is a partial subset" gap (top-level functions are captured
exhaustively) and the versioning gap (use ``--out`` + config `versions:` to
pin e.g. ``pandas@2.2.3.json``).
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
from typing import Optional


def _pip_install_if_needed(spec: str) -> str:
    """Return canonical ``pkg==ver``; pip-install the spec if not importable."""
    pkg = re.split(r"[=<>!~]", spec, maxsplit=1)[0].strip()
    try:
        mod = importlib.import_module(pkg)
        ver = getattr(mod, "__version__", "")
        if ver and f"=={ver}" in spec.replace(" ", ""):
            return f"{pkg}=={ver}"
        if not ver:
            return pkg
        # installed but different version than requested -> warn, keep going
        if f"=={ver}" != spec.replace(" ", "") and "==" in spec:
            print(f"note: installed {pkg}=={ver} differs from {spec}", file=sys.stderr)
        return f"{pkg}=={ver}"
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", spec])
        mod = importlib.import_module(pkg)
        return f"{pkg}=={getattr(mod, '__version__', '')}"


def _public_dir(obj) -> list[str]:
    names = []
    for n in dir(obj):
        if n.startswith("_"):
            continue
        names.append(n)
    return sorted(names)


def _class_members(cls) -> list[str]:
    out: set[str] = set()
    try:
        for mro_cls in inspect.getmro(cls):
            if mro_cls.__module__.split(".")[0] not in ("builtins",):
                out.update(_public_dir(mro_cls))
    except Exception:
        pass
    # drop nested classes that start uppercase-of-uppercase noise? keep simple
    return sorted(out)


def generate_snapshot(spec: str, out_dir: Path, max_members: int = 400,
                      submodules: Optional[list[str]] = None) -> Path:
    pkg, _, version = spec.partition("==")
    mod = importlib.import_module(pkg)
    members: dict[str, list[str]] = {}

    top = [n for n in _public_dir(mod)]
    members["_any"] = top[:max_members]

    for name in top:
        try:
            obj = getattr(mod, name)
        except AttributeError:
            continue
        if inspect.isclass(obj) and obj.__module__.startswith(pkg):
            members[name] = _class_members(obj)[:max_members]
        elif inspect.ismodule(obj) and obj.__name__.startswith(pkg + "."):
            members[obj.__name__[len(pkg) + 1:]] = _public_dir(obj)[:max_members]

    # explicitly requested deep submodules, e.g. torch.nn.functional
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


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", help="package spec, e.g. pandas==2.2.3")
    ap.add_argument("--out", default=None,
                    help="output dir (default: src/slop_guard/api_snapshots)")
    ap.add_argument("--max-members", type=int, default=400)
    ap.add_argument("--submodule", action="append", default=[],
                    help="extra dotted submodule to snapshot (repeatable)")
    ns = ap.parse_args(argv)
    _pip_install_if_needed(ns.spec)
    default_out = Path(__file__).resolve().parents[1].joinpath(
        "src/slop_guard/api_snapshots")
    out = Path(ns.out) if ns.out else default_out
    path = generate_snapshot(ns.spec, out, ns.max_members, ns.submodule)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
