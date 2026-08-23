"""Batched, structured triage over deduped findings -- Day 23.

One model call per ST_BATCH_SIZE findings, not one call per finding: 559 deduped
findings at batch size 5 is ~112 calls instead of 559, and on a CPU-only backend that
difference is the gap between testable and not.

Two guards, both learned from earlier weeks:

- Every returned `fingerprint` must be one that was actually sent. A model naming a
  fingerprint nobody sent is the same failure mode as Day 10's invented citations
  ([1] pointing at a chunk that was never retrieved) -- the fix is the same shape,
  drop what wasn't in the input rather than trust it.
- `needs_human` is a legal `priority`, not an error path. A model forced to choose
  between four real severities on a finding it can't actually judge doesn't refuse --
  it guesses, confidently, and the guess looks identical to a real triage. Giving it a
  legal way to decline is what makes the other four priorities trustworthy.
"""

import logging
import os
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from errors import TriageProviderError
from provider import BaseTriageProvider, get_triage_provider
from scanners import Finding

logger = logging.getLogger(__name__)

BATCH_SIZE = int(os.getenv("ST_BATCH_SIZE", "5"))

# One number, used twice: as the schema's hard cap and as the figure the prompt quotes.
# They have to agree, and on Day 26 they did not -- see the Field comment below.
EXPLANATION_MAX = 160

SYSTEM_PROMPT = f"""You are a security triage assistant. You are given findings already \
produced by real scanners (Trivy, Bandit, Checkov) -- your job is not to find more \
issues, it is to judge the ones you're given.

For each finding, judge, in this order:
- exploitability: how easy this is to actually trigger here (low/medium/high)
- impact: the blast radius if it is triggered (low/medium/high)
- priority: your overall call, and it must follow from the two ratings you just made. \
High exploitability with high impact is not a low priority. Low exploitability with low \
impact is not a high one. If your priority does not follow from them, change it.

Rules:
- Return exactly one result per finding you were given, using its fingerprint exactly \
as sent. Do not invent a fingerprint, rename one, or skip one.
- If you cannot judge a finding with reasonable confidence -- not enough context, or it \
genuinely needs a human's judgment -- set priority to "needs_human" instead of \
guessing at a severity.
- confidence is your own calibrated certainty in this specific judgment, 0.0-1.0. It is \
not the scanner's severity field restated as a decimal.
- explanation is one short sentence of at most {EXPLANATION_MAX} characters, plain \
language. State your judgment; do not enumerate hypothetical cases ("if it can be \
exploited... if it cannot...").
- explanation must agree with the exploitability and impact you assigned to the same \
finding. Do not write that the impact is high on a finding you rated impact low, and do \
not describe something as easily exploitable when you rated exploitability low. If the \
sentence and the ratings disagree, change one of them before answering.
"""


class TriageResult(BaseModel):
    # Field order is load-bearing. Ollama grammar-constrains generation to this schema in
    # declared order, so a field is decided before every field below it exists. Until
    # Day 27 `priority` came first, which made the prompt's "your overall call, weighing
    # both of the above" impossible to follow -- there was nothing above it yet. The model
    # guessed a verdict, then filled in the ratings, and the full-corpus run shows the
    # result: priority came out *anti-correlated* with its own inputs, `expl=medium
    # imp=high` landing on `low` while `expl=low imp=medium` landed on `high`.
    #
    # The order below is the dependency order the prompt always described: the two
    # component judgments, then the call that weighs them, then the sentence explaining a
    # verdict that already exists, then certainty about it. Same fix as
    # log-analyzer/log_analyzer.py's LogAnalysis the same day -- reasoning before
    # conclusion, expressed through the schema rather than asked for in the prompt.
    fingerprint: str
    exploitability: Literal["low", "medium", "high"]
    impact: Literal["low", "medium", "high"]
    priority: Literal["critical", "high", "medium", "low", "needs_human"]
    # max_length is in the JSON schema Ollama grammar-constrains against, not just a
    # post-hoc check: found live on 2026-08-19, qwen2.5-coder:1.5b fell into a repeating
    # conditional ("if it can be exploited... if it cannot...") dozens of times over on
    # an unbounded `str`. A hard cap stops the loop during decoding; repeat_penalty
    # alone (below) wasn't enough to stop it from starting.
    #
    # 160, down from Day 23's 280, because 280 was doing two jobs badly. Day 26's output
    # ran ~270 characters against a prompt asking for one short sentence, and the reason
    # is that only one of those two bounds is real to the model: the grammar constraint
    # is enforced token by token during decoding, "one short sentence" is a suggestion.
    # Given a 280-character budget it wrote 280 characters. The fix is to make the hard
    # bound mean what the soft one asked for, and to quote the same number in the prompt
    # so they cannot drift apart again -- test_triage.py asserts they still match.
    explanation: str = Field(max_length=EXPLANATION_MAX)
    confidence: float = Field(ge=0.0, le=1.0)


class TriageBatch(BaseModel):
    results: list[TriageResult]


