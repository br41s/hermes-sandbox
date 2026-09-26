"""Fork's own packaging-metadata tests (test pin literals), kept out of upstream's file so upstream merges do not conflict."""

import ast
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


# Matches "name==version" and "name[extra]==version", ignoring any trailing
# environment marker / comment. Only exact pins are collected; ranged specs
# (">=", "<") can't be compared for equality and are skipped.
_PIN_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;,#]+)"
)


def _canonical(name: str) -> str:
    # PEP 503 normalization so e.g. discord.py / discord-py compare equal.
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins_from_specs(specs):
    """Map canonical package name -> set of exact-pinned versions seen."""
    pins: dict[str, set[str]] = {}
    for spec in specs:
        m = _PIN_RE.match(spec)
        if not m:
            continue
        pins.setdefault(_canonical(m.group(1)), set()).add(m.group(2))
    return pins


def _pyproject_pinned_specs():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    specs = list(data["project"].get("dependencies", []))
    for extra in data["project"].get("optional-dependencies", {}).values():
        specs.extend(extra)
    return specs


def _lazy_deps_pinned_specs():
    """Extract every string literal inside the LAZY_DEPS dict via AST.

    Parsing rather than importing keeps this test free of
    tools/lazy_deps.py's runtime imports and side effects.
    """
    src = (REPO_ROOT / "tools" / "lazy_deps.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    specs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "LAZY_DEPS" for t in targets):
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                specs.append(sub.value)
    assert specs, "could not extract specs from LAZY_DEPS — the AST parser drifted"
    return specs


# --- third copy of the pin: test literals -----------------------------------
# The two guards in test_packaging_metadata.py check that pyproject.toml and tools/lazy_deps.py agree
# WITH EACH OTHER. They passed while tests/tools/test_computer_use.py still
# asserted `mcp==1.26.0`, because that literal is a third copy nothing
# cross-checks — the bump in PR #288 updated both files, both guards went
# green, and CI slice 5/8 caught it only after push.
#
# Scope rule: only literals naming a package we ACTUALLY pin are checked. A
# test using `foo==1.0` as throwaway fixture data for a package we do not ship
# is not drift and must not be flagged, or this guard becomes noise and gets
# muted — the same failure mode the dependency queue itself hit.
#
# Escape hatch: put `pin-literal-ok` in a comment on the same line for a test
# that deliberately pins an off-version (e.g. exercising upgrade logic).

_PIN_OK_MARKER = "pin-literal-ok"


def _test_pin_literals():
    """(file, lineno, spec) for every `pkg==version` literal under tests/.

    Line-based rather than AST-based on purpose: it also catches pins written
    inside docstrings, parametrize tables and comments — anywhere a stale
    version can mislead the next reader, not just where it is asserted.
    """
    out = []
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for lineno, line in enumerate(lines, 1):
            if _PIN_OK_MARKER in line:
                continue
            for spec in re.findall(r'["\']([A-Za-z0-9_.\-]+==[0-9][^"\']*)["\']', line):
                out.append((path.relative_to(REPO_ROOT), lineno, spec))
    return out


def test_test_pin_literals_match_the_shipped_pins():
    """A pinned version written into a test must match what we ship.

    Without this, bumping a dependency leaves stale literals asserting the old
    version, and the only thing that notices is a red CI slice after push.
    """
    shipped = _pins_from_specs(_pyproject_pinned_specs() + _lazy_deps_pinned_specs())

    drift = []
    for relpath, lineno, spec in _test_pin_literals():
        m = _PIN_RE.match(spec)
        if not m:
            continue
        name = _canonical(m.group(1))
        expected = shipped.get(name)
        if not expected:
            continue  # not a package we pin — throwaway fixture data, fine
        if m.group(2) not in expected:
            drift.append(
                f"{relpath}:{lineno}: {spec} but we ship "
                f"{sorted(expected)} (pyproject/lazy_deps)"
            )

    assert not drift, (
        "a test hardcodes a version that no longer matches the shipped pin. "
        "Update the literal, or add a `pin-literal-ok` comment on that line if "
        "the off-version is deliberate:\n  " + "\n  ".join(drift)
    )
