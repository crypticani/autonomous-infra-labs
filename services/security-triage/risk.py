"""One score and one verdict out of a pile of judgments -- Day 25.

A risk threshold is a policy decision, and this module exists to make that decision
explicit and per-repo rather than leave it implicit in "did any scanner say CRITICAL".
"No criticals" and "safe" are different claims: a service with forty medium findings
across its dependency tree is not safer than one with a single critical in a file
nobody reaches, it is just easier to describe.

So the score is a **weighted sum, capped at 100**, not worst-finding-wins:

    1 critical                        ->  40   fails at the default bar
    3 highs, no critical              ->  45   fails
    40 mediums, no critical, no high  -> 100   fails
    2 lows                            ->   2   passes

Worst-finding-wins would score the last two rows 40 and 10 -- one medium and two
hundred mediums identical -- which is precisely the failure this day is about. The cap
exists because past 100 the number stops meaning anything; a repo at 340 and a repo at
980 both need the same response, which is "stop and look".

`confidence` is deliberately **not** in the formula. Day 23 measured it flat and
uncalibrated at every model size tried -- 0.8 on a judgment the model had no business
making and 0.8 again on an obvious one. Weighting a score by a number that does not
vary buys nothing and disguises the score's provenance; `priority` is the only judgment
the model demonstrably makes.

`needs_human` scores zero and is counted separately. It is a declined judgment, not a
low-risk one, and inventing a weight for it would mean a run where the model gave up on
everything scoring like a clean one.

# ponytail: needs_human is reported via `review_required`, not scored, and does not by
# itself fail the gate -- so a run that declined every finding still returns
# verdict="pass" with review_required=True next to it. Ceiling: a repo that ignores the
# comment body and reads only the exit status learns nothing from those findings.
# Upgrade path: its own threshold (ST_MAX_NEEDS_HUMAN), once Day 27's benchmark says
# what a normal needs_human rate even looks like -- setting that bar before measuring it
# would just be a number picked to make the demo pass.

`assess` and `top_findings` are separate on purpose. The score is a policy calculation
over judgments alone and nothing else belongs in it; the ranked list is a presentation
concern that has to join judgments back to the findings they were about, because a PR
comment full of sixteen-character fingerprints tells a reviewer nothing.
"""

import os
from typing import Literal

from pydantic import BaseModel

from scanners import Finding
from triage import TriageResult

WEIGHTS = {"critical": 40, "high": 15, "medium": 4, "low": 1}

# 40, so exactly one critical fails a build and three highs (45) do too, while a
# scattering of lows and mediums does not. A number, not a discovery -- which is the
# point of it being per-repo overridable: a repo that ships a public API and one that
# ships a cron script have genuinely different bars, and neither should have to argue
# with this default.
THRESHOLD = int(os.getenv("ST_RISK_THRESHOLD", "40"))

# How many findings the PR comment can usefully carry. Beyond this the comment stops
# being read, and the run record has the counts for the rest.
TOP_N = int(os.getenv("ST_TOP_FINDINGS", "10"))

# Within one priority: the most exploitable and highest blast radius first, so the top
# of the comment is the part worth reading first rather than whatever the scanner
# happened to emit first.
_RANK = {"high": 2, "medium": 1, "low": 0}


class RiskAssessment(BaseModel):
    score: int
    threshold: int
    verdict: Literal["pass", "fail"]
    counts: dict[str, int]
    review_required: bool


class TopFinding(BaseModel):
    """One row of the PR comment: the judgment, plus enough of the finding to act on."""

    fingerprint: str
    priority: str
    explanation: str
    scanner: str
    rule_id: str
    title: str
    target: str
    line: int | None = None


def assess(results: list[TriageResult], threshold: int | None = None) -> RiskAssessment:
    """Score and verdict.

    `threshold` is the caller's bar when they set one, this deploy's default otherwise.
    A threshold of 0 fails everything including a clean run, which is a legal thing for
    a repo to ask for and not this module's business to second-guess.
    """
    threshold = THRESHOLD if threshold is None else threshold

    counts = {priority: 0 for priority in (*WEIGHTS, "needs_human")}
    for result in results:
        counts[result.priority] += 1

    score = min(100, sum(WEIGHTS.get(r.priority, 0) for r in results))
    return RiskAssessment(
        score=score,
        threshold=threshold,
        verdict="fail" if score >= threshold else "pass",
        counts=counts,
        review_required=counts["needs_human"] > 0,
    )


def _sort_key(result: TriageResult) -> tuple:
    return (
        -WEIGHTS.get(result.priority, 0),
        -(_RANK[result.exploitability] + _RANK[result.impact]),
        # Ties broken on the fingerprint so the same corpus always produces the same
        # comment -- an unstable top-N makes two identical runs look like a change.
        result.fingerprint,
    )


def top_findings(
    results: list[TriageResult], findings: list[Finding], n: int | None = None
) -> list[TopFinding]:
    """The worst judgments, joined back to what they were judgments *about*.

    A result whose fingerprint matches no finding is dropped rather than rendered with
    blanks. triage.py already refuses fingerprints that were never sent, so this can
    only fire if the two lists came from different runs -- in which case the honest
    output is nothing, not a row claiming a rule id it made up.

    needs_human weighs 0, so those sort last and appear only when there is room --
    present, because "the model could not judge these" is something a reviewer should
    see, but never crowding out a critical.
    """
    n = TOP_N if n is None else n
    by_fingerprint = {f.fingerprint: f for f in findings}

    rows = []
    for result in sorted(results, key=_sort_key):
        finding = by_fingerprint.get(result.fingerprint)
        if finding is None:
            continue
        rows.append(
            TopFinding(
                fingerprint=result.fingerprint,
                priority=result.priority,
                explanation=result.explanation,
                scanner=finding.scanner,
                rule_id=finding.rule_id,
                title=finding.title,
                target=finding.target,
                line=finding.line,
            )
        )
        if len(rows) == n:
            break
    return rows