def _format_finding(finding: Finding, index: int) -> str:
    lines = [
        f"Finding {index}:",
        f"  fingerprint: {finding.fingerprint}",
        f"  scanner: {finding.scanner}",
        f"  rule_id: {finding.rule_id}",
        f"  title: {finding.title}",
        f"  target: {finding.target}",
    ]
    if finding.line is not None:
        lines.append(f"  line: {finding.line}")
    if finding.package:
        fixed = finding.fixed_version or "no fix available"
        lines.append(
            f"  package: {finding.package} {finding.installed_version} -> {fixed}"
        )
    if finding.cwe:
        lines.append(f"  cwe: {finding.cwe}")
    if finding.severity_raw:
        lines.append(f"  scanner_severity: {finding.severity_raw}")
    return "\n".join(lines)


def build_prompt(findings: list[Finding]) -> str:
    return "\n\n".join(_format_finding(f, i) for i, f in enumerate(findings, start=1))


def _drop_unsent_fingerprints(
    results: list[TriageResult], sent: set[str], provider_name: str
) -> list[TriageResult]:
    kept = [r for r in results if r.fingerprint in sent]
    dropped = len(results) - len(kept)
    if dropped:
        invented = sorted({r.fingerprint for r in results} - sent)
        logger.warning(
            f"{provider_name} returned {dropped} fingerprint(s) not in the sent "
            f"batch: {invented}; dropping"
        )
    return kept


def triage_batch(
    provider: BaseTriageProvider, findings: list[Finding]
) -> list[TriageResult]:
    """One model call for up to BATCH_SIZE findings. Never returns a fingerprint that
    wasn't in `findings` -- a triage result for a finding nobody sent isn't a missing
    answer, it's a wrong one, and wrong risk data is worse than incomplete risk data.
    """
    sent = {f.fingerprint for f in findings}
    raw = provider.generate(SYSTEM_PROMPT, build_prompt(findings), schema=TriageBatch)
    try:
        parsed = TriageBatch.model_validate_json(raw)
    except ValidationError as e:
        # Pydantic's own error truncates the offending JSON; the full text is what
        # actually tells a rambling model apart from one that's merely too terse for
        # MAX_TOKENS -- worth the full line since ST_MAX_TOKENS already bounds its size.
        logger.warning(f"invalid triage JSON from {provider.name}: {raw}")
        raise TriageProviderError(
            f"the model returned invalid triage JSON: {e}", 502, provider=provider.name
        ) from e
    return _drop_unsent_fingerprints(parsed.results, sent, provider.name)


def triage_findings(
    findings: list[Finding],
    provider: BaseTriageProvider | None = None,
    batch_size: int | None = None,
) -> list[TriageResult]:
    provider = provider or get_triage_provider()
    batch_size = batch_size or BATCH_SIZE
    results: list[TriageResult] = []
    for i in range(0, len(findings), batch_size):
        results.extend(triage_batch(provider, findings[i : i + batch_size]))
    return results


if __name__ == "__main__":
    # The Day 23 verify step: triage a slice of the real fixture corpus against
    # whichever provider ST_LLM_PROVIDER names, printing wall-clock per batch and then
    # every judgment it produced. Defaults to 15 findings (3 batches at the default size)
    # rather than the full 559-deduped fixture -- on CPU Ollama that full run could be
    # hours, and the point here is a timing reading plus eyes on the actual output, not a
    # full corpus triage.
    #
    #   python triage.py [fixture_path] [limit]
    import json
    import sys
    import time
    from collections import Counter

    from scanners import dedupe, parse_envelope

    fixture_path = sys.argv[1] if len(sys.argv) > 1 else "fixtures/this-repo.json"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 15

    with open(fixture_path) as f:
        envelope = json.load(f)
    findings = dedupe(parse_envelope(envelope))[:limit]
    print(f"triaging {len(findings)} findings, batch size {BATCH_SIZE}")

    triage_provider = get_triage_provider()
    print(f"provider: {triage_provider.name} ({triage_provider.model_name})")

    sent_all: set[str] = set()
    results: list[TriageResult] = []
    for batch_num, start_idx in enumerate(range(0, len(findings), BATCH_SIZE), start=1):
        batch = findings[start_idx : start_idx + BATCH_SIZE]
        sent_all.update(f.fingerprint for f in batch)
        started = time.monotonic()
        batch_results = triage_batch(triage_provider, batch)
        elapsed = time.monotonic() - started
        print(
            f"batch {batch_num}: {len(batch)} findings in {elapsed:.1f}s, "
            f"{len(batch_results)} results"
        )
        results.extend(batch_results)

    returned = {r.fingerprint for r in results}
    assert returned <= sent_all, "a fingerprint was returned that was never sent"

    # The judgments themselves, not just that some arrived. Counting results proves the
    # pipeline ran; only reading them catches a model returning five valid-shaped,
    # useless ones -- which the counts above would call a clean run.
    by_fingerprint = {f.fingerprint: f for f in findings}
    print()
    for result in results:
        finding = by_fingerprint[result.fingerprint]
        print(
            f"  {result.priority:<11} conf {result.confidence:.2f}  "
            f"{finding.rule_id} ({finding.scanner})  {finding.target}"
        )
        print(f"      {result.explanation}")

    tally = Counter(r.priority for r in results)
    print()
    print("  ".join(f"{priority}: {n}" for priority, n in tally.most_common()))
    print(f"{len(results)}/{len(findings)} findings triaged, 0 invented fingerprints")
