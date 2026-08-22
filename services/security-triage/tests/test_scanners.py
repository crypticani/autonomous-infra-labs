import json
import os

import pytest

from scanners import Finding, dedupe, parse_envelope

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "fixtures", "this-repo.json"
)


def _trivy_envelope(**vuln_overrides):
    vuln = {
        "VulnerabilityID": "CVE-2026-45829",
        "PkgName": "chromadb",
        "InstalledVersion": "1.5.9",
        "FixedVersion": "1.5.10",
        "Severity": "CRITICAL",
        "Title": "chromadb: arbitrary code execution",
        "CweIDs": ["CWE-94", "CWE-502"],
        **vuln_overrides,
    }
    return {
        "repo": "example/repo",
        "commit": "abc123",
        "branch": "main",
        "scans": {
            "trivy": {
                "Results": [
                    {
                        "Target": "services/knowledge-copilot/requirements.txt",
                        "Vulnerabilities": [vuln],
                    }
                ]
            }
        },
    }


def test_parses_a_trivy_vulnerability():
    findings = parse_envelope(_trivy_envelope())

    assert len(findings) == 1
    f = findings[0]
    assert f.scanner == "trivy"
    assert f.rule_id == "CVE-2026-45829"
    assert f.severity_raw == "CRITICAL"
    assert f.package == "chromadb"
    assert f.installed_version == "1.5.9"
    assert f.fixed_version == "1.5.10"
    assert f.target == "services/knowledge-copilot/requirements.txt"
    assert f.cwe == "CWE-94,CWE-502"


def test_parses_a_trivy_misconfiguration():
    envelope = {
        "scans": {
            "trivy": {
                "Results": [
                    {
                        "Target": "services/log-analyzer/Dockerfile",
                        "Misconfigurations": [
                            {
                                "ID": "DS-0026",
                                "Severity": "LOW",
                                "Title": "No HEALTHCHECK defined",
                                "CauseMetadata": {"StartLine": 12},
                            }
                        ],
                    }
                ]
            }
        }
    }

    findings = parse_envelope(envelope)

    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "DS-0026"
    assert f.target == "services/log-analyzer/Dockerfile"
    assert f.line == 12
    assert f.package is None


def test_parses_a_bandit_result():
    envelope = {
        "scans": {
            "bandit": {
                "results": [
                    {
                        "test_id": "B104",
                        "issue_severity": "MEDIUM",
                        "issue_text": "Possible binding to all interfaces.",
                        "filename": "services/knowledge-copilot/app.py",
                        "line_number": 619,
                        "issue_cwe": {"id": 605},
                    }
                ]
            }
        }
    }

    findings = parse_envelope(envelope)

    assert len(findings) == 1
    f = findings[0]
    assert f.scanner == "bandit"
    assert f.rule_id == "B104"
    assert f.target == "services/knowledge-copilot/app.py"
    assert f.line == 619
    assert f.cwe == "605"


def test_parses_a_checkov_failed_check():
    envelope = {
        "scans": {
            "checkov": [
                {
                    "check_type": "kubernetes",
                    "results": {
                        "failed_checks": [
                            {
                                "check_id": "CKV_K8S_21",
                                "check_name": "The default namespace should not be used",
                                "severity": None,
                                "file_path": "/services/log-analyzer/k8s/configmap.yaml",
                                "file_line_range": [1, 12],
                            }
                        ],
                        "passed_checks": [{"check_id": "CKV_K8S_99"}],
                    },
                }
            ]
        }
    }

    findings = parse_envelope(envelope)

    assert len(findings) == 1
    f = findings[0]
    assert f.scanner == "checkov"
    assert f.rule_id == "CKV_K8S_21"
    assert f.severity_raw is None
    assert f.target == "/services/log-analyzer/k8s/configmap.yaml"
    assert f.line == 1


def test_missing_scanner_key_is_not_an_error():
    envelope = _trivy_envelope()
    del envelope["scans"]

    findings = parse_envelope({"scans": {}})

    assert findings == []
    # a fuller partial envelope (trivy present, bandit/checkov absent) still parses
    findings = parse_envelope(_trivy_envelope())
    assert len(findings) == 1


def test_dedupe_collapses_the_same_cve_from_two_scans():
    findings = parse_envelope(_trivy_envelope()) + parse_envelope(_trivy_envelope())

    assert len(findings) == 2
    deduped = dedupe(findings)
    assert len(deduped) == 1


