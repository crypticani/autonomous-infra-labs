"""Four wire shapes, one Finding.

Trivy nests `Results[].Vulnerabilities[]` / `Misconfigurations[]` / `Secrets[]`, Bandit
is a flat `results[]`, Checkov is a *list* of per-framework reports. This module is the
only place that knows any of that. Any `scans` key can be absent -- a Go repo never runs
Bandit, which is a different key set, not an error.

`k8s_audit` is the one that argues for the seam: not a scanner but a stream of Kubernetes
audit events, no file, no line, no package, describing what already happened. It lands in
the same `Finding` and rides the same fingerprint, dedup, batching and scoring, because
all anything downstream needed was "a thing worth a judgment, and where it is".

Dedup is the second job, and it is arithmetic on strings, not a model call. A finding
tied to a package is identified by (rule_id, package, installed_version) -- not by which
scan produced it, so a filesystem scan and an image scan of the same package collapse.
One tied to a line and no package is identified by (target, line) alone.

# ponytail: (target, line) is a heuristic, not a rule-id crosswalk -- Checkov's CKV_* and
# Trivy's KSV-* share no vocabulary, so it is the only thing that can catch them
# describing the same block. Ceiling: two different findings on one line merge. That is
# real -- ten securityContext rules against one container block all report the block's
# StartLine -- which is why fixes.py reads the pre-dedup list and groups on the insertion
# point instead.
"""

import hashlib

from pydantic import BaseModel


class Finding(BaseModel):
    scanner: str
    rule_id: str
    severity_raw: str | None = None
    title: str
    target: str
    line: int | None = None
    package: str | None = None
    installed_version: str | None = None
    fixed_version: str | None = None
    cwe: str | None = None
    # Things the scanners already send that this module used to drop. None feeds
    # `_fingerprint`, so no fingerprint changed when they were added.
    resolution: str | None = (
        None  # the scanner's own remediation line, where it has one
    )
    message: str | None = None  # Trivy's per-finding message; the only thing that names
    # which container of a Deployment a misconfiguration is about
    context: list[tuple[int, str]] = []  # (line number, content), in file order
    fingerprint: str


def _fingerprint(
    rule_id: str,
    target: str,
    line: int | None,
    package: str | None,
    installed_version: str | None,
) -> str:
    if package:
        key = ("dep", rule_id, package, installed_version)
    elif line is not None:
        key = ("loc", target, line)
    else:
        key = ("other", rule_id, target)
    return hashlib.sha256(repr(key).encode()).hexdigest()[:16]


def _finding(**fields) -> Finding:
    fingerprint = _fingerprint(
        fields["rule_id"],
        fields["target"],
        fields.get("line"),
        fields.get("package"),
        fields.get("installed_version"),
    )
    return Finding(fingerprint=fingerprint, **fields)


def _trivy_context(holder: dict) -> list[tuple[int, str]]:
    """Trivy's `Code.Lines`, stopping at the truncation sentinel."""
    lines = []
    for line in (holder.get("Code") or {}).get("Lines") or []:
        if line.get("Truncated"):
            break
        lines.append((line["Number"], line["Content"]))
    return lines


def _bandit_context(code: str | None) -> list[tuple[int, str]]:
    """Bandit ships the offending line plus a neighbour either side as one string, each
    line prefixed with its unpadded number and a single space -- so one partition per
    line recovers the original content exactly, indentation included.
    """
    lines = []
    for raw_line in (code or "").split("\n"):
        number, _, content = raw_line.partition(" ")
        if number.isdigit():
            lines.append((int(number), content))
    return lines


def _checkov_context(code_block: list | None) -> list[tuple[int, str]]:
    """Checkov's `code_block`, a list of [number, content] pairs."""
    return [(number, content.rstrip("\n")) for number, content in code_block or []]


