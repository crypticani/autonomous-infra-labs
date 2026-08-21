from risk import TOP_N, WEIGHTS, assess, top_findings
from scanners import Finding
from triage import TriageResult


def _result(
    priority="high", fingerprint=None, exploitability="medium", impact="medium"
):
    return TriageResult(
        fingerprint=fingerprint or f"fp-{priority}-{exploitability}-{impact}",
        priority=priority,
        exploitability=exploitability,
        impact=impact,
        explanation="reachable from an unauthenticated endpoint",
        confidence=0.8,
    )


def _finding(fingerprint, **overrides):
    fields = dict(
        scanner="trivy",
        rule_id="CVE-2026-45829",
        title="chromadb: arbitrary code execution",
        target="services/knowledge-copilot/requirements.txt",
        line=12,
        fingerprint=fingerprint,
    )
    fields.update(overrides)
    return Finding(**fields)


def test_score_is_the_weighted_sum():
    assessment = assess([_result("high", "a"), _result("medium", "b")])
    assert assessment.score == WEIGHTS["high"] + WEIGHTS["medium"]


def test_one_critical_fails_the_default_bar():
    assert assess([_result("critical", "a")]).verdict == "fail"


def test_volume_fails_without_a_single_critical_or_high():
    """The whole reason the score is a sum. Worst-finding-wins would pass this."""
    assessment = assess([_result("medium", f"fp{i}") for i in range(40)])
    assert assessment.counts == {
        "critical": 0,
        "high": 0,
        "medium": 40,
        "low": 0,
        "needs_human": 0,
    }
    assert assessment.score == 100
    assert assessment.verdict == "fail"


def test_score_is_capped_at_100():
    assert assess([_result("critical", f"fp{i}") for i in range(50)]).score == 100


def test_a_scattering_of_lows_passes():
    assessment = assess([_result("low", f"fp{i}") for i in range(5)])
    assert assessment.score == 5
    assert assessment.verdict == "pass"


def test_a_clean_run_scores_zero_and_passes():
    assessment = assess([])
    assert (assessment.score, assessment.verdict) == (0, "pass")
    assert assessment.review_required is False


def test_needs_human_scores_nothing_but_is_counted():
    assessment = assess([_result("needs_human", f"fp{i}") for i in range(20)])
    assert assessment.score == 0
    assert assessment.counts["needs_human"] == 20
    assert assessment.review_required is True


def test_per_request_threshold_overrides_the_deploy_default():
    results = [_result("high", "a")]  # 15
    assert assess(results, threshold=10).verdict == "fail"
    assert assess(results, threshold=90).verdict == "pass"


def test_a_zero_threshold_fails_a_clean_run():
    """A legal bar for a repo to ask for, and not this module's job to argue with."""
    assert assess([], threshold=0).verdict == "fail"


def test_top_is_ordered_by_priority_then_exploitability_and_impact():
    results = [
        _result("low", "low"),
        _result("critical", "crit"),
        _result("high", "high-lo", exploitability="low", impact="low"),
        _result("high", "high-hi", exploitability="high", impact="high"),
        _result("needs_human", "unjudged"),
    ]
    findings = [_finding(r.fingerprint) for r in results]
    assert [row.fingerprint for row in top_findings(results, findings)] == [
        "crit",
        "high-hi",
        "high-lo",
        "low",
        "unjudged",
    ]


def test_top_carries_what_a_comment_needs():
    """A fingerprint in a PR comment tells a reviewer nothing -- the join is the point."""
    results = [_result("critical", "a")]
    findings = [_finding("a", rule_id="B105", scanner="bandit", target="x.py", line=3)]
    row = top_findings(results, findings)[0]
    assert row.rule_id == "B105"
    assert row.scanner == "bandit"
    assert (row.target, row.line) == ("x.py", 3)
    assert row.priority == "critical"


def test_a_judgment_with_no_matching_finding_is_dropped_not_blanked():
    assert top_findings([_result("critical", "ghost")], []) == []


def test_top_is_stable_for_an_identical_corpus():
    """Two runs over the same findings must produce the same comment -- an unstable
    top-N reads as a change in the codebase when nothing changed."""
    results = [_result("medium", f"fp{i}") for i in range(20)]
    findings = [_finding(r.fingerprint) for r in results]
    assert [r.fingerprint for r in top_findings(results, findings)] == [
        r.fingerprint for r in top_findings(list(reversed(results)), findings)
    ]


def test_top_is_capped():
    results = [_result("high", f"fp{i}") for i in range(TOP_N + 5)]
    findings = [_finding(r.fingerprint) for r in results]
    assert len(top_findings(results, findings)) == TOP_N
    assert assess(results).counts["high"] == TOP_N + 5
