#!/usr/bin/env python3
"""Detect merge splices: duplicate top-level definitions in Python files.

THE failure mode of merging this fork with upstream is not a conflict git flags
— it is a resolution that keeps BOTH sides' blocks. The result parses, compiles
and passes ruff, and is silently wrong because the later one wins. Six of the
nine real regressions in the v2026.7.20 merge were of that shape.

USE IT LIKE THIS — the ``--base`` form is the one that works:

    python scripts/check_merge_splice.py --base v2026.7.20   # after resolving

Exit codes: 0 clean, 1 duplicates found, 2 bad usage.

WHAT IT CATCHES, MEASURED, so you can calibrate how much to trust a clean run.
Replayed against the four files whose splices were fixed by hand:

    caught  hermes_cli/container_boot.py  duplicate ``_AUTOSTART_STATES``
    caught* cron/scheduler.py             duplicated TERMINAL_CWD block
                                          (cron writer-lock deadlock)
    caught* tools/approval.py             both cron-deny blocks, which made
                                          upstream's tirith content scan dead
                                          code -> unattended cron approved
                                          commands it should have BLOCKED
    MISSED  hermes_cli/main.py            duplicate ``cron`` subparser, which
                                          broke the ENTIRE hermes CLI

    * only with ``--statements`` (see below)

The miss is instructive and not fixable here: there was no duplicate *name*.
Two different code paths registered the same argparse subcommand, and nothing
short of importing the module reveals it. **A clean run is not a merge gate —
running the actual test suite is.** This narrows what you must read; it does
not replace reading.

DEFAULT MODE reports only duplicate TOP-LEVEL definitions (module-level defs,
classes and CONSTANT assignments, plus methods in one class body). On the 73
non-test files this fork changes vs upstream it reports ZERO false positives,
because it excludes the two legitimate ways a name repeats: ``@property`` /
``@x.setter`` pairs and ``@overload`` stubs, and rebinds that consume their own
previous value (``CONFIG_SCHEMA = _build(...)`` ... ``CONFIG_SCHEMA = reordered``).

``--statements`` ALSO flags statements written twice close together in one
function. That is what catches the scheduler and approval.py splices, but it is
noisy — ~572 hits across those same 73 files, nearly all legitimate parallel
branches. Point it at ONE file you are actively resolving.

Whole-tree mode (no ``--base``) reports ~133 duplicates that are upstream's own
and predate us. Don't chase them.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections import Counter
from pathlib import Path

# Assignments that are legitimately rebound at module level in this codebase.
_ALLOWED_REBIND = {"__all__", "logger", "log", "__version__"}


def _is_constant_name(name: str) -> bool:
    """Only treat SCREAMING_SNAKE (and _SCREAMING) names as constants."""
    stripped = name.lstrip("_")
    return bool(stripped) and stripped.upper() == stripped and any(
        c.isalpha() for c in stripped
    )


def _is_overload_or_accessor(node) -> bool:
    """True for redefinitions the language *requires* to share a name.

    ``@x.setter`` / ``@x.deleter`` / ``@x.getter`` companions to a
    ``@property``, ``@typing.overload`` stubs, and ``@singledispatch``
    registrations all legitimately repeat the name.
    """
    for dec in getattr(node, "decorator_list", []):
        if isinstance(dec, ast.Attribute) and dec.attr in {"setter", "deleter", "getter", "register"}:
            return True
        name = dec.id if isinstance(dec, ast.Name) else getattr(dec, "attr", None)
        if name in {"overload", "register"}:
            return True
    return False


def _name_is_read_between(body, name: str, start: int, end: int) -> bool:
    """True if *name* is READ between two assignments to it.

    A rebind that consumes its own previous value is deliberate — e.g.
    ``CONFIG_SCHEMA = _build(...)`` then reorder then
    ``CONFIG_SCHEMA = _ordered_schema``. A splice never does this.
    """
    for stmt in body:
        if not (start <= getattr(stmt, "lineno", -1) <= end):
            continue
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Name) and sub.id == name and isinstance(sub.ctx, ast.Load):
                return True
    return False


def _duplicates_in_body(body, *, constants: bool) -> list[tuple[str, int, int]]:
    """Return (name, first_lineno, dup_lineno) for duplicated definitions."""
    seen: dict[str, int] = {}
    dups: list[tuple[str, int, int]] = []

    def _record(name: str, lineno: int) -> None:
        if name in _ALLOWED_REBIND:
            return
        if name in seen:
            dups.append((name, seen[name], lineno))
        else:
            seen[name] = lineno

    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _is_overload_or_accessor(node):
                continue
            _record(node.name, node.lineno)
        elif constants and isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and _is_constant_name(target.id):
                    _record(target.id, node.lineno)
    return [
        (name, first, dup)
        for name, first, dup in dups
        if not _name_is_read_between(body, name, first, dup)
    ]


_MIN_STATEMENT_LEN = 25
# A spliced resolution keeps both sides of ONE conflict hunk, so the two copies
# land close together. Repeats further apart than this are overwhelmingly
# legitimate (setup/teardown pairs, if/else arms, retry blocks) and reporting
# them buried the real signal ~100:1 when measured against this repo.
_PROXIMITY_LINES = 60


def _repeated_statements(fn) -> list[tuple[str, int, int]]:
    """Return (source, first_lineno, dup_lineno) for statements written twice.

    Walks the whole function body, not just direct children: a statement inside
    a loop appears ONCE in the AST, so a repeat here means it was literally
    written twice — which is what a spliced resolution produces.

    Only assignments and bare calls, and only distinctive ones, so the signal
    stays readable. ``if``/``else`` branches can legitimately repeat a
    statement, so this is a prompt to look, not a verdict.
    """
    counted: dict[str, list[int]] = {}
    for node in ast.walk(fn):
        if not isinstance(node, (ast.Assign, ast.AugAssign)) and not (
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        ):
            continue
        try:
            src = ast.unparse(node)
        except Exception:
            continue
        if len(src) < _MIN_STATEMENT_LEN or src.startswith(("logger.", "log.", "print(")):
            continue
        counted.setdefault(src, []).append(node.lineno)
    out = []
    for src, lines in counted.items():
        if len(lines) < 2:
            continue
        lines.sort()
        for first, dup in zip(lines, lines[1:]):
            if dup - first <= _PROXIMITY_LINES:
                out.append((src, first, dup))
                break
    return out


def scan(path: Path, *, statements: bool = False) -> list[str]:
    """Return human-readable findings for one Python file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError) as exc:
        return [f"{path}: could not parse ({exc.__class__.__name__}: {exc})"]

    findings = [
        f"{path}:{dup} duplicate top-level {name!r} (first defined at line {first})"
        for name, first, dup in _duplicates_in_body(tree.body, constants=True)
    ]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            findings += [
                f"{path}:{dup} duplicate method {node.name}.{name!r} "
                f"(first defined at line {first})"
                for name, first, dup in _duplicates_in_body(node.body, constants=False)
            ]
    if statements:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for src, first, dup in _repeated_statements(node):
                short = src if len(src) <= 90 else src[:87] + "..."
                findings.append(
                    f"{path}:{dup} repeated statement in {node.name}() "
                    f"(also at line {first}): {short}"
                )
    return findings


