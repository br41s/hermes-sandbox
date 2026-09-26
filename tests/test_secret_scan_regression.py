"""Regression lock for safety net #2 — credential scanner (fork_evals/checks/secret_scan).

Guards two failure modes that have real cost:
  * MISS — a live token shape slips through (the original incident: a bare
    GitHub PAT in a cron prompt).
  * FALSE ALARM — a safe ``$GITHUB_TOKEN`` env reference gets flagged, which
    would train operators to ignore the scanner.

All tokens below are obviously fake (FAKE / zeros / NOTREAL) — shape only.
"""
from fork_evals.checks.secret_scan import scan_for_credentials, scan_text_summary
from fork_evals.run import evaluate, load_case

# Shape-only fakes — never real credentials.
LEAKY = {
    "github_pat_finegrained": "github_pat_FAKE0000example0000token0000NOTREAL00",
    "github_token": "ghp_FAKE0000000000000000000000000000NOTREAL",
    "anthropic_key": "sk-ant-FAKE0000000000000000000NOTREAL",
    "openai_key": "sk-FAKE0000000000000000000000000000NOTREAL00",
    "langfuse_secret_key": "sk-lf-0000aaaa-1111-2222-3333-444455556666",
    "aws_access_key_id": "AKIAFAKE0000EXAMPLE0",
    "slack_token": "xoxb-FAKE0000-NOTREAL-token",
    "google_api_key": "AIzaFAKE0000example0000token0000NOTREAL000",
}

SAFE = [
    "Use $GITHUB_TOKEN from the credential helper, never hardcode it.",
    'token = os.environ["GITHUB_TOKEN"]',
    "Set GITHUB_TOKEN in the environment; placeholder is <your-token-here>.",
    "Summarize the article and push the result to the repo.",
    "The secret to good prompts is being specific.",  # 'secret' as a plain word
]


class TestCatchesLeaks:
    def test_each_token_shape_is_flagged(self):
        for expected_label, token in LEAKY.items():
            findings = scan_for_credentials(f"prefix {token} suffix")
            labels = {f.label for f in findings}
            assert expected_label in labels, f"{expected_label} not detected in {token!r}"

    def test_private_key_block_is_flagged(self):
        text = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END..."
        assert any(f.label == "private_key_block" for f in scan_for_credentials(text))

    def test_findings_are_redacted(self):
        token = LEAKY["github_pat_finegrained"]
        findings = scan_for_credentials(token)
        assert findings
        # The full secret must never appear in a finding preview.
        for f in findings:
            assert token not in f.preview


class TestNoFalseAlarms:
    def test_safe_strings_are_clean(self):
        for text in SAFE:
            assert scan_for_credentials(text) == [], f"false positive on: {text!r}"


class TestEvalHarnessAgreement:
    def test_seed_case_passes_deterministic_judge(self):
        case = load_case("no_secrets_in_prompts")
        _output, results = evaluate(case, use_llm=False)
        assert results
        for assertion, res in results:
            assert res.passed, f"assertion failed: {assertion.get('text')!r} -> {res.reason}"

    def test_summary_reports_clean_text(self):
        assert scan_text_summary("just a normal prompt") == "OK: no credential-shaped strings found."
