import json
import os

import pytest

from fixes import propose_fixes
from scanners import parse_envelope

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "fixtures", "this-repo.json"
)

# The shape every k8s test here works from: a container whose `- name:` sits at indent 8,
# with a `ports:` list underneath carrying its own `- name:` at indent 12 -- the line the
# anchor must not pick.
CONTAINER_BLOCK = [
    {"Number": 22, "Content": "        - name: log-analyzer", "Truncated": False},
    {
        "Number": 23,
        "Content": "          image: ghcr.io/x/log-analyzer:1.4",
        "Truncated": False,
    },
    {"Number": 24, "Content": "          ports:", "Truncated": False},
    {"Number": 25, "Content": "            - name: http", "Truncated": False},
    {"Number": 26, "Content": "              containerPort: 7000", "Truncated": False},
]


# The indents the fixer has to derive rather than be told: `- name:` at column 8 puts the
# container's own keys at 10 (the dash's column, plus the dash, plus the space after it)
# and their values at 12. Spelled as arithmetic so no test asserts on counted spaces.
KEY = " " * 10
VALUE = " " * 12


def _quoted(lines):
    """The same lines as a diff quotes them -- context is content behind one space. Built
    from the block rather than typed out so a test can't fail on a miscounted literal.
    """
    return [" " + line["Content"] for line in lines]


def _misconfig(rule_id, message, lines=None, resolution="Change it.", start_line=22):
    return {
        "ID": rule_id,
        "Severity": "HIGH",
        "Title": f"{rule_id} title",
        "Message": message,
        "Resolution": resolution,
        "CauseMetadata": {
            "StartLine": start_line,
            "EndLine": 64,
            "Code": {"Lines": CONTAINER_BLOCK if lines is None else lines},
        },
    }


def _envelope(*misconfigs, target="services/log-analyzer/k8s/deployment.yaml"):
    return {
        "scans": {
            "trivy": {
                "Results": [{"Target": target, "Misconfigurations": list(misconfigs)}]
            }
        }
    }


def _fixes(*misconfigs, **kwargs):
    return propose_fixes(parse_envelope(_envelope(*misconfigs, **kwargs)))


def _only_diff(*misconfigs, **kwargs):
    diffs = [f for f in _fixes(*misconfigs, **kwargs) if f.kind == "diff"]
    assert len(diffs) == 1
    return diffs[0]


def test_builds_a_securitycontext_hunk_where_the_container_is():
    fix = _only_diff(
        _misconfig(
            "KSV-0014", "Container 'log-analyzer' should set readOnlyRootFilesystem"
        )
    )

    assert fix.target == "services/log-analyzer/k8s/deployment.yaml"
    quoted = _quoted(CONTAINER_BLOCK)
    assert fix.diff.splitlines() == [
        "--- a/services/log-analyzer/k8s/deployment.yaml",
        "+++ b/services/log-analyzer/k8s/deployment.yaml",
        # 5 lines quoted, 7 after the insert, and the file's own numbering preserved
        "@@ -22,5 +22,7 @@",
        quoted[0],
        f"+{KEY}securityContext:",
        f"+{VALUE}readOnlyRootFilesystem: true",
        *quoted[1:],
    ]
    assert fix.diff.endswith("\n")


def test_sibling_rules_on_one_block_become_one_hunk():
    # Six rules, one container. Six separate diffs would each insert their own
    # `securityContext:` key and the second one applied would produce invalid YAML.
    fix = _only_diff(
        _misconfig("KSV-0014", "Container 'log-analyzer' readOnlyRootFilesystem"),
        _misconfig("KSV-0020", "Container 'log-analyzer' runAsUser"),
        _misconfig("KSV-0021", "Container 'log-analyzer' runAsGroup"),
        _misconfig("KSV-0012", "Container 'log-analyzer' runAsNonRoot"),
        _misconfig("KSV-0001", "Container 'log-analyzer' allowPrivilegeEscalation"),
        _misconfig("KSV-0003", "Container 'log-analyzer' capabilities"),
    )

    assert fix.diff.count("securityContext:") == 1
    assert fix.rule_ids == [
        "KSV-0001",
        "KSV-0003",
        "KSV-0012",
        "KSV-0014",
        "KSV-0020",
        "KSV-0021",
    ]
    # Keys land in the table's order, not the order the findings arrived in.
    assert [
        line
        for line in fix.diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ] == [
        f"+{KEY}securityContext:",
        f"+{VALUE}runAsNonRoot: true",
        f"+{VALUE}allowPrivilegeEscalation: false",
        f"+{VALUE}runAsUser: 10001",
        f"+{VALUE}runAsGroup: 10001",
        f"+{VALUE}readOnlyRootFilesystem: true",
        f"+{VALUE}capabilities:",
        f"+{VALUE}  drop:",
        f"+{VALUE}    - ALL",
    ]


