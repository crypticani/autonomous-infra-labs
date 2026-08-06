from datetime import datetime, timedelta, timezone

import pytest
import requests

from connectors.alertmanager import (
    AlertmanagerError,
    fetch_alerts,
    merge,
    to_document,
)

FIRING = {
    "fingerprint": "a1b2c3d4e5f6",
    "startsAt": "2026-08-06T14:22:11.000Z",
    "endsAt": "2026-08-06T23:00:00.000Z",
    "updatedAt": "2026-08-06T14:52:11.000Z",
    "status": {"state": "active", "silencedBy": [], "inhibitedBy": []},
    "labels": {
        "alertname": "PostgresConnectionPoolExhausted",
        "severity": "critical",
        "instance": "appsrv:5432",
        "job": "postgres-exporter",
        "service": "postgres",
    },
    "annotations": {
        "summary": "connection pool utilisation above 90%",
        "description": "postgres-primary has held >90% pool utilisation for 10 minutes.",
    },
}

FIRING_META = {
    "doc_type": "alert",
    "source": "PostgresConnectionPoolExhausted",
    "fingerprint": "a1b2c3d4e5f6",
    "started_at": "2026-08-06T14:22:11.000Z",
    "severity": "critical",
    "instance": "appsrv:5432",
    "service": "postgres",
    "job": "postgres-exporter",
}

NOW = datetime(2026, 8, 6, 16, 0, 0, tzinfo=timezone.utc)


def indexed_from(docs):
    """Mimic what sync_alerts reads back out of Chroma: metadata keyed by fingerprint."""
    return {doc.metadata["fingerprint"]: dict(doc.metadata) for doc in docs}


# --- rendering -------------------------------------------------------------


def test_slug_is_the_fingerprint():
    assert to_document(FIRING, status="firing").slug == "alert-a1b2c3d4e5f6"


def test_text_carries_the_operator_facing_fields():
    text = to_document(FIRING, status="firing").text
    assert "Alert: PostgresConnectionPoolExhausted" in text
    assert "Status: firing" in text
    assert "Severity: critical" in text
    assert "connection pool utilisation above 90%" in text
    assert "Started: 2026-08-06T14:22:11.000Z" in text


def test_updated_at_never_reaches_the_document():
    """It changes on every poll; including it would re-embed everything every 60s."""
    doc = to_document(FIRING, status="firing")
    assert "14:52:11" not in doc.text
    assert "14:52:11" not in str(doc.metadata)


def test_metadata_is_the_discriminator_plus_filter_dimensions():
    meta = to_document(FIRING, status="firing").metadata
    assert meta["doc_type"] == "alert"
    assert meta["source"] == "PostgresConnectionPoolExhausted"
    assert meta["fingerprint"] == "a1b2c3d4e5f6"
    assert meta["status"] == "firing"
    assert meta["severity"] == "critical"
    assert meta["started_at"] == "2026-08-06T14:22:11.000Z"


def test_resolved_alert_states_it_in_the_text():
    """/ask-runbook pastes text, not metadata. A resolved alert whose text still says
    'firing' gets reported as a live incident, fluently and with a valid citation."""
    doc = to_document(FIRING, status="resolved", resolved_at="2026-08-06T15:04:02.000Z")
    assert "Status: resolved" in doc.text
    assert "Resolved: 2026-08-06T15:04:02.000Z" in doc.text
    assert doc.metadata["status"] == "resolved"
    assert doc.metadata["resolved_at"] == "2026-08-06T15:04:02.000Z"


def test_firing_alert_omits_resolved_at_rather_than_nulling_it():
    """Chroma rejects None metadata values."""
    meta = to_document(FIRING, status="firing").metadata
    assert "resolved_at" not in meta
    assert None not in meta.values()


def test_missing_optional_labels_do_not_crash():
    bare = {**FIRING, "labels": {"alertname": "Bare"}, "annotations": {}}
    doc = to_document(bare, status="firing")
    assert "Alert: Bare" in doc.text
    assert "severity" not in doc.metadata


# --- retention -------------------------------------------------------------


def test_a_live_alert_passes_straight_through():
    docs = merge([FIRING], indexed={}, now=NOW)
    assert [d.slug for d in docs] == ["alert-a1b2c3d4e5f6"]
    assert docs[0].metadata["status"] == "firing"


def test_an_alert_that_vanished_is_marked_resolved_and_kept():
    """Alertmanager drops resolved alerts from /api/v2/alerts. Absence is the signal."""
    was = indexed_from(merge([FIRING], indexed={}, now=NOW))
    docs = merge([], indexed=was, now=NOW)

    assert len(docs) == 1
    assert docs[0].metadata["status"] == "resolved"
    assert docs[0].metadata["resolved_at"] == NOW.isoformat()
    assert "Status: resolved" in docs[0].text


def test_a_resolved_alert_survives_the_retention_window():
    resolved_at = (NOW - timedelta(hours=23)).isoformat()
    indexed = {
        "a1b2c3d4e5f6": {
            **FIRING_META,
            "status": "resolved",
            "resolved_at": resolved_at,
        }
    }
    assert len(merge([], indexed=indexed, now=NOW)) == 1