def _parse_trivy(raw: dict) -> list[Finding]:
    findings = []
    for result in raw.get("Results") or []:
        target = result.get("Target", "")

        for vuln in result.get("Vulnerabilities") or []:
            findings.append(
                _finding(
                    scanner="trivy",
                    rule_id=vuln["VulnerabilityID"],
                    severity_raw=vuln.get("Severity"),
                    title=vuln.get("Title") or vuln["VulnerabilityID"],
                    target=target,
                    package=vuln.get("PkgName"),
                    installed_version=vuln.get("InstalledVersion"),
                    fixed_version=vuln.get("FixedVersion"),
                    cwe=",".join(vuln["CweIDs"]) if vuln.get("CweIDs") else None,
                )
            )

        for misconfig in result.get("Misconfigurations") or []:
            cause = misconfig.get("CauseMetadata") or {}
            findings.append(
                _finding(
                    scanner="trivy",
                    rule_id=misconfig["ID"],
                    severity_raw=misconfig.get("Severity"),
                    title=misconfig.get("Title", misconfig["ID"]),
                    target=target,
                    line=cause.get("StartLine"),
                    resolution=misconfig.get("Resolution"),
                    message=misconfig.get("Message"),
                    context=_trivy_context(cause),
                )
            )

        for secret in result.get("Secrets") or []:
            findings.append(
                _finding(
                    scanner="trivy",
                    rule_id=secret.get("RuleID", "secret"),
                    severity_raw=secret.get("Severity"),
                    title=secret.get("Title", "secret detected"),
                    target=target,
                    line=secret.get("StartLine"),
                    # Redacted by Trivy, so this shows a human *where* and can never be
                    # diffed back. fixes.py's rule allowlist is the other half of that.
                    context=_trivy_context(secret),
                )
            )

    return findings


def _parse_bandit(raw: dict) -> list[Finding]:
    findings = []
    for result in raw.get("results") or []:
        cwe = result.get("issue_cwe")
        findings.append(
            _finding(
                scanner="bandit",
                rule_id=result["test_id"],
                severity_raw=result.get("issue_severity"),
                title=result.get("issue_text", result["test_id"]),
                target=result.get("filename", ""),
                line=result.get("line_number"),
                cwe=str(cwe["id"]) if cwe else None,
                context=_bandit_context(result.get("code")),
            )
        )
    return findings


def _parse_checkov(raw: list) -> list[Finding]:
    findings = []
    for report in raw or []:
        for check in (report.get("results") or {}).get("failed_checks") or []:
            line_range = check.get("file_line_range") or [None]
            findings.append(
                _finding(
                    scanner="checkov",
                    rule_id=check["check_id"],
                    severity_raw=check.get("severity"),
                    title=check.get("check_name", check["check_id"]),
                    target=check.get("file_path", ""),
                    line=line_range[0],
                    # A URL: Checkov has no prose remediation field.
                    resolution=check.get("guideline"),
                    context=_checkov_context(check.get("code_block")),
                )
            )
    return findings


_READ_VERBS = {"get", "list", "watch"}
_WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection"}

# One row: four RBAC resources are the same judgment, something changed who can do what.
_RBAC_WRITE = ("K8S-RBAC-WRITE", "changed who can do what in the cluster", _WRITE_VERBS)

# (resource, subresource) -> rule id, what happened, applicable verbs (None = any).
# Narrower than the audit policy on purpose: the policy bounds volume, this decides
# meaning, and an event matching nothing here is not a finding. Creating a Secret is how
# the cluster works; reading one is the interesting verb.
_AUDIT_RULES = {
    ("pods", "exec"): ("K8S-EXEC", "ran an interactive command in a pod", None),
    ("pods", "attach"): ("K8S-ATTACH", "attached to a running pod's process", None),
    ("pods", "portforward"): ("K8S-PORTFORWARD", "opened a tunnel into a pod", None),
    ("secrets", ""): (
        "K8S-SECRET-READ",
        "read Secret contents through the API",
        _READ_VERBS,
    ),
    ("serviceaccounts", "token"): (
        "K8S-TOKEN-MINT",
        "minted a ServiceAccount token",
        None,
    ),
    ("roles", ""): _RBAC_WRITE,
    ("rolebindings", ""): _RBAC_WRITE,
    ("clusterroles", ""): _RBAC_WRITE,
    ("clusterrolebindings", ""): _RBAC_WRITE,
}


