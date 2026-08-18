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
