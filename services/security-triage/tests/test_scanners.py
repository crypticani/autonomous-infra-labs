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