def test_dedupe_keeps_distinct_packages_with_the_same_cve_id():
    a = parse_envelope(_trivy_envelope(PkgName="chromadb"))
    b = parse_envelope(_trivy_envelope(PkgName="other-package"))

    deduped = dedupe(a + b)

    assert len(deduped) == 2


def test_dedupe_collapses_the_same_location_across_scanners():
    trivy = {
        "scans": {
            "trivy": {
                "Results": [
                    {
                        "Target": "services/log-analyzer/Dockerfile",
                        "Misconfigurations": [
                            {
                                "ID": "DS-0026",
                                "Severity": "LOW",
                                "Title": "No HEALTHCHECK defined",
                                "CauseMetadata": {"StartLine": 12},
                            }
                        ],
                    }
                ]
            }
        }
    }
    checkov = {
        "scans": {
            "checkov": [
                {
                    "check_type": "dockerfile",
                    "results": {
                        "failed_checks": [
                            {
                                "check_id": "CKV_DOCKER_2",
                                "check_name": "Ensure that HEALTHCHECK instructions...",
                                "severity": None,
                                "file_path": "services/log-analyzer/Dockerfile",
                                "file_line_range": [12, 12],
                            }
                        ]
                    },
                }
            ]
        }
    }

    findings = parse_envelope(trivy) + parse_envelope(checkov)
    assert len(findings) == 2

    deduped = dedupe(findings)
    assert len(deduped) == 1


def test_trivy_context_stops_at_the_truncation_sentinel():
    # Trivy caps a code block at ten lines and marks the cut with an entry whose Content
    # is empty. Carrying that sentinel through would put a phantom blank line into any
    # diff built from these lines.
    envelope = {
        "scans": {
            "trivy": {
                "Results": [
                    {
                        "Target": "services/log-analyzer/k8s/deployment.yaml",
                        "Misconfigurations": [
                            {
                                "ID": "KSV-0014",
                                "Title": "Root file system is not read-only",
                                "Message": "Container 'log-analyzer' of Deployment x",
                                "Resolution": "Change it to 'true'.",
                                "CauseMetadata": {
                                    "StartLine": 22,
                                    "Code": {
                                        "Lines": [
                                            {
                                                "Number": 22,
                                                "Content": "        - name: log-analyzer",
                                                "Truncated": False,
                                            },
                                            {
                                                "Number": 23,
                                                "Content": "",
                                                "Truncated": False,
                                            },
                                            {
                                                "Number": 24,
                                                "Content": "",
                                                "Truncated": True,
                                            },
                                        ]
                                    },
                                },
                            }
                        ],
                    }
                ]
            }
        }
    }

    f = parse_envelope(envelope)[0]

    # The blank line at 23 is a real blank line in the file; the one at 24 is a hole.
    assert f.context == [(22, "        - name: log-analyzer"), (23, "")]
    assert f.resolution == "Change it to 'true'."
    assert f.message == "Container 'log-analyzer' of Deployment x"


