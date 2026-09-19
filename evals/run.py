"""Self-repair eval loop — CLI orchestrator.

    python -m evals.run <case_name> [--typesafe-judge] [--llm-judge] [--diagnose]
                                    [--trace-id ID] [--model M] [--provider P]

Flow: load case -> run scenario -> judge each assertion -> report. If any
assertion fails and --diagnose is set, ask a Hermes sub-agent to propose a fix
(printed for human review, never auto-applied).

Exit code 0 if all assertions pass, else 1.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import yaml

from evals.judge import JudgeResult, judge

_CASES_DIR = Path(__file__).resolve().parent / "cases"


def load_case(name: str) -> Dict[str, Any]:
    """Load a case by bare name (``fallback_switch_notice``) or explicit path."""
    path = Path(name)
    if not path.exists():
        path = _CASES_DIR / (name if name.endswith((".yaml", ".yml")) else f"{name}.yaml")
    if not path.exists():
        raise FileNotFoundError(f"eval case not found: {name}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def run_scenario(scenario: Dict[str, Any]) -> str:
    """Produce the user-facing output string for a case scenario."""
    kind = scenario.get("kind")
    if kind == "function_call":
        module = importlib.import_module(scenario["module"])
        func = getattr(module, scenario["function"])
        # Two shapes: explicit literal `args` (any callable), or a stub `agent`
        # built from `agent_state` and passed as the sole positional argument.
        if "args" in scenario:
            result = func(*scenario["args"])
        else:
            agent = SimpleNamespace(**(scenario.get("agent_state") or {}))
            result = func(agent)
        wrap = scenario.get("wrap")
        return wrap.format(result=result) if wrap else str(result)
    raise ValueError(f"unsupported scenario kind: {kind!r}")


def evaluate(
    case: Dict[str, Any],
    *,
    use_typesafe: bool = False,
    use_llm: bool = False,
    model: str | None = None,
    provider: str | None = None,
) -> Tuple[str, List[Tuple[Dict[str, Any], JudgeResult]]]:
    """Run the case and judge every assertion. Returns (output, results)."""
    output = run_scenario(case["scenario"])
    results = [
        (a, judge(output, a, use_typesafe=use_typesafe, use_llm=use_llm,
                  model=model, provider=provider))
        for a in case.get("assertions", [])
    ]
    return output, results


def _print_report(case: Dict[str, Any], output: str, results) -> bool:
    print(f"\n=== eval: {case['name']} ===")
    print(f"--- output ---\n{output}\n--------------")
    all_passed = True
    for assertion, res in results:
        mark = "✓" if res.passed else "✗"
        text = " ".join((assertion.get("text") or "").split())
        print(f"{mark} [{res.mode}] {text}")
        print(f"    -> {res.reason}")
        all_passed = all_passed and res.passed
    print(f"\nRESULT: {'PASS' if all_passed else 'FAIL'}")
    return all_passed


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run")
    parser.add_argument("case", help="case name (e.g. fallback_switch_notice) or path")
    parser.add_argument("--typesafe-judge", action="store_true", help="use TypeSafe System One as judge (needs TYPESAFE_API_KEY)")
    parser.add_argument("--llm-judge", action="store_true", help="use the model as judge (needs hermes CLI + creds)")
    parser.add_argument("--diagnose", action="store_true", help="on failure, ask a sub-agent to propose a fix")
    parser.add_argument("--trace-id", default=None, help="failing Langfuse trace id to enrich diagnosis")
    parser.add_argument("--model", default=None)
    parser.add_argument("--provider", default=None)
    args = parser.parse_args(argv)

    case = load_case(args.case)
    output, results = evaluate(case, use_typesafe=args.typesafe_judge, use_llm=args.llm_judge,
                               model=args.model, provider=args.provider)
    passed = _print_report(case, output, results)

    if not passed and args.diagnose:
        from evals.diagnose import run_diagnose

        failures = [" ".join((a.get("text") or "").split()) + f" ({r.reason})" for a, r in results if not r.passed]
        print("\n=== diagnose (proposed fix — review before applying) ===")
        print(run_diagnose(case, output, failures, trace_id=args.trace_id, model=args.model, provider=args.provider))

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
