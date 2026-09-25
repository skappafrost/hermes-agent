"""Moved-symbol import sanity gate for update-hermes (Windows).

Why: the custom/obsidian-vault rebase policy is custom-side-wins. When
upstream refactors a symbol into a new module, an OLD custom-side call site
can survive the rebase importing a name that no longer exists in the target
module (2026-09-08: cron/scheduler.py imported _scan_cron_skill_assembled
from tools.cronjob_tools after upstream 5a8fbbf moved it to
tools.cronjob_prompt_scan -> every skill-backed cron run ImportError'd).

What: statically walk every tracked .py file's ``from <local.module> import
<name>`` statements (AST, function-local imports included) and verify each
imported name exists at the target module's top level — counting defs,
classes, assignments, and the module's OWN imports (which covers re-export
shims). Never executes repo code; safe to run mid-update.

Whitelist: one ``path.py:NAME`` per line, # comments allowed. Entries cover
known-benign misses (dynamic attributes, monkeypatched modules). Lives
OUTSIDE the repo so update's git ops never fight it.

Exit codes: 0 = clean, 1 = violations found (update must fail), 2 = harness
error (python missing, not a git repo...).
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
WHITELIST = Path(__file__).with_name("import-check-known-issues.txt")

# Only check imports whose target resolves inside the repo tree — external
# packages (stdlib, site-packages) are out of scope for this gate.
STDLIBish = set(sys.stdlib_module_names)


def git_tracked_py() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--", "*.py"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8", "replace")
    return [REPO / p.replace("/", "\\") for p in out.split("\0") if p]


def resolve_module(dotted: str, origin: Path, level: int) -> Path | None:
    if level:
        base = origin.parent
        for _ in range(level - 1):
            base = base.parent
        parts = dotted.split(".") if dotted else []
        root = base.joinpath(*parts) if parts else base
    else:
        parts = dotted.split(".")
        root = (REPO / Path(*parts)) if parts[0] not in STDLIBish else None
        if root is None:
            return None
    if root.with_suffix(".py").exists():
        return root.with_suffix(".py")
    init = root / "__init__.py"
    return init if init.exists() else None


_toplevel_cache: dict[Path, set[str] | None] = {}


def toplevel_names(path: Path) -> set[str] | None:
    """Top-level names a ``from path import name`` can bind: defs, classes,
    assignments, function signatures excluded, plus names the module itself
    imports (re-export shims). For a package ``__init__.py`` the valid set
    also includes every submodule name (``from pkg import submodule`` is a
    legal bind). Returns None if the file can't be parsed."""
    if path in _toplevel_cache:
        return _toplevel_cache[path]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, SyntaxError):
        _toplevel_cache[path] = None
        return None
    # PEP 562: a module-level __getattr__ can serve ANY name lazily
    # (dynamic proxies, re-export tables) — the file is un-checkable.
    # A wildcard import likewise injects an unbounded dynamic namespace.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "__getattr__":
            _toplevel_cache[path] = None
            return None
        if isinstance(node, ast.ImportFrom) and any(
                a.name == "*" for a in node.names):
            _toplevel_cache[path] = None
            return None
    names: set[str] = set()
    def _collect_targets(t: ast.expr, sink: set[str]) -> None:
        if isinstance(t, ast.Name):
            sink.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for el in t.elts:
                _collect_targets(el, sink)
    def _bind(node: ast.AST, sink: set[str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            sink.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                _collect_targets(tgt, sink)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            sink.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                sink.add(a.asname or a.name.split(".")[0])
    for node in tree.body:
        _bind(node, names)
        # Module-level compound blocks (if TYPE_CHECKING / try-except fallback /
        # with) still bind into the module namespace — walk one statement level.
        if isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith)):
            for sub in node.body + getattr(node, "orelse", []) \
                    + getattr(node, "finalbody", []) \
                    + [h.body[0] if h.body else None for h in getattr(node, "handlers", []) if h.body]:
                if sub is not None:
                    _bind(sub, names)
    _toplevel_cache[path] = names
    return names


def load_whitelist() -> set[str]:
    if not WHITELIST.exists():
        return set()
    return {
        line.split("#", 1)[0].strip()
        for line in WHITELIST.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    }


def _git_grep(pattern: str, ref: str | None) -> list[str]:
    args = ["git", "grep", "-n", "-E", pattern, "--"]
    if ref:
        args = ["git", "grep", "-n", "-E", pattern, ref, "--"]
    try:
        r = subprocess.run(args, cwd=REPO, capture_output=True, text=True)
        if r.returncode == 0:
            return [ln for ln in r.stdout.splitlines() if ln]
    except OSError:
        pass
    return []


