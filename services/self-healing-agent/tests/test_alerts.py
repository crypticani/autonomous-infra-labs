import alerts
from conftest import metric


def payload(*alert_list, status="firing"):
    """An Alertmanager v4 webhook body, with only the fields this module reads."""
    return {
        "version": "4",
        "groupKey": '{}:{alertname="PodOOMKilled"}',
        "status": status,
        "alerts": list(alert_list),
    }


def an_alert(status="firing", fingerprint="abc123", **labels):
    return {
        "status": status,
        "labels": {"alertname": "PodOOMKilled", "namespace": "sandbox", **labels},
        "annotations": {"summary": "checkout-api was OOMKilled"},
        "startsAt": "2026-08-16T02:30:00.000Z",
        "fingerprint": fingerprint,
    }


def test_a_firing_alert_is_accepted():
    intake = alerts.accept(payload(an_alert()))

    assert len(intake.accepted) == 1
    assert intake.resolved == 0
    assert intake.duplicate == 0


def test_the_alert_is_passed_through_unchanged():
    # Not reshaped. The loop stringifies whatever it is given, and every field dropped
    # here is a field the model cannot reason about -- `namespace` and `pod` above all,
    # since those are the arguments get_pod_logs needs to say anything useful.
    original = an_alert(pod="checkout-api-7f9d")

    intake = alerts.accept(payload(original))

    assert intake.accepted[0] == original


def test_a_resolved_alert_is_skipped():
    # Alertmanager sends the same webhook when an alert clears. Diagnosing a problem that
    # has already stopped is a model call spent on nothing.
    intake = alerts.accept(payload(an_alert(status="resolved")))

    assert intake.accepted == ()
    assert intake.resolved == 1


def test_a_group_of_alerts_is_accepted_alert_by_alert():
    intake = alerts.accept(
        payload(
            an_alert(fingerprint="aaa"),
            an_alert(status="resolved", fingerprint="bbb"),
            an_alert(fingerprint="ccc"),
        )
    )

    assert len(intake.accepted) == 2
    assert intake.resolved == 1


def test_the_same_alert_twice_is_diagnosed_once():
    # The guard that matters most today. Alertmanager re-sends a firing group every
    # group_interval -- five minutes by default -- until it resolves. Without this, one
    # flapping alert is a fresh diagnosis every five minutes, and the free tier is gone
    # before anyone reads the first proposal.
    alerts.accept(payload(an_alert()))

    intake = alerts.accept(payload(an_alert()))

    assert intake.accepted == ()
    assert intake.duplicate == 1


def test_the_same_alert_is_diagnosed_again_after_the_ttl():
    # Suppression, not amnesia. An alert still firing an hour later has outlived the
    # first proposal's TTL, and is worth looking at again.
    alerts.accept(payload(an_alert()), now=1000.0)

    intake = alerts.accept(payload(an_alert()), now=1000.0 + alerts.DEDUP_TTL + 1)

    assert len(intake.accepted) == 1


def test_alerts_without_a_fingerprint_dedupe_on_their_labels():
    # Older Alertmanagers omit it. Falling back to identity would mean no dedupe at all,
    # which is the one failure mode this module exists to prevent.
    bare = an_alert()
    del bare["fingerprint"]
    alerts.accept(payload(bare))

    intake = alerts.accept(payload(dict(bare)))

    assert intake.accepted == ()
    assert intake.duplicate == 1


def test_different_alerts_are_not_confused_by_the_label_fallback():
    first, second = an_alert(pod="api-1"), an_alert(pod="api-2")
    del first["fingerprint"], second["fingerprint"]

    intake = alerts.accept(payload(first, second))

    assert len(intake.accepted) == 2


def test_a_body_with_no_alerts_is_not_an_error():
    # A trust boundary: this is an unparsed POST from outside the process. A KeyError
    # here is a 500 that makes Alertmanager retry a body that will never work.
    assert alerts.accept({"version": "4"}).accepted == ()
    assert alerts.accept({"alerts": None}).accepted == ()
    assert alerts.accept({"alerts": "not-a-list"}).accepted == ()


def test_an_alert_that_is_not_a_dict_is_dropped_rather_than_raising():
    intake = alerts.accept({"alerts": ["nonsense", an_alert()]})

    assert len(intake.accepted) == 1


def test_every_outcome_is_counted_under_its_own_label():
    # The label, not the total. "Some alerts arrived" answers nothing; `duplicate`
    # climbing while `accepted` stays flat is the healthy shape for a flapping alert,
    # and the two rising together is the model budget draining.
    before = {
        outcome: metric("sha_alerts_received_total", outcome=outcome)
        for outcome in ("accepted", "resolved", "duplicate")
    }
    alerts.accept(payload(an_alert(fingerprint="dup")))

    alerts.accept(
        payload(
            an_alert(fingerprint="dup"),
            an_alert(fingerprint="new"),
            an_alert(fingerprint="gone", status="resolved"),
        )
    )

    assert (
        metric("sha_alerts_received_total", outcome="accepted")
        == before["accepted"] + 2
    )
    assert (
        metric("sha_alerts_received_total", outcome="resolved")
        == before["resolved"] + 1
    )
    assert (
        metric("sha_alerts_received_total", outcome="duplicate")
        == before["duplicate"] + 1
    )