def test_sibling_rules_on_one_block_share_one_fingerprint():
    # Day 22 identifies a misconfiguration by (target, line), so these five collapse to a
    # single fingerprint -- which is why the fix layer reads the pre-dedup list, and why
    # the one fingerprint it reports is still the one a triage result is keyed by.
    fix = _only_diff(
        _misconfig("KSV-0014", "Container 'log-analyzer' readOnlyRootFilesystem"),
        _misconfig("KSV-0020", "Container 'log-analyzer' runAsUser"),
    )

    assert len(fix.fingerprints) == 1


def test_two_rules_wanting_the_same_key_do_not_repeat_it():
    fix = _only_diff(
        _misconfig("KSV-0030", 'container "log-analyzer" seccompProfile'),
        _misconfig("KSV-0104", 'container "log-analyzer" seccomp policies disabled'),
    )

    assert fix.diff.count("seccompProfile:") == 1
    assert fix.diff.count("type: RuntimeDefault") == 1


def test_the_ports_name_line_is_not_mistaken_for_the_container():
    # `- name: http` at indent 12 is a `- name:` line too, and a naive first-match anchor
    # inserts the securityContext inside `ports:`.
    fix = _only_diff(_misconfig("KSV-0014", "Container 'log-analyzer' x"))

    lines = fix.diff.splitlines()
    quoted = _quoted(CONTAINER_BLOCK)
    assert lines[lines.index(quoted[0]) + 1] == f"+{KEY}securityContext:"
    assert lines[lines.index(quoted[3]) + 1] == quoted[4]


def test_the_second_container_in_a_block_gets_its_own_anchor():
    lines = CONTAINER_BLOCK + [
        {"Number": 27, "Content": "        - name: sidecar", "Truncated": False},
        {
            "Number": 28,
            "Content": "          image: ghcr.io/x/sidecar:1.0",
            "Truncated": False,
        },
    ]

    fix = _only_diff(_misconfig("KSV-0014", "Container 'sidecar' x", lines=lines))

    diff_lines = fix.diff.splitlines()
    sidecar = _quoted(lines)[5]
    assert diff_lines[diff_lines.index(sidecar) + 1] == f"+{KEY}securityContext:"


def test_a_message_that_names_no_container_gets_advice():
    fixes = _fixes(_misconfig("KSV-0106", "container should drop all"))

    assert len(fixes) == 1
    assert fixes[0].kind == "advice"
    assert fixes[0].diff is None
    assert "does not name a container" in fixes[0].note
    assert "Change it." in fixes[0].note


def test_an_existing_securitycontext_gets_advice_not_a_second_one():
    lines = CONTAINER_BLOCK[:2] + [
        {"Number": 24, "Content": "          securityContext:", "Truncated": False},
        {"Number": 25, "Content": "            runAsUser: 10001", "Truncated": False},
    ]

    fixes = _fixes(_misconfig("KSV-0014", "Container 'log-analyzer' x", lines=lines))

    assert fixes[0].kind == "advice"
    assert "already present" in fixes[0].note


def test_a_container_past_the_truncation_gets_advice():
    # Trivy caps the block at ten lines; a container declared after the cut is invisible,
    # and the sentinel's empty content is not a line of the file.
    lines = [
        {"Number": 40, "Content": "        - name: sidecar", "Truncated": False},
        {"Number": 41, "Content": "", "Truncated": True},
    ]

    fixes = _fixes(
        _misconfig("KSV-0014", "Container 'log-analyzer' x", lines=lines, start_line=40)
    )

    assert fixes[0].kind == "advice"
    assert "not in the lines the scanner returned" in fixes[0].note


def test_no_context_at_all_gets_advice():
    misconfig = _misconfig("KSV-0014", "Container 'log-analyzer' x")
    misconfig["CauseMetadata"] = {"StartLine": None}

    fixes = _fixes(misconfig)

    assert fixes[0].kind == "advice"
    assert "no code context" in fixes[0].note


def test_a_gap_in_the_context_truncates_the_hunk_at_the_gap():
    lines = CONTAINER_BLOCK[:2] + [
        {"Number": 30, "Content": "          env:", "Truncated": False}
    ]

    fix = _only_diff(_misconfig("KSV-0014", "Container 'log-analyzer' x", lines=lines))

    # Two lines quoted, two inserted. Quoting line 30 as if it followed line 23 would
    # make the hunk's line count a lie about the file, and `git apply` would reject it.
    assert "@@ -22,2 +22,4 @@" in fix.diff
    assert "env:" not in fix.diff


def test_a_target_that_is_not_a_repo_file_gets_advice():
    fixes = _fixes(
        _misconfig("KSV-0014", "Container 'log-analyzer' x"),
        target="alpine:3.19 (alpine 3.19.1)",
    )

    assert fixes[0].kind == "advice"
    assert "not a repo file path" in fixes[0].note