def test_a_resolved_alert_expires_after_the_window():
    resolved_at = (NOW - timedelta(hours=25)).isoformat()
    indexed = {
        "a1b2c3d4e5f6": {
            **FIRING_META,
            "status": "resolved",
            "resolved_at": resolved_at,
        }
    }
    # Falls out of `desired`, so plan_reconcile deletes it.
    assert merge([], indexed=indexed, now=NOW) == []


def test_a_resolved_alert_is_not_restamped_into_immortality():
    """Taking `now` every poll would keep it forever 'just resolved'."""
    resolved_at = (NOW - timedelta(hours=10)).isoformat()
    indexed = {
        "a1b2c3d4e5f6": {
            **FIRING_META,
            "status": "resolved",
            "resolved_at": resolved_at,
        }
    }
    docs = merge([], indexed=indexed, now=NOW)
    assert docs[0].metadata["resolved_at"] == resolved_at


def test_resyncing_a_resolved_alert_is_byte_identical():
    """The churn test. Rebuilt metadata must not fold in the previous content_hash or
    indexed_at, or every poll re-embeds every resolved alert forever."""
    was = indexed_from(merge([FIRING], indexed={}, now=NOW))
    first = merge([], indexed=was, now=NOW)[0]

    # What Chroma actually hands back: ingest's enrich() has stamped these on.
    stored = {
        **first.metadata,
        "content_hash": "deadbeef",
        "indexed_at": "2026-08-06",
        "chunk_index": 0,
    }
    second = merge(
        [], indexed={"a1b2c3d4e5f6": stored}, now=NOW + timedelta(minutes=5)
    )[0]

    assert second.text == first.text
    assert second.metadata == first.metadata


def test_an_alert_that_comes_back_is_firing_again():
    resolved = indexed_from(
        merge([], indexed=indexed_from(merge([FIRING], {}, NOW)), now=NOW)
    )
    docs = merge([FIRING], indexed=resolved, now=NOW + timedelta(hours=1))
    assert docs[0].metadata["status"] == "firing"
    assert "resolved_at" not in docs[0].metadata


def test_a_silenced_alert_is_firing_not_resolved():
    """It is still firing; someone muted the notification. Reporting it resolved
    would be a lie about the state of the system."""
    silenced = {
        **FIRING,
        "status": {"state": "suppressed", "silencedBy": ["abc"], "inhibitedBy": []},
    }
    docs = merge([silenced], indexed={}, now=NOW)
    assert docs[0].metadata["status"] == "silenced"
    assert "Status: silenced" in docs[0].text


def test_an_alert_with_a_past_endsat_is_resolved_immediately():
    ended = {**FIRING, "endsAt": "2026-08-06T15:00:00.000Z"}
    docs = merge([ended], indexed={}, now=NOW)
    assert docs[0].metadata["status"] == "resolved"
    assert docs[0].metadata["resolved_at"] == "2026-08-06T15:00:00.000Z"


def test_alertmanagers_zero_endsat_is_not_a_resolution():
    """Alertmanager writes 0001-01-01T00:00:00Z when there is no end time. Naively
    compared against now, that reads as 'resolved two thousand years ago'."""
    zero = {**FIRING, "endsAt": "0001-01-01T00:00:00Z"}
    docs = merge([zero], indexed={}, now=NOW)
    assert docs[0].metadata["status"] == "firing"


# --- fetch -----------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = [] if payload is None else payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


def test_a_successful_fetch_returns_the_alerts(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse([FIRING]))
    assert fetch_alerts("http://am:9093") == [FIRING]


def test_a_successful_empty_response_is_trusted(monkeypatch):
    """A quiet cluster is a real state and must stay distinguishable from a failure."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse([]))
    assert fetch_alerts("http://am:9093") == []


def test_an_http_error_raises_rather_than_returning_empty(monkeypatch):
    """The data-loss path: [] on failure marks every indexed alert resolved."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(status=500))
    with pytest.raises(AlertmanagerError):
        fetch_alerts("http://am:9093")


def test_a_timeout_raises(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.Timeout("too slow")

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(AlertmanagerError):
        fetch_alerts("http://am:9093")


def test_connection_refused_raises(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(AlertmanagerError):
        fetch_alerts("http://am:9093")


def test_a_resolved_alert_keeps_its_summary_and_description():
    """Rebuilt from metadata, a resolved alert must not lose the sentence that
    carries the actual information -- 'root filesystem is 79.3% full' is what a
    question about last night's alerts needs to match."""
    was = indexed_from(merge([FIRING], indexed={}, now=NOW))
    doc = merge([], indexed=was, now=NOW)[0]
    assert "Summary: connection pool utilisation above 90%" in doc.text
    assert "Description: postgres-primary has held" in doc.text


def test_a_stale_resolved_at_on_a_firing_alert_is_ignored():
    """Chroma's upsert MERGES metadata, so resolved_at survives every later write
    that omits it. A flapping alert -- resolve, re-fire, resolve again -- would
    otherwise read back the first resolution and expire instantly."""
    stale = (NOW - timedelta(hours=40)).isoformat()
    indexed = {
        "a1b2c3d4e5f6": {**FIRING_META, "status": "firing", "resolved_at": stale}
    }
    docs = merge([], indexed=indexed, now=NOW)

    assert len(docs) == 1, "a 40h-stale resolved_at must not expire a live alert"
    assert docs[0].metadata["resolved_at"] == NOW.isoformat()
