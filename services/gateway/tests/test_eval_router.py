"""The eval's own grading, checked without a model.

Worth its own file because the grading is the eval's whole claim. A scorer that quietly
gave both degenerate routers a good mark would keep printing a number and the number would
mean nothing -- and nothing else in the suite reads eval_set.json.
"""

import json

import pytest

import backends
from eval_router import EVAL_SET, grade, load_cases, report

CASES = json.loads(EVAL_SET.read_text(encoding="utf-8"))


def _case(expect):
    return {"label": "x", "question": "y", "expect": expect}


# --- grading ---


def test_the_expected_service_passes():
    assert grade(_case("log-analyzer"), "log-analyzer") == (True, "")


def test_declining_a_definite_question_is_a_miss():
    passed, why = grade(_case("log-analyzer"), None)
    assert not passed
    assert "declined a definite" in why


def test_answering_a_vague_question_is_a_miss():
    passed, why = grade(_case(None), "security-triage")
    assert not passed
    assert "nothing was confident enough" in why


def test_declining_a_vague_question_passes():
    assert grade(_case(None), None)[0]


def test_a_list_accepts_any_of_its_members():
    case = _case(["log-analyzer", "self-healing-agent", None])
    assert grade(case, "log-analyzer")[0]
    assert grade(case, "self-healing-agent")[0]
    assert grade(case, None)[0]
    assert not grade(case, "security-triage")[0]


def test_routing_to_the_wrong_service_is_a_miss():
    passed, why = grade(_case("log-analyzer"), "knowledge-copilot")
    assert not passed
    assert "wanted log-analyzer" in why


# --- the set itself, which is the part that makes a score mean something ---


def test_both_degenerate_routers_fail_about_half_the_set():
    """The reason a score here is worth reading. A router that names a service for
    everything and one that declines everything are both easy to ship by accident, and both
    have to be punished or the number is decoration.
    """
    always_declines = sum(grade(c, None)[0] for c in CASES)
    always_guesses = sum(grade(c, "knowledge-copilot")[0] for c in CASES)

    assert always_declines < len(CASES) * 0.6
    assert always_guesses < len(CASES) * 0.6


def test_the_set_has_definite_and_vague_cases_in_both_directions():
    definite = [c for c in CASES if c["expect"] is not None]
    vague = [c for c in CASES if c["expect"] is None]

    # An eval of only clear cases cannot detect a router that never declines, which is the
    # whole reason the null rows exist.
    assert len(vague) >= 4
    assert len(definite) >= 12


def test_every_service_is_represented():
    """A backend nothing routes to is a backend whose description never gets graded."""
    named = {c["expect"] for c in CASES if isinstance(c["expect"], str)}
    assert named == set(backends.NAMES)


def test_the_keyword_trap_pairs_expect_different_services():
    """Two questions about the same subject that differ only in what they ask for. If a
    router passes the rest of the set and fails these, it is matching words rather than
    intent -- and the prompt has a rule against exactly that.
    """
    by_label = {c["label"]: c for c in CASES}
    assert by_label["discrimination pair A: what does the log mean"]["expect"] == (
        "log-analyzer"
    )
    assert by_label["discrimination pair B: what do I do about it"]["expect"] == (
        "self-healing-agent"
    )
    assert by_label["keyword trap: runbook question using a log word"]["expect"] == (
        "knowledge-copilot"
    )
    assert by_label["keyword trap: log question about the same word"]["expect"] == (
        "log-analyzer"
    )


def test_labels_are_unique():
    """The table is read by label, so two rows sharing one is a report you cannot act on."""
    labels = [c["label"] for c in CASES]
    assert len(set(labels)) == len(labels)


def test_every_question_is_long_enough_for_the_endpoint_to_accept():
    """AskRequest sets min_length=10, so a case shorter than that grades a route the real
    /ask would have 422'd before reaching the router."""
    for case in CASES:
        assert len(case["question"]) >= 10, case["label"]


def test_load_cases_rejects_a_service_that_does_not_exist(tmp_path):
    """A case naming a retired service would otherwise fail forever for a reason that looks
    like a model regression."""
    bad = tmp_path / "eval_set.json"
    bad.write_text(json.dumps([{"label": "x", "question": "y", "expect": "grafana"}]))

    with pytest.raises(SystemExit) as caught:
        load_cases(bad)
    assert caught.value.code == 2


def test_load_cases_accepts_the_committed_set():
    assert len(load_cases(EVAL_SET)) == len(CASES)


# --- the report, which nothing covered until it had broken three times ---


def _row(
    expect, routed, confidence, passed, why="", reason="", error=None, declined_by=None
):
    return {
        "case": {
            "label": f"a case expecting {expect}",
            "question": "q",
            "expect": expect,
        },
        "passed": passed,
        "why": why,
        "routed": routed,
        "error": error,
        "declined_by": declined_by,
        "confidence": confidence,
        "reason": reason,
        "seconds": 1.0,
    }


def test_the_report_renders_every_kind_of_row():
    """`report()` has now broken three times -- an errored row printed as a decline, a
    `why` column that wrapped a connection traceback across fifteen lines, and a level
    formatted with `:.2f` after confidence stopped being a float. Each one got through
    because nothing in the suite ever called this function. This is the cheapest thing that
    fails the next time.
    """
    rows = [
        _row(
            "log-analyzer", "log-analyzer", "high", True, reason="asks what a log means"
        ),
        _row(
            "self-healing-agent",
            None,
            "low",
            False,
            why="declined a definite self-healing-agent question",
            reason="scaling question",
        ),
        # The two kinds of decline, which the table showed identically until the run that
        # made it matter: the model saying it cannot place the question, versus the floor
        # overruling a service it named too tentatively. Only the second is GW_ROUTE_ON's
        # doing, so only the second changes if the bar moves.
        _row(None, None, "low", True, reason="vague", declined_by="none"),
        _row(None, None, "low", True, reason="a guess", declined_by="floor"),
        _row(
            ["log-analyzer", "self-healing-agent", None],
            "log-analyzer",
            "high",
            True,
            reason="asks about log text",
        ),
        # The outage row: no level and no reason, which is the shape that printed as a
        # decline and made an unreachable backend look like a cautious model.
        _row(
            "security-triage",
            None,
            None,
            False,
            why="GatewayProviderError: unreachable",
            error="GatewayProviderError: unreachable",
        ),
    ]

    assert report(rows, elapsed=12.0, tokens=(1000, 100)) is False


def test_the_report_survives_a_run_where_nothing_reached_the_model():
    """Every row errored, so `graded` is empty -- which is a median over no samples and a
    ratio with a zero denominator if either is written carelessly."""
    rows = [_row("log-analyzer", None, None, False, why="boom", error="boom")]
    assert report(rows, elapsed=1.0, tokens=(0, 0)) is False


def test_the_report_returns_true_only_when_every_row_passed():
    rows = [
        _row("log-analyzer", "log-analyzer", "high", True, reason="a reason"),
        _row(None, None, "low", True, reason="another"),
    ]
    assert report(rows, elapsed=2.0, tokens=(10, 5)) is True
