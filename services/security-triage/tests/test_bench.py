import bench
from scanners import Finding
from triage import TriageBatch, TriageResult


def _finding(fingerprint):
    return Finding(
        scanner="trivy",
        rule_id="CVE-2026-45829",
        title="chromadb: arbitrary code execution",
        target="services/knowledge-copilot/requirements.txt",
        fingerprint=fingerprint,
    )


def _result(fingerprint, **overrides):
    fields = dict(
        fingerprint=fingerprint,
        priority="high",
        exploitability="medium",
        impact="high",
        explanation="reachable from an unauthenticated endpoint",
        confidence=0.8,
    )
    fields.update(overrides)
    return TriageResult(**fields)


class CountingProvider:
    """A provider whose token counters climb the way a real one's do -- cumulatively,
    and without resetting between calls. That is the property run_config's delta read
    depends on."""

    name = "ollama"
    model_name = "fake"

    def __init__(self, per_call=(100, 20)):
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.per_call = per_call
        self.calls = 0

    def generate(self, system, user, schema):
        self.calls += 1
        self.prompt_tokens += self.per_call[0]
        self.output_tokens += self.per_call[1]
        fingerprints = [
            line.split(": ", 1)[1]
            for line in user.splitlines()
            if line.strip().startswith("fingerprint: ")
        ]
        return TriageBatch(results=[_result(f) for f in fingerprints]).model_dump_json()


def test_run_config_reports_only_its_own_tokens():
    provider = CountingProvider()
    findings = [_finding(f"fp{i}") for i in range(10)]

    bench.run_config(provider, findings, batch_size=2, calls=1)
    second = bench.run_config(provider, findings, batch_size=2, calls=1)

    # The counters are cumulative and shared, so after two configs the provider holds
    # 200 prompt tokens. Reporting that for the second config -- rather than its own 100
    # -- would make every row after the first look progressively more expensive.
    assert provider.prompt_tokens == 200
    assert second["prompt_tokens"] == 100
    assert second["output_tokens"] == 20


def test_run_config_counts_calls_and_findings_from_the_batch_size():
    provider = CountingProvider()
    findings = [_finding(f"fp{i}") for i in range(10)]

    row = bench.run_config(provider, findings, batch_size=3, calls=2)

    assert row["sent"] == 6
    assert row["calls"] == 2
    assert provider.calls == 2
    assert row["returned"] == 6
    # The claim batching rests on: 200 prompt tokens over 6 findings. Falling as the
    # batch grows is what makes a bigger default cheaper per finding.
    assert row["prompt_per_finding"] == 200 / 6


def test_cost_scales_to_a_thousand_findings():
    provider = CountingProvider(per_call=(100, 20))
    row = bench.run_config(provider, [_finding(f"fp{i}") for i in range(4)], 2, 2)
    # Two calls of two findings: 240 tokens over 4 findings, so 60,000 per 1,000.
    assert row["tokens_per_1k"] == 60_000


def test_calls_per_1k_halves_as_the_batch_doubles():
    findings = [_finding(f"fp{i}") for i in range(10)]
    small = bench.run_config(CountingProvider(), findings, batch_size=5, calls=1)
    large = bench.run_config(CountingProvider(), findings, batch_size=10, calls=1)
    # The free-tier cost model: the quota charges per request, not per finding, so this
    # is the column batch size actually buys down.
    assert small["calls_per_1k"] == 200
    assert large["calls_per_1k"] == 100


def test_contradictions_catches_an_explanation_fighting_its_own_rating():
    results = [
        _result("fp1", impact="low", explanation="the impact is high if triggered"),
        _result("fp2", exploitability="low", explanation="easily exploited remotely"),
        _result("fp3", impact="low", explanation="local only, limited blast radius"),
    ]
    assert bench._contradictions(results) == 2


def test_contradictions_allows_a_high_impact_claim_on_a_high_impact_rating():
    results = [_result("fp1", impact="high", explanation="the impact is high here")]
    assert bench._contradictions(results) == 0


def test_not_easily_exploitable_agrees_with_low_exploitability():
    # The false positive the full-corpus run produced four of. "not easily exploitable"
    # on an exploitability:low finding is the model being consistent, and a checker that
    # calls it a contradiction sends you hunting for a model bug that isn't there.
    results = [
        _result(
            "fp1",
            exploitability="low",
            explanation="Default security context, but not easily exploitable without more.",
        )
    ]
    assert bench._contradictions(results) == 0


def test_priority_mismatch_flags_a_verdict_that_ignores_its_own_ratings():
    results = [
        # medium + high = 5, cannot be low priority. The exact shape the full-corpus run
        # produced before TriageResult was reordered.
        _result("fp1", exploitability="medium", impact="high", priority="low"),
        # low + medium = 3, cannot be high priority. The inverse, same run.
        _result("fp2", exploitability="low", impact="medium", priority="high"),
        # high + high = 6 called critical: consistent, not flagged.
        _result("fp3", exploitability="high", impact="high", priority="critical"),
    ]
    assert bench._priority_mismatches(results) == 2


def test_priority_mismatch_exempts_needs_human():
    # A refusal is not a severity. Requiring it to follow from ratings the model just said
    # it could not confidently apply would penalise the one honest answer available.
    results = [
        _result("fp1", exploitability="medium", impact="high", priority="needs_human")
    ]
    assert bench._priority_mismatches(results) == 0


def test_truncated_counts_explanations_pinned_to_the_cap():
    from triage import EXPLANATION_MAX

    row = bench.run_config(CountingProvider(), [_finding("fp0")], 1, 1)
    # The fake writes a short explanation, so nothing is at the cap.
    assert row["truncated"] == 0
    assert row["expl_max"] < EXPLANATION_MAX
