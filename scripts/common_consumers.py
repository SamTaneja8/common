#!/usr/bin/env python3
"""Which repos need rebuilding for a change to common.

Each repo below copies common/common_utils into its image at build time, so
it only needs rebuilding when something it actually uses changed since the
common commit it was last built with. This works that out:

1. Changed files: `git diff` between the common commit each repo was last
   built with (recorded by deal-pipeline's deploy.sh in STATE_DIR/<repo>)
   and common's current HEAD.
2. Changed common_utils modules, plus every common_utils module that imports
   one of them (transitively) -- including package __init__ files, which run
   whenever anything in the package is imported.
3. A repo is affected if its own code (Python imports, or `common_utils.x`
   references in shell/YAML, e.g. `python -m common_utils.metering_cli`)
   uses an affected module.

Errs on the side of rebuilding: no record of the last build, an unknown
commit, pyproject.toml, or a non-Python file inside common_utils all count
as affecting the repo. Changes outside common_utils that no image contains
(orchestration/, observability/, host scripts, docs, tests) affect nothing,
except scraper-base's own inputs, which affect the repos built on the local
scraper-base image (amazonnew).

Usage:
    common_consumers.py --host vps1 [--repos-root ~] [--state-dir DIR] [--names-only]

Exit 0; prints one line per repo, or with --names-only just the affected
repo names, one per line, in rebuild order. Standard library only, so it
runs with the host's python3.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

# Repos that copy common_utils into their image, per host, in rebuild order.
CONSUMERS = {
    "vps1": ["dealnews1", "dealmoon1", "amazonnew", "reviewgate", "autopub"],
    "vps2": ["aistage"],
}
# Built FROM the local scraper-base image (common/Dockerfile.scraper-base).
SCRAPER_BASE_CONSUMERS = {"amazonnew"}
SCRAPER_BASE_INPUTS = {"Dockerfile.scraper-base", ".dockerignore", "scripts/run_job_common.sh", "bin/metering_cli.py"}
PACKAGE = "common_utils"
DOC_SUFFIXES = {".md", ".rst", ".txt"}
SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__", "tests", "logs", ".next", "runs"}
REFERENCE_RE = re.compile(r"\bcommon_utils(?:\.[A-Za-z_]\w*)+")


def git(common_dir: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(common_dir), *args], check=True, capture_output=True, text=True).stdout


def module_name(path: str) -> str | None:
    """'common_utils/dealvant/store.py' -> 'common_utils.dealvant.store'."""
    if not path.startswith(PACKAGE + "/") or not path.endswith(".py"):
        return None
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def parents(module: str) -> list[str]:
    """'a.b.c' -> ['a', 'a.b']: packages whose __init__ runs on import."""
    parts = module.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


def imported_modules(source: str, *, current: str | None, known: set[str]) -> set[str]:
    """common_utils modules a Python file imports (with their parent
    packages). `current` resolves relative imports inside common_utils."""
    found: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {m for ref in REFERENCE_RE.findall(source) for m in _with_parents(ref, known)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PACKAGE or alias.name.startswith(PACKAGE + "."):
                    found.update(_with_parents(alias.name, known))
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level and current:
                package = current.split(".")[: -node.level] if not current.endswith("__init__") else current.split(".")
                base = ".".join(package + ([base] if base else []))
            if base == PACKAGE or base.startswith(PACKAGE + "."):
                found.update(_with_parents(base, known))
                for alias in node.names:
                    candidate = f"{base}.{alias.name}"
                    if candidate in known:
                        found.update(_with_parents(candidate, known))
    return found


def _with_parents(name: str, known: set[str]) -> set[str]:
    # Trim attribute access (common_utils.metering_cli.main) to the module.
    parts = name.split(".")
    while len(parts) > 1 and ".".join(parts) not in known:
        parts.pop()
    module = ".".join(parts)
    return {module, *parents(module)}


def common_modules(common_dir: Path) -> dict[str, Path]:
    modules = {}
    for path in (common_dir / PACKAGE).rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        name = module_name(path.relative_to(common_dir).as_posix())
        if name:
            modules[name] = path
    return modules


def affected_modules(changed: set[str], modules: dict[str, Path]) -> set[str]:
    """Changed modules plus everything in common_utils that imports them,
    transitively."""
    known = set(modules)
    importers: dict[str, set[str]] = {}
    for name, path in modules.items():
        current = name + ".__init__" if path.name == "__init__.py" else name
        for target in imported_modules(path.read_text(encoding="utf-8", errors="replace"), current=current, known=known):
            importers.setdefault(target, set()).add(name)
    result = set(changed)
    frontier = list(changed)
    while frontier:
        for importer in importers.get(frontier.pop(), ()):
            if importer not in result:
                result.add(importer)
                frontier.append(importer)
    return result


def repo_references(repo_dir: Path, known: set[str]) -> dict[str, str]:
    """common_utils modules the repo uses -> one file that uses each."""
    uses: dict[str, str] = {}
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in files:
            path = Path(root) / filename
            if filename.endswith(".py"):
                found = imported_modules(path.read_text(encoding="utf-8", errors="replace"), current=None, known=known)
            elif filename.endswith((".sh", ".yml", ".yaml", ".toml", ".cfg", ".ini")) or filename == "Dockerfile":
                text = path.read_text(encoding="utf-8", errors="replace")
                found = {m for ref in REFERENCE_RE.findall(text) for m in _with_parents(ref, known)}
            else:
                continue
            for module in found:
                uses.setdefault(module, path.relative_to(repo_dir.parent).as_posix())
    return uses


def assess(repo: str, *, common_dir: Path, repos_root: Path, state_dir: Path, head: str, modules: dict[str, Path]) -> tuple[bool, str]:
    """(needs rebuild, reason)."""
    record = state_dir / repo
    if not record.exists():
        return True, "no record of the common commit it was last built with"
    built = record.read_text(encoding="utf-8").strip()
    if built == head:
        return False, f"already built with common {head[:7]}"
    try:
        git(common_dir, "cat-file", "-e", f"{built}^{{commit}}")
    except subprocess.CalledProcessError:
        return True, f"its recorded common commit {built[:7]} isn't in this checkout"
    changed_files = [line for line in git(common_dir, "diff", "--name-only", f"{built}..{head}").splitlines() if line]

    changed_modules: set[str] = set()
    for path in changed_files:
        if path == "pyproject.toml":
            return True, "common's pyproject.toml changed (packaging)"
        if path in SCRAPER_BASE_INPUTS and repo in SCRAPER_BASE_CONSUMERS:
            return True, f"scraper-base input {path} changed (image built FROM it)"
        if path.startswith(PACKAGE + "/"):
            name = module_name(path)
            if name:
                changed_modules.add(name)
            elif Path(path).suffix not in DOC_SUFFIXES:
                return True, f"{path} changed (non-Python file shipped inside common_utils)"
    if not changed_modules:
        return False, f"nothing it ships changed since {built[:7]} ({len(changed_files)} file(s) outside common_utils)"

    affected = affected_modules(changed_modules, modules)
    uses = repo_references(repos_root / repo, set(modules) | changed_modules)
    # Most specific first: name the module, not just its package.
    hits = sorted((m for m in affected if m in uses and m != PACKAGE), key=lambda m: (-m.count("."), m))
    if hits:
        return True, f"uses {hits[0]} ({uses[hits[0]]})" + (f" and {len(hits) - 1} more changed module(s)" if len(hits) > 1 else "")
    if PACKAGE in affected and PACKAGE in uses:
        return True, "common_utils/__init__.py changed"
    names = sorted(changed_modules)
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    return False, f"doesn't use the changed module(s): {shown}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Which repos need rebuilding for a change to common.")
    parser.add_argument("--host", required=True, choices=sorted(CONSUMERS))
    parser.add_argument("--repos-root", default=os.path.expanduser("~"))
    parser.add_argument("--state-dir", help="default: <repos-root>/deal-pipeline/.state/common-built")
    parser.add_argument("--names-only", action="store_true", help="print only the repos to rebuild, in order")
    args = parser.parse_args(argv)

    repos_root = Path(args.repos_root)
    common_dir = repos_root / "common"
    state_dir = Path(args.state_dir) if args.state_dir else repos_root / "deal-pipeline" / ".state" / "common-built"
    head = git(common_dir, "rev-parse", "HEAD").strip()
    modules = common_modules(common_dir)

    for repo in CONSUMERS[args.host]:
        if not (repos_root / repo / ".git").exists():
            if not args.names_only:
                print(f"{repo:<11} skip     not checked out on this host")
            continue
        rebuild, reason = assess(repo, common_dir=common_dir, repos_root=repos_root, state_dir=state_dir, head=head, modules=modules)
        if args.names_only:
            if rebuild:
                print(repo)
        else:
            print(f"{repo:<11} {'REBUILD' if rebuild else 'skip':<8} {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
