import pytest
from pydantic import ValidationError

from errors import TriageProviderError
from scanners import Finding
from triage import (
    EXPLANATION_MAX,
    SYSTEM_PROMPT,
    TriageResult,
    build_prompt,
    triage_batch,
    triage_findings,
)


def _finding(fingerprint="fp1", **overrides):
    fields = dict(
        scanner="trivy",
        rule_id="CVE-2026-45829",
        title="chromadb: arbitrary code execution",
        target="services/knowledge-copilot/requirements.txt",
        fingerprint=fingerprint,
    )
    fields.update(overrides)
    return Finding(**fields)


def _result(fingerprint="fp1", priority="high", **overrides):
    fields = dict(
        fingerprint=fingerprint,
        priority=priority,
        exploitability="medium",
        impact="high",
        explanation="reachable from an unauthenticated endpoint",
        confidence=0.8,
    )
    fields.update(overrides)
    return TriageResult(**fields)


class FakeProvider:
    name = "fake"

    def __init__(self, responses):
        # A single JSON string, or a list -- one per expected call, in order.
        self._responses = responses if isinstance(responses, list) else [responses]
        self.calls = []

    def generate(self, system, user, schema):
        self.calls.append({"system": system, "user": user, "schema": schema})
        return self._responses[len(self.calls) - 1]


def _batch_json(*results: TriageResult) -> str:
    from triage import TriageBatch

    return TriageBatch(results=list(results)).model_dump_json()


def test_build_prompt_includes_every_findings_fingerprint():
    findings = [_finding("fp1"), _finding("fp2", rule_id="B104")]
    prompt = build_prompt(findings)
    assert "fp1" in prompt
    assert "fp2" in prompt


def test_triage_result_accepts_needs_human_as_a_legal_priority():
    result = _result(priority="needs_human")
    assert result.priority == "needs_human"


def test_triage_result_rejects_an_unknown_priority():
    with pytest.raises(ValidationError):
        _result(priority="urgent")


def test_triage_result_rejects_an_explanation_over_the_length_cap():
    # Found live on 2026-08-19: an unbounded explanation let qwen2.5-coder:1.5b loop a
    # repeating conditional dozens of times over. The cap is meant to stop generation
    # during decoding, but this only proves the schema-level guarantee holds.
    with pytest.raises(ValidationError):
        _result(explanation="x" * (EXPLANATION_MAX + 1))


def test_the_prompt_quotes_the_same_length_the_schema_enforces():
    # The Day 26 bug, as a test. The prompt said "one short sentence" and the schema
    # allowed 280 characters; only the schema is real to the model, because Ollama
    # grammar-constrains against it token by token, so it wrote 280-character
    # "sentences". Two bounds that disagree means the soft one is decoration -- if a
    # future edit moves one number, this fails.
    assert f"{EXPLANATION_MAX} characters" in SYSTEM_PROMPT
    assert TriageResult.model_fields["explanation"].metadata[0].max_length == (
        EXPLANATION_MAX
    )


def test_the_prompt_forbids_contradicting_the_assigned_ratings():
    # Day 26 shipped explanations announcing "the impact is high" on findings the same
    # result rated impact low. Each field was individually valid, so every guard passed.
    assert "impact is high" in SYSTEM_PROMPT
    assert "impact low" in SYSTEM_PROMPT


def test_triage_batch_returns_a_result_per_finding_sent():
    findings = [_finding("fp1"), _finding("fp2")]
    provider = FakeProvider(_batch_json(_result("fp1"), _result("fp2")))

    results = triage_batch(provider, findings)

    assert {r.fingerprint for r in results} == {"fp1", "fp2"}


def test_triage_batch_drops_a_fingerprint_the_model_invented():
    # The one guard this module exists for: a triage result for a finding nobody sent
    # is wrong risk data, not just incomplete risk data.
    findings = [_finding("fp1")]
    provider = FakeProvider(_batch_json(_result("fp1"), _result("fp-invented")))

    results = triage_batch(provider, findings)

    assert [r.fingerprint for r in results] == ["fp1"]


def test_triage_batch_keeps_a_needs_human_result():
    findings = [_finding("fp1")]
    provider = FakeProvider(_batch_json(_result("fp1", priority="needs_human")))

    results = triage_batch(provider, findings)

    assert results[0].priority == "needs_human"


def test_triage_batch_raises_on_malformed_json():
    provider = FakeProvider("not json at all")
    with pytest.raises(TriageProviderError) as caught:
        triage_batch(provider, [_finding("fp1")])
    assert caught.value.status == 502
    assert caught.value.provider == "fake"


def test_triage_batch_raises_on_a_schema_violation():
    provider = FakeProvider('{"results": [{"fingerprint": "fp1"}]}')
    with pytest.raises(TriageProviderError):
        triage_batch(provider, [_finding("fp1")])


def test_triage_findings_splits_into_batches_of_batch_size():
    findings = [_finding(f"fp{i}") for i in range(5)]
    responses = [
        _batch_json(_result("fp0"), _result("fp1")),
        _batch_json(_result("fp2"), _result("fp3")),
        _batch_json(_result("fp4")),
    ]
    provider = FakeProvider(responses)

    results = triage_findings(findings, provider=provider, batch_size=2)

    assert len(provider.calls) == 3
    assert {r.fingerprint for r in results} == {f"fp{i}" for i in range(5)}


def test_triage_findings_with_fewer_findings_than_batch_size_makes_one_call():
    findings = [_finding("fp1"), _finding("fp2")]
    provider = FakeProvider(_batch_json(_result("fp1"), _result("fp2")))

    triage_findings(findings, provider=provider, batch_size=5)

    assert len(provider.calls) == 1