def test_trivy_secret_context_is_the_redacted_lines():
    envelope = {
        "scans": {
            "trivy": {
                "Results": [
                    {
                        "Target": "services/self-healing-agent/.secrets/kubeconfig",
                        "Secrets": [
                            {
                                "RuleID": "jwt-token",
                                "Title": "JWT token",
                                "StartLine": 18,
                                "Code": {
                                    "Lines": [
                                        {
                                            "Number": 18,
                                            "Content": "      token: ****",
                                            "Truncated": False,
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                ]
            }
        }
    }

    f = parse_envelope(envelope)[0]

    # Trivy puts Code on the secret itself, not under CauseMetadata as it does for a
    # misconfiguration -- and the content it returns is already asterisked out.
    assert f.context == [(18, "      token: ****")]


def test_bandit_code_block_recovers_the_original_lines():
    envelope = {
        "scans": {
            "bandit": {
                "results": [
                    {
                        "test_id": "B104",
                        "issue_text": "Possible binding to all interfaces.",
                        "filename": "./services/knowledge-copilot/app.py",
                        "line_number": 619,
                        # Bandit's own format: unpadded number, one space, then the line
                        # exactly as it appears, indentation included.
                        "code": '618 if __name__ == "__main__":\n619     uvicorn.run(app)\n',
                    }
                ]
            }
        }
    }

    f = parse_envelope(envelope)[0]

    assert f.context == [
        (618, 'if __name__ == "__main__":'),
        (619, "    uvicorn.run(app)"),
    ]


def test_checkov_carries_its_guideline_and_code_block():
    envelope = {
        "scans": {
            "checkov": [
                {
                    "check_type": "kubernetes",
                    "results": {
                        "failed_checks": [
                            {
                                "check_id": "CKV_K8S_21",
                                "check_name": "The default namespace should not be used",
                                "file_path": "/services/log-analyzer/k8s/configmap.yaml",
                                "file_line_range": [1, 12],
                                "guideline": "https://docs.example/bc-k8s-20",
                                "code_block": [
                                    [1, "apiVersion: v1\n"],
                                    [2, "kind: ConfigMap\n"],
                                ],
                            }
                        ]
                    },
                }
            ]
        }
    }

    f = parse_envelope(envelope)[0]

    assert f.resolution == "https://docs.example/bc-k8s-20"
    assert f.context == [(1, "apiVersion: v1"), (2, "kind: ConfigMap")]


def test_a_finding_with_no_context_defaults_to_empty():
    # The committed fixture's Checkov half is exactly this: scan.sh runs with --compact,
    # which strips code_block, and DS-0026 ships no CauseMetadata at all.
    f = parse_envelope(_trivy_envelope())[0]

    assert f.context == []
    assert f.resolution is None
    assert f.message is None


def test_fingerprint_is_stable_across_calls():
    a = parse_envelope(_trivy_envelope())[0]
    b = parse_envelope(_trivy_envelope())[0]

    assert a.fingerprint == b.fingerprint


@pytest.mark.skipif(not os.path.exists(FIXTURE_PATH), reason="no committed fixture yet")
def test_real_fixture_dedupes_without_dropping_everything():
    with open(FIXTURE_PATH) as f:
        envelope = json.load(f)

    findings = parse_envelope(envelope)
    deduped = dedupe(findings)

    assert len(findings) > 0
    assert 0 < len(deduped) <= len(findings)


# --- Day 26: Kubernetes audit events, the fourth shape -------------------------------


def _audit_event(**overrides):
    """One `audit.k8s.io/v1` Event, shaped like the real thing.

    A `kubectl exec` at Metadata level, which is what the committed audit policy
    produces -- the exec's command is in `requestURI`'s query string, and there is no
    request or response body at all, on purpose (see the policy's comment about a Secret
    read at RequestResponse level being a second copy of the secret).
    """
    event = {
        "kind": "Event",
        "apiVersion": "audit.k8s.io/v1",
        "level": "Metadata",
        "stage": "ResponseComplete",
        "requestURI": "/api/v1/namespaces/sandbox/pods/flaky-app-7d9/exec?command=ls",
        "verb": "create",
        "user": {"username": "kubernetes-admin", "groups": ["kubeadm:cluster-admins"]},
        "sourceIPs": ["172.18.0.1"],
        "objectRef": {
            "resource": "pods",
            "subresource": "exec",
            "namespace": "sandbox",
            "name": "flaky-app-7d9",
        },
        "responseStatus": {"code": 101},
        "stageTimestamp": "2026-08-21T13:40:12.000000Z",
        "annotations": {"authorization.k8s.io/decision": "allow"},
    }
    event.update(overrides)
    return event


def _audit_envelope(*events):
    return {"repo": "kind-audit", "scans": {"k8s_audit": list(events)}}


def test_parses_a_kubectl_exec():
    f = parse_envelope(_audit_envelope(_audit_event()))[0]

    assert f.scanner == "k8s-audit"
    assert f.rule_id == "K8S-EXEC"
    assert f.target == "kubernetes-admin@sandbox/flaky-app-7d9"
    assert "1 request," in f.title
    assert "2026-08-21T13:40:12" in f.title
    # Nothing assigned a severity here and this module does not invent one -- judging is
    # triage.py's job, and a file/line/package on a runtime event would be fiction.
    assert f.severity_raw is None
    assert f.line is None
    assert f.package is None


def test_repeated_events_group_into_one_finding_with_a_count():
    findings = parse_envelope(
        _audit_envelope(
            _audit_event(stageTimestamp="2026-08-21T13:40:12.000000Z"),
            _audit_event(stageTimestamp="2026-08-21T13:41:30.000000Z"),
            _audit_event(stageTimestamp="2026-08-21T13:40:55.000000Z"),
        )
    )

    assert len(findings) == 1
    assert "3 requests," in findings[0].title
    # The newest, not the last one in the file -- an audit log is append-only in
    # practice, but nothing in the format promises it.
    assert "last 2026-08-21T13:41:30" in findings[0].title


def test_two_actors_on_one_object_stay_two_findings():
    # The reason the actor is spelled into `target`: it is one of the three fields
    # _fingerprint reads for a finding with no package and no line, so without it these
    # two share a fingerprint and dedupe drops one of them -- losing the fact that a
    # second identity did the same thing.
    findings = parse_envelope(
        _audit_envelope(
            _audit_event(),
            _audit_event(user={"username": "system:serviceaccount:sandbox:default"}),
        )
    )

    assert len(findings) == 2
    assert len({f.fingerprint for f in findings}) == 2
    assert len(dedupe(findings)) == 2


def test_reading_a_secret_is_a_finding_but_creating_one_is_not():
    secret_ref = {"resource": "secrets", "namespace": "sandbox", "name": "demo-creds"}

    read = parse_envelope(
        _audit_envelope(_audit_event(verb="get", objectRef=secret_ref))
    )
    written = parse_envelope(
        _audit_envelope(_audit_event(verb="create", objectRef=secret_ref))
    )

    assert [f.rule_id for f in read] == ["K8S-SECRET-READ"]
    # Creating a Secret is how the cluster is supposed to work. The audit policy logs it
    # anyway -- volume is bounded by the policy, meaning by the rule table.
    assert written == []


def test_a_refused_request_is_a_finding_whatever_the_verb():
    findings = parse_envelope(
        _audit_envelope(
            _audit_event(
                verb="create",
                objectRef={"resource": "secrets", "namespace": "kube-system"},
                responseStatus={"code": 403},
                annotations={"authorization.k8s.io/decision": "forbid"},
            )
        )
    )

    assert [f.rule_id for f in findings] == ["K8S-FORBIDDEN"]
    assert "refused a create" in findings[0].title


def test_impersonation_names_both_identities():
    f = parse_envelope(
        _audit_envelope(
            _audit_event(
                impersonatedUser={"username": "system:serviceaccount:sandbox:default"}
            )
        )
    )[0]

    assert f.target == (
        "kubernetes-admin as system:serviceaccount:sandbox:default"
        "@sandbox/flaky-app-7d9"
    )


def test_an_unaudited_resource_is_not_a_finding():
    findings = parse_envelope(
        _audit_envelope(
            _audit_event(
                verb="get",
                objectRef={
                    "resource": "configmaps",
                    "namespace": "sandbox",
                    "name": "app-config",
                },
            )
        )
    )

    assert findings == []


def test_the_four_rbac_resources_share_one_rule():
    findings = parse_envelope(
        _audit_envelope(
            *(
                _audit_event(
                    verb="create",
                    objectRef={"resource": resource, "name": f"grant-{resource}"},
                    stageTimestamp="2026-08-21T13:40:12.000000Z",
                )
                for resource in (
                    "roles",
                    "rolebindings",
                    "clusterroles",
                    "clusterrolebindings",
                )
            )
        )
    )

    assert {f.rule_id for f in findings} == {"K8S-RBAC-WRITE"}
    # Cluster-scoped: no namespace in the objectRef, so the target is just the name.
    assert findings[0].target == "kubernetes-admin@grant-roles"


def test_a_nameless_request_keeps_its_resource_in_the_target():
    # Found on the first real audit log, not by writing this test first: a create has no
    # name in its objectRef and neither does a list, so without the resource standing in
    # these two refusals were both `kubernetes-admin@cluster` -- one fingerprint for two
    # unrelated events, and dedupe keeps one.
    refused = {"authorization.k8s.io/decision": "forbid"}
    findings = parse_envelope(
        _audit_envelope(
            _audit_event(
                verb="list", objectRef={"resource": "secrets"}, annotations=refused
            ),
            _audit_event(
                verb="create",
                objectRef={"resource": "clusterrolebindings"},
                annotations=refused,
            ),
        )
    )

    assert [f.target for f in findings] == [
        "kubernetes-admin@secrets",
        "kubernetes-admin@clusterrolebindings",
    ]
    assert len({f.fingerprint for f in findings}) == 2


def test_an_empty_object_ref_falls_back_to_cluster():
    f = parse_envelope(
        _audit_envelope(
            _audit_event(
                verb="get",
                objectRef={},
                annotations={"authorization.k8s.io/decision": "forbid"},
            )
        )
    )[0]

    assert f.target == "kubernetes-admin@cluster"


def test_runtime_events_and_scanner_findings_arrive_from_one_envelope():
    # The day's actual claim: one envelope, one parse call, one Finding shape, whether
    # the thing happened at build time or five minutes ago.
    envelope = _trivy_envelope()
    envelope["scans"]["k8s_audit"] = [_audit_event()]

    findings = parse_envelope(envelope)

    assert {f.scanner for f in findings} == {"trivy", "k8s-audit"}
    assert len(dedupe(findings)) == 2
