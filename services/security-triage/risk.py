"""One score and one verdict out of a pile of judgments.

A **weighted sum capped at 100**, not worst-finding-wins:

    1 critical                        ->  40   fails at the default bar
    3 highs, no critical              ->  45   fails
    40 mediums, no critical, no high  -> 100   fails
    2 lows                            ->   2   passes

Worst-finding-wins would score the last two rows 40 and 10 -- one medium and two hundred
mediums identical -- which is the difference between "no criticals" and "safe". The cap
exists because past 100 a repo at 340 and one at 980 need the same response.

`confidence` is not in the formula: measured flat and uncalibrated at every model size,
so weighting by it buys nothing and disguises the score's provenance.

`needs_human` scores zero and is counted separately -- a declined judgment is not a
low-risk one.

# ponytail: it is reported via `review_required` and does not itself fail the gate, so a
# run that declined everything still returns verdict="pass". Ceiling: a repo reading only
# the exit status learns nothing from those findings. Upgrade path is its own threshold,
# once there is a measured baseline for what a normal rate looks like.

`assess` and `top_findings` are separate: the score is policy over judgments alone, the
ranked list is presentation and has to join judgments back to their findings.
"""

import os
from typing import Literal

from pydantic import BaseModel

from scanners import Finding
from triage import TriageResult

WEIGHTS = {"critical": 40, "high": 15, "medium": 4, "low": 1}

# 40: one critical fails, three highs (45) fail, a scattering of lows does not. A
# number, not a discovery -- hence per-repo overridable.
THRESHOLD = int(os.getenv("ST_RISK_THRESHOLD", "40"))

# Beyond this a comment stops being read; the run record has the rest.
TOP_N = int(os.getenv("ST_TOP_FINDINGS", "10"))

# Within one priority: most exploitable and highest blast radius first.
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
    """Score and verdict. A threshold of 0 fails everything, which is a legal bar."""
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

    An unmatched fingerprint is dropped rather than rendered with blanks; triage.py already
    refuses unsent ones, so this only fires if the lists came from different runs.
    needs_human weighs 0, so it appears only when there is room.
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