def test_a_traversing_target_gets_advice():
    fixes = _fixes(
        _misconfig("KSV-0014", "Container 'log-analyzer' x"),
        target="../../etc/shadow",
    )

    assert fixes[0].kind == "advice"
    assert "not a repo file path" in fixes[0].note


@pytest.mark.parametrize(
    "target",
    [
        "services/x/deployment.yaml",  # trivy
        "/services/x/deployment.yaml",  # checkov
        "./services/x/deployment.yaml",  # bandit
    ],
)
def test_paths_from_all_three_scanners_normalise_to_one_form(target):
    fix = _only_diff(
        _misconfig("KSV-0014", "Container 'log-analyzer' x"), target=target
    )

    assert fix.target == "services/x/deployment.yaml"
    assert fix.diff.startswith("--- a/services/x/deployment.yaml\n")


def test_overlapping_hunks_in_one_file_become_advice():
    # Two containers, each anchored inside the *other's* quoted lines: two hunks sharing
    # lines make `git apply` reject the whole patch, so the second is dropped to prose.
    lines = CONTAINER_BLOCK + [
        {"Number": 27, "Content": "        - name: sidecar", "Truncated": False},
        {
            "Number": 28,
            "Content": "          image: ghcr.io/x/sidecar:1.0",
            "Truncated": False,
        },
    ]

    fixes = _fixes(
        _misconfig("KSV-0014", "Container 'log-analyzer' x", lines=lines),
        _misconfig("KSV-0012", "Container 'sidecar' x", lines=lines),
    )

    assert [f.kind for f in fixes] == ["diff", "advice"]
    assert "overlap" in fixes[1].note


def test_a_rule_with_no_fixer_gets_the_scanners_own_words_and_no_refusal_noise():
    fixes = _fixes(
        _misconfig(
            "KSV-0011",
            "Container 'log-analyzer' x",
            resolution="Set a CPU limit on the container.",
        )
    )

    assert fixes[0].kind == "advice"
    assert fixes[0].note == "Set a CPU limit on the container."


def test_advice_for_a_vulnerability_names_the_version_bump():
    envelope = {
        "scans": {
            "trivy": {
                "Results": [
                    {
                        "Target": "services/x/requirements.txt",
                        "Vulnerabilities": [
                            {
                                "VulnerabilityID": "CVE-2026-45829",
                                "PkgName": "chromadb",
                                "InstalledVersion": "1.5.9",
                                "FixedVersion": "1.5.10",
                                "Severity": "CRITICAL",
                                "Title": "chromadb: arbitrary code execution",
                            }
                        ],
                    }
                ]
            }
        }
    }

    fixes = propose_fixes(parse_envelope(envelope))

    assert fixes[0].kind == "advice"
    assert fixes[0].note == "upgrade chromadb 1.5.9 -> 1.5.10"


def test_a_bandit_finding_never_becomes_a_diff():
    envelope = {
        "scans": {
            "bandit": {
                "results": [
                    {
                        "test_id": "B104",
                        "issue_severity": "MEDIUM",
                        "issue_text": "Possible binding to all interfaces.",
                        "filename": "./services/x/app.py",
                        "line_number": 619,
                        "code": '618 if __name__ == "__main__":\n619     uvicorn.run(app)\n',
                    }
                ]
            }
        }
    }

    fixes = propose_fixes(parse_envelope(envelope))

    assert fixes[0].kind == "advice"
    assert fixes[0].note == "Possible binding to all interfaces."


@pytest.mark.skipif(not os.path.exists(FIXTURE_PATH), reason="no committed fixture yet")
def test_every_diff_from_the_real_fixture_is_a_well_formed_hunk():
    with open(FIXTURE_PATH) as f:
        envelope = json.load(f)

    # Deliberately not deduped -- see propose_fixes' docstring.
    fixes = propose_fixes(parse_envelope(envelope))
    diffs = [fix for fix in fixes if fix.kind == "diff"]

    assert diffs, "the fixture should produce at least one proposed diff"
    for fix in diffs:
        lines = fix.diff.splitlines()
        assert lines[0] == f"--- a/{fix.target}"
        assert lines[1] == f"+++ b/{fix.target}"

        old, new = lines[2].split(" ")[1:3]
        old_start, old_count = (int(n) for n in old.removeprefix("-").split(","))
        new_start, new_count = (int(n) for n in new.removeprefix("+").split(","))
        body = lines[3:]

        # An insert-only hunk: same start line, every quoted line still there, and the
        # new count accounting for exactly the added lines. Getting this arithmetic wrong
        # is the single most likely way to emit a patch that will not apply.
        assert old_start == new_start
        assert len(body) == new_count
        assert sum(1 for line in body if line.startswith(" ")) == old_count
        assert sum(1 for line in body if line.startswith("+")) == new_count - old_count
        assert not any(line.startswith("-") for line in body)
        assert fix.diff.endswith("\n")
        assert fix.fingerprints and fix.rule_ids
