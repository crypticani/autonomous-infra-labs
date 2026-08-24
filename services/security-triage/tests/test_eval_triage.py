"""Day 28. The eval's own grading logic, which is the part that can be wrong in a way
nobody notices -- an eval that passes everything is indistinguishable from a healthy
model until the day it matters.

Day 27 has a worked example of exactly this: two prompt changes were made off eval
movements that turned out to be the checker's bug and batch nondeterminism, not the
model. So the checker gets tests before it gets trusted.
"""

import json
from pathlib import Path

import pytest

from eval_triage import EVAL_SET, ORDER, band, grade


def case(**bounds):
    return {"label": "a finding", "why": "because", **bounds}


@pytest.mark.parametrize(
    "bounds,priority,expected",
    [
        # A floor: anything at or above it is in band, anything below is the regression
        # this whole file exists to catch.
        ({"min": "high"}, "critical", True),
        ({"min": "high"}, "high", True),
        ({"min": "high"}, "medium", False),
        ({"min": "high"}, "low", False),
        # A ceiling: the noise cases. Under-calling noise is always fine.
        ({"max": "low"}, "low", True),
        ({"max": "low"}, "medium", False),
        ({"max": "medium"}, "medium", True),
        ({"max": "medium"}, "critical", False),
        # Both bounds, for the findings with a defensible answer on each side.
        ({"min": "medium", "max": "high"}, "high", True),
        ({"min": "medium", "max": "high"}, "critical", False),
        ({"min": "medium", "max": "high"}, "low", False),
    ],
)
def test_bands_pass_what_is_defensible_and_fail_what_is_not(bounds, priority, expected):
    passed, _ = grade(case(**bounds), priority)
    assert passed is expected


def test_declining_a_serious_finding_is_a_miss():
    """A model that answers `needs_human` to everything satisfies every guard in
    triage.py and produces a run that looks clean. Day 23 shipped exactly that from a
    1.5b model. It has to fail here or the eval is decoration.
    """
    passed, why = grade(case(min="high"), "needs_human")
    assert passed is False
    assert "high" in why


def test_declining_a_noise_finding_is_allowed():
    """The other half of the rule, and it is not leniency: a declined noise finding
    still reaches a human through `review_required`, which is where it belonged. Only a
    case that claims the finding is definitely serious can be failed by a decline.
    """
    passed, _ = grade(case(max="low"), "needs_human")
    assert passed is True


def test_a_finding_the_model_never_answered_about_fails():
    """Not an absent measurement -- a wrong one. The run's own counts would call this a
    success, because triage.py drops unsent fingerprints silently and returns what is
    left.
    """
    passed, why = grade(case(min="high"), None)
    assert passed is False
    assert "no result" in why


def test_needs_human_is_not_on_the_severity_scale():
    """If it were ranked, `needs_human` would satisfy a `max` bound by comparing as some
    severity -- which is how a declined judgment would start counting as a correct one.
    """
    assert "needs_human" not in ORDER


def test_the_committed_eval_set_asserts_something_on_every_case():
    """A case with neither bound is a row in the table that can never fail. The runner
    exits 2 on one; this catches it at commit time instead of at 3am.
    """
    cases = json.loads(Path(EVAL_SET).read_text(encoding="utf-8"))
    assert cases, "the eval set is empty"
    for c in cases:
        assert "min" in c or "max" in c, f"{c['label']!r} asserts nothing"
        assert c["why"].strip(), f"{c['label']!r} has no stated reason"
        for bound in ("min", "max"):
            if bound in c:
                assert c[bound] in ORDER, f"{c['label']!r} has a bogus {bound}"


def test_band_renders_each_bound_shape():
    assert band(case(min="high")) == ">= high"
    assert band(case(max="low")) == "<= low"
    assert band(case(min="medium", max="high")) == "medium..high"