def suggest_fix(name: str) -> str:
    """Where did the missing symbol go? Working tree first (moved by an
    upstream refactor), then the main/origin refs (definition dropped from
    the merged file by custom-side-wins). Best-effort; '' = inconclusive.
    Runs ONLY on the failure path, so the passing gate stays fast."""
    patterns = (
        rf"\b(def|class)[[:space:]]+{name}\b",  # functions / classes
        rf"^{name}[[:space:]]*(=|:[^=])",        # module constants
    )
    for pat in patterns:
        hits = [h for h in _git_grep(pat, None)
                if not h.split(":", 1)[0].startswith(("tests/", "evals/"))]
        if hits:
            return (f"hint: '{name}' is defined in the working tree "
                    f"({hits[0].split(':',2)[0]}:{hits[0].split(':')[1]}) — "
                    "repoint the import to that module.")
        for ref in ("main", "origin/main", "my-fork/main"):
            hits = [h for h in _git_grep(pat, ref)
                    if not h.split(":", 1)[1].startswith(("tests/", "evals/"))]
            if hits:
                _, path, line = hits[0].split(":", 2)
                return (f"hint: '{name}' exists on '{ref}' ({path}:{line}) but NOT in the "
                        "working tree — the custom-side merge dropped upstream's definition; "
                        f"restore it: git show {ref}:{path} (copy the def into the target file).")
    return (f"hint: '{name}' found nowhere — upstream likely deleted it outright; remove "
            "the import and its use, or check upstream for the replacement API.")


def main() -> int:
    if not (REPO / ".git").exists():
        print(f"[import-check] not a git repo: {REPO}", file=sys.stderr)
        return 2
    whitelist = load_whitelist()
    violations: list[str] = []
    checked = 0
    for path in git_tracked_py():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError):
            continue
        rel = path.relative_to(REPO).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level == 0 and (node.module is None or "." not in node.module
                                    and node.module.split(".")[0] not in {
                                        "tools", "hermes_cli", "cron", "gateway",
                                        "agent", "plugins", "apps", "tui_gateway",
                                        "scripts"}):
                continue
            target = resolve_module(node.module or "", path, node.level)
            if target is None:
                continue  # not repo-internal (external pkg / data dir)
            names = toplevel_names(target)
            if names is None:
                continue
            pkg_dir = target.parent if target.name == "__init__.py" else None
            skip_file = rel.startswith("tests/") or rel.startswith("evals/")
            for a in node.names:
                if a.name == "*":
                    continue
                checked += 1
                if a.name in names:
                    continue
                # `from pkg import submodule` binds a child module, not a
                # top-level name — accept a resolvable submodule path.
                if pkg_dir is not None and (
                    (pkg_dir / f"{a.name}.py").exists()
                    or (pkg_dir / a.name / "__init__.py").exists()
                ):
                    continue
                key = f"{target.relative_to(REPO).as_posix()}:{a.name}"
                if key not in whitelist:
                    violations.append((skip_file, f"{rel}:{node.lineno}: {key}"))
    print(f"[import-check] {checked} import bindings verified against repo tree")
    hard = [v for is_soft, v in violations if not is_soft]
    soft = [v for is_soft, v in violations if is_soft]
    if soft:
        print(f"[import-check] {len(soft)} stale import(s) in tests/evals (WARN only "
              "- they run in CI, not the shipped product):", file=sys.stderr)
        for v in soft:
            print(f"  {v}", file=sys.stderr)
    if hard:
        print(f"[import-check] {len(hard)} STALE IMPORT(S) FOUND in runtime code:", file=sys.stderr)
        for v in hard:
            print(f"  {v}", file=sys.stderr)
        # One actionable hint per missing symbol (not per occurrence) —
        # failure-path only, so the passing gate stays fast.
        seen: set[str] = set()
        for v in hard:
            name = v.rsplit(":", 1)[1]
            if name not in seen:
                seen.add(name)
                hint = suggest_fix(name)
                if hint:
                    print(f"  {hint}", file=sys.stderr)
        print("  -> A call site imports a name that no longer exists in the target "
              "module (moved/removed by upstream, kept by the custom-side-wins "
              f"rebase). Fix the import, or whitelist a known-benign case in "
              f"{WHITELIST.name}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