def _changed_python_files(base: str) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=d", f"{base}...HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"error: could not diff against {base!r}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    return [Path(p) for p in out.split() if p.endswith(".py") and Path(p).is_file()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--base",
        help="only scan .py files changed vs this ref (e.g. origin/main). "
             "Omit to scan the whole tree.",
    )
    ap.add_argument(
        "--statements",
        action="store_true",
        help="ALSO flag statements written twice close together inside one "
             "function. High recall, LOW precision (~572 hits across the 73 "
             "files of the v2026.7.20 merge, nearly all legitimate parallel "
             "branches). Point it at ONE file you are actively resolving, not "
             "the whole tree.",
    )
    ap.add_argument(
        "--include-tests",
        action="store_true",
        help="also scan tests/ (skipped by default — fixtures legitimately repeat "
             "setup/teardown statements, which drowns the signal)",
    )
    ap.add_argument("paths", nargs="*", type=Path, help="explicit files/dirs to scan")
    args = ap.parse_args()

    if args.paths:
        files = [p for p in args.paths if p.suffix == ".py" and p.is_file()]
        for d in (p for p in args.paths if p.is_dir()):
            files += sorted(d.rglob("*.py"))
    elif args.base:
        files = _changed_python_files(args.base)
    else:
        skip = {".git", ".venv", "node_modules", "__pycache__", "dist", "build"}
        files = [
            p for p in Path(".").rglob("*.py")
            if not any(part in skip for part in p.parts)
        ]

    if not args.include_tests and not args.paths:
        files = [f for f in files if "tests" not in f.parts]

    findings: list[str] = []
    for f in sorted(set(files)):
        findings += scan(f, statements=args.statements)

    if not findings:
        print(f"OK — no duplicate top-level definitions in {len(set(files))} file(s).")
        return 0

    print(f"MERGE SPLICE SUSPECTED — {len(findings)} finding(s):\n")
    for f in findings:
        print(f"  {f}")
    print(
        "\nEach is a name defined twice at the same level: the LATER one wins and "
        "the earlier is dead code.\nIf a duplicate is intentional, rename it — do "
        "not silence this check."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
