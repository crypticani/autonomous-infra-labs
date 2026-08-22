import json

from runtime import build_envelope, read_events
from scanners import dedupe, parse_envelope


def _event(name="flaky-app-7d9", verb="create", stamp="2026-08-21T13:40:12.000000Z"):
    return {
        "level": "Metadata",
        "stage": "ResponseComplete",
        "verb": verb,
        "user": {"username": "kubernetes-admin"},
        "objectRef": {
            "resource": "pods",
            "subresource": "exec",
            "namespace": "sandbox",
            "name": name,
        },
        "stageTimestamp": stamp,
        "annotations": {"authorization.k8s.io/decision": "allow"},
    }


def _write_log(tmp_path, *lines):
    path = tmp_path / "audit.log"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def test_reads_one_event_per_line(tmp_path):
    path = _write_log(tmp_path, json.dumps(_event("a")), json.dumps(_event("b")))

    events, malformed = read_events(path)

    assert len(events) == 2
    assert malformed == 0


def test_a_torn_last_line_is_counted_not_fatal(tmp_path):
    # What a log captured while the API server is still appending actually looks like.
    path = _write_log(
        tmp_path, json.dumps(_event("a")), "", '{"level":"Metadata","stage":"Resp'
    )

    events, malformed = read_events(path)

    assert len(events) == 1
    # Counted rather than swallowed: one torn line is normal, a file of them means the
    # wrong file or a policy that never loaded, and a silent skip makes those identical.
    assert malformed == 1


def test_only_the_newest_events_are_kept(tmp_path):
    path = _write_log(tmp_path, *(json.dumps(_event(f"pod-{i}")) for i in range(5)))

    events, _ = read_events(path, max_events=2)

    assert [e["objectRef"]["name"] for e in events] == ["pod-3", "pod-4"]


def test_the_envelope_claims_no_commit_or_branch():
    envelope = build_envelope([_event()], "kind-audit")

    assert envelope["repo"] == "kind-audit"
    # A cluster has no commit. Inventing one would put a lie in the PR comment header.
    assert envelope["commit"] == ""
    assert envelope["branch"] == ""
    assert list(envelope["scans"]) == ["k8s_audit"]


def test_a_collected_log_normalises_into_findings(tmp_path):
    # The seam, end to end on the client side: log file -> envelope -> the same
    # parse_envelope the endpoint calls, with no scanner involved anywhere.
    path = _write_log(
        tmp_path,
        json.dumps(_event("a")),
        json.dumps(_event("a", stamp="2026-08-21T13:41:00.000000Z")),
        json.dumps(_event("b")),
    )

    events, _ = read_events(path)
    findings = dedupe(parse_envelope(build_envelope(events, "kind-audit")))

    assert len(findings) == 2
    assert {f.scanner for f in findings} == {"k8s-audit"}
    assert "2 requests," in next(f.title for f in findings if f.target.endswith("/a"))
