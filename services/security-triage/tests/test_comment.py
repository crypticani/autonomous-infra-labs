from comment import render


def _run(**overrides):
    run = {
        "id": "abc123def456",
        "status": "done",
        "commit": "0123456789abcdef",
        "findings_raw": 629,
        "findings": 559,
        "triaged": 559,
        "risk": {
            "score": 55,
            "threshold": 40,
            "verdict": "fail",
            "counts": {
                "critical": 1,
                "high": 1,
                "medium": 0,
                "low": 0,
                "needs_human": 0,
            },
            "review_required": False,
        },
        "top": [
            {
                "priority": "critical",
                "rule_id": "B105",
                "scanner": "bandit",
                "target": "./services/x/settings.py",
                "line": 3,
                "explanation": "a credential in source",
            }
        ],
        "fixes": [],
        "error": None,
    }
    run.update(overrides)
    return run


def test_a_failing_run_is_marked_failing():
    body = render(_run())
    assert "❌" in body
    assert "score **55**" in body
    assert "/ threshold 40" in body


def test_a_passing_run_is_marked_passing():
    risk = {**_run()["risk"], "score": 5, "verdict": "pass"}
    body = render(_run(risk=risk))
    assert "✅" in body
    assert "❌" not in body


def test_the_table_carries_rule_and_location_not_fingerprints():
    body = render(_run())
    assert "| critical | `B105` (bandit) | `./services/x/settings.py:3` |" in body


def test_a_finding_with_no_line_renders_the_path_alone():
    top = [{**_run()["top"][0], "line": None}]
    assert "`./services/x/settings.py` |" in render(_run(top=top))


def test_zero_counts_are_left_out():
    body = render(_run())
    assert "**critical** 1" in body
    assert "medium" not in body


def test_needs_human_gets_its_own_callout():
    counts = {**_run()["risk"]["counts"], "needs_human": 5}
    risk = {**_run()["risk"], "counts": counts, "review_required": True}
    body = render(_run(risk=risk))
    assert "5 finding(s) the model declined to judge" in body
    assert "a passing verdict does not cover them" in body


def test_only_diffs_reach_the_comment():
    fixes = [
        {"kind": "advice", "diff": None, "note": "no fixer for this rule"},
        {"kind": "diff", "diff": "--- a/k8s/x.yaml\n+++ b/k8s/x.yaml\n", "note": ""},
    ]
    body = render(_run(fixes=fixes))
    assert "1 proposed fix(es)" in body
    assert "```diff" in body
    assert "no fixer for this rule" not in body


def test_a_failed_run_does_not_read_like_a_pass():
    """The one that matters. A comment saying nothing after a crashed run is
    indistinguishable from a clean bill of health to whoever reads only the check mark.
    """
    body = render(
        _run(status="failed", error="ollama: the model took too long", risk=None)
    )
    assert "could not finish" in body
    assert "ollama: the model took too long" in body
    assert "absence of one" in body
    assert "✅" not in body


def test_every_comment_names_its_run_and_commit():
    assert "run `abc123def456`" in render(_run())
    assert "commit `01234567`" in render(_run())