def _audit_rule(event: dict) -> tuple[str, str] | None:
    """Which rule this event is, or None for noise.

    A refusal short-circuits the table: the API server saying no to an audited resource is
    worth triaging whatever the verb was.

    ponytail: only refusals for resources the policy already logs are seen, so probing every
    Deployment and being refused is invisible. A policy wide enough to catch that logs every
    request the cluster serves. Upgrade path is a second Metadata rule.
    """
    if (event.get("annotations") or {}).get(
        "authorization.k8s.io/decision"
    ) == "forbid":
        return "K8S-FORBIDDEN", f"was refused a {event.get('verb') or 'request'}"

    ref = event.get("objectRef") or {}
    rule = _AUDIT_RULES.get((ref.get("resource", ""), ref.get("subresource", "")))
    if rule is None:
        return None

    rule_id, what, verbs = rule
    if verbs is not None and event.get("verb") not in verbs:
        return None
    return rule_id, what


def _audit_actor(event: dict) -> str:
    """Who did it, including the impersonated identity."""
    user = (event.get("user") or {}).get("username") or "unknown"
    impersonated = (event.get("impersonatedUser") or {}).get("username")
    return f"{user} as {impersonated}" if impersonated else user


def _parse_k8s_audit(raw: list) -> list[Finding]:
    """Kubernetes audit events, grouped into findings."""
    groups: dict[tuple[str, str], dict] = {}
    for event in raw or []:
        rule = _audit_rule(event)
        if rule is None:
            continue
        rule_id, what = rule

        ref = event.get("objectRef") or {}
        # The resource stands in when there is no name: a `create` has none in its
        # objectRef (the name is in the body, which Metadata omits) and nor does a
        # `list`. Without it, two unrelated refusals shared one fingerprint.
        where = "/".join(
            p
            for p in (ref.get("namespace"), ref.get("name") or ref.get("resource"))
            if p
        )
        target = f"{_audit_actor(event)}@{where or 'cluster'}"
        seen = event.get("stageTimestamp") or ""

        group = groups.setdefault(
            (rule_id, target), {"what": what, "count": 0, "last": seen}
        )
        group["count"] += 1
        group["last"] = max(group["last"], seen)

    findings = []
    for (rule_id, target), group in groups.items():
        count = group["count"]
        findings.append(
            _finding(
                scanner="k8s-audit",
                rule_id=rule_id,
                # In the title because that is what reaches the model: triage.py sends title
                # and target, and "how often" and "when" are what make it judgeable.
                title=(
                    f"{group['what']} ({count} request{'s' if count > 1 else ''}, "
                    f"last {group['last'] or 'unknown'})"
                ),
                target=target,
            )
        )
    return findings


_PARSERS = {
    "trivy": _parse_trivy,
    "bandit": _parse_bandit,
    "checkov": _parse_checkov,
    "k8s_audit": _parse_k8s_audit,
}


def parse_envelope(envelope: dict) -> list[Finding]:
    """Every finding across whichever of the three scans are present. A scan key that is
    absent, or present but empty, contributes nothing -- never an error.
    """
    findings = []
    for name, parser in _PARSERS.items():
        raw = (envelope.get("scans") or {}).get(name)
        if raw:
            findings.extend(parser(raw))
    return findings


def dedupe(findings: list[Finding]) -> list[Finding]:
    """First occurrence per fingerprint wins, input order preserved."""
    seen: set[str] = set()
    deduped = []
    for finding in findings:
        if finding.fingerprint in seen:
            continue
        seen.add(finding.fingerprint)
        deduped.append(finding)
    return deduped
