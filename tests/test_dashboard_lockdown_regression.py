"""Regression lock for safety net #3 — dashboard auth gate stays locked.

Guards against a repeat of the cryptominer incident (fake MCP servers injected
through an unauthenticated panel). Two failure modes:
  * OVER-EXPOSURE — a sensitive endpoint becomes reachable without auth.
  * SUB-PATH LEAK — an exact-match allowlist entry accidentally exposing
    sub-paths or look-alike prefixes.

Uses the real gate decision + live allowlist via fork_evals/checks/dashboard_gate.
"""
from fork_evals.checks.dashboard_gate import (
    EXPECTED_PUBLIC,
    audit_public_allowlist,
    audit_summary,
    gate_decision,
)
from fork_evals.run import evaluate, load_case
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS


class TestSensitivePathsAreGated:
    def test_mcp_and_capability_paths_require_auth(self):
        for path in (
            "/api/mcp/servers",
            "/api/mcp/install",
            "/api/tools",
            "/api/config",        # write endpoint (only /config/defaults is public)
            "/api/exec",
            "/api/secrets",
            "/",                  # the SPA shell / any HTML route
        ):
            assert gate_decision(path) == "GATED", f"{path} must require auth"

    def test_liveness_probe_stays_public(self):
        # The portal's wildcard health check depends on this staying open.
        assert gate_decision("/api/status") == "PUBLIC"

    def test_exact_match_does_not_leak_subpaths(self):
        # Documented reason for exact (not prefix) /api matching.
        assert gate_decision("/api/status/secret-extension") == "GATED"

    def test_static_prefix_does_not_leak_lookalikes(self):
        assert gate_decision("/assets/app.css") == "PUBLIC"   # legit static mount
        assert gate_decision("/assetsleak") == "GATED"        # look-alike, not public


class TestAllowlistIsMinimal:
    def test_live_allowlist_has_no_unexpected_or_sensitive_paths(self):
        assert audit_public_allowlist() == []

    def test_live_allowlist_matches_expected_set(self):
        # Any drift (addition or removal) must be a conscious change here.
        assert frozenset(PUBLIC_API_PATHS) == EXPECTED_PUBLIC

    def test_audit_tripwire_fires_on_injected_sensitive_path(self):
        bad = set(PUBLIC_API_PATHS) | {"/api/mcp/install"}
        problems = audit_public_allowlist(bad)
        assert any("mcp" in p for p in problems)

    def test_audit_tripwire_fires_on_unexpected_path(self):
        bad = set(PUBLIC_API_PATHS) | {"/api/analytics/raw"}
        problems = audit_public_allowlist(bad)
        assert any("unexpected" in p for p in problems)


class TestEvalHarnessAgreement:
    def test_seed_case_passes_deterministic_judge(self):
        case = load_case("dashboard_lockdown")
        output, results = evaluate(case, use_llm=False)
        assert "OK" in output
        assert results
        for assertion, res in results:
            assert res.passed, f"assertion failed: {assertion.get('text')!r} -> {res.reason}"
