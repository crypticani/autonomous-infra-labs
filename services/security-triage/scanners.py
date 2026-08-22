"""Four wire shapes, one Finding -- Day 22, extended on Day 26.

Three scanners, three schemas that agree on almost nothing: Trivy nests
`Results[].Vulnerabilities[]` / `Misconfigurations[]` / `Secrets[]` under a scan target,
Bandit is a flat `results[]`, and Checkov is a *list* of per-framework reports each with
`results.failed_checks[]` (plus `passed_checks`/`skipped_checks` we never read). None of
that is the caller's problem -- POST /triage always takes the same envelope, and this
module is the only place that knows any of these shapes exist.

The envelope is `{repo, commit, branch, scans: {trivy?, bandit?, checkov?, k8s_audit?}}`,
and any of those `scans` keys can be absent -- a Go repo's scan.sh never runs Bandit, and
that is not an error, just a caller with a different key set (see `scan.sh`'s Bandit
guard).

Day 26 adds the fourth key, and it is the one that argues for this seam existing at all.
`k8s_audit` is not a scanner: it is a stream of Kubernetes audit events, no file, no line,
no package, no severity, describing something that *already happened* rather than
something that might. It still lands in the same `Finding`, gets fingerprinted by the same
function, deduped by the same rule, batched into the same model calls and scored by the
same policy -- because the only thing downstream of here ever needed was "a thing worth a
judgment, and where it is". Static findings and runtime events are the same triage problem
once the normalisation is honest, and the cost of proving that was one parser and one line
in `_PARSERS`. `runtime.py` is the collector on the other side, `scan.sh`'s counterpart.

Dedup is the second job here, and it is arithmetic on strings, not a model call: cheaper
now, and it is what keeps Day 23's LLM batches from re-triaging the same finding twice.
A finding tied to a package (a CVE) is identified by (rule_id, package, installed_version)
-- deliberately *not* by which scan produced it, so the same CVE reported from a filesystem
scan and an image scan of the same package collapses into one. A finding tied to a specific
line and no package (a misconfiguration or a code-scan hit) is identified by (target, line)
alone, dropping the rule id and the scanner name entirely.

# ponytail: (target, line) identity is a naive heuristic, not a rule-id crosswalk --
# Checkov's CKV_* and Trivy's KSV-*/DS-* checks share no common vocabulary, so this is the
# only thing that can catch them describing the same misconfigured block. Ceiling: two
# genuinely different findings that happen to land on the same line get merged into one.
# Upgrade path: a hand-built rule-id crosswalk table, if that turns out to happen in
# practice against real fixtures.
#
# It does happen in practice, found on Day 24: the ten securityContext rules Trivy can
# raise against one container block all report the block's StartLine, so they share one
# fingerprint and `dedupe` keeps exactly one. That is the right identity for triage
# (one judgment about one misconfigured block) and the wrong one for fixes (one surviving
# rule means one key in the proposed diff instead of ten), so `fixes.py` reads the
# pre-dedup list and groups on the insertion point instead. Left as-is rather than
# upgraded: the collapse is correct for what dedup exists to do, which is keep Day 23's
# batches from re-triaging the same block.
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
    # Day 24's three additions, all of them things the scanners were already sending and
    # this module was dropping on the floor. None of them feeds `_fingerprint`, so every
    # fingerprint Day 22 and Day 23 measured is unchanged.
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
    """Trivy's `Code.Lines`, stopping at the truncation sentinel.

    Trivy caps a code block at ten lines and marks the cut with an entry whose
    `Truncated` is true and whose `Content` is empty -- so a 43-line container block
    arrives as lines 22-30 and then a hole. Everything after that sentinel is a lie about
    the file, and a unified diff hunk needs contiguous lines with an accurate count, so
    the run before it is all any diff can be built from. (`holder` is `CauseMetadata` for
    a misconfiguration and the secret object itself for a secret -- Trivy puts `Code` in
    a different place for each.)
    """
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
    """Checkov's `code_block`, a list of [number, content] pairs.

    Empty for every finding in the committed fixture, because `scan.sh` runs checkov
    with `--compact` and that is precisely the flag that strips it. Parsed anyway so
    dropping the flag is the only change needed to get Checkov diffs out of `fixes.py`.
    """
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
                    # Redacted by Trivy -- the secret itself comes back as asterisks, so
                    # this shows a human *where* and can never be diffed back into the
                    # file. `fixes.py` only builds diffs for an allowlist of rule ids,
                    # which is what keeps a redacted line out of a patch.
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
                    # A URL, not a sentence -- Checkov has no prose remediation field.
                    # Still the best advice available for a check with no fixer.
                    resolution=check.get("guideline"),
                    context=_checkov_context(check.get("code_block")),
                )
            )
    return findings


_READ_VERBS = {"get", "list", "watch"}
_WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection"}

# One shared row, because four RBAC resources are the same judgment: something changed
# who can do what.
_RBAC_WRITE = ("K8S-RBAC-WRITE", "changed who can do what in the cluster", _WRITE_VERBS)

# (resource, subresource) -> rule id, what happened, the verbs it applies to (None = any).
# Deliberately narrower than the audit policy that produced the events: the policy bounds
# how much gets logged, this table decides what any of it means, and an event that matches
# nothing here is not a finding. `kubectl create secret` is logged and ignored -- creating
# a Secret is how the cluster is supposed to work; reading one is the interesting verb.
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
    """Which rule this event is, or None for the ones that are just noise.

    A refusal short-circuits the table: the API server saying no to a request against an
    audited resource is worth triaging whatever the verb was, and it is the one signal
    here that means something even when the request itself was mundane.

    # ponytail: a refusal is only seen for resources the audit policy already logs, so
    # this cannot catch someone probing, say, every Deployment in the cluster and being
    # refused. Ceiling accepted -- a policy wide enough to catch that logs every request
    # the cluster serves. Upgrade path: a second `level: Metadata` rule scoped to
    # non-resource URLs and a wider resource list, once there is a volume budget for it.
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
    """Who did it -- the impersonated identity too, when there is one.

    `kubectl --as` is how a cluster-admin borrows a lesser identity, and it is also how
    an attacker with admin does something while it looks like it came from somewhere
    else. Both halves belong in the answer, so this reads `admin as sa:sandbox:default`
    rather than picking one.
    """
    user = (event.get("user") or {}).get("username") or "unknown"
    impersonated = (event.get("impersonatedUser") or {}).get("username")
    return f"{user} as {impersonated}" if impersonated else user


def _parse_k8s_audit(raw: list) -> list[Finding]:
    """Day 26's fourth shape: Kubernetes audit events, grouped into findings.

    Grouped, and not one finding per event, because an audit log is a stream and a
    triage queue is a list of things to decide about. Fifty `get secret` requests from
    one identity against one Secret is one judgment with a count attached, not fifty
    identical ones -- and at one model call per ST_BATCH_SIZE findings, the difference is
    the whole run.

    The group key is (rule, actor, object), and it is spelled into `target` as
    `actor@namespace/name` rather than kept beside it. That is not cosmetic: `target` is
    one of the three fields `_fingerprint` reads for a finding with no package and no
    line, so putting the actor there is what makes two different users reading the same
    Secret two findings instead of one that `dedupe` silently drops. It also lands in the
    PR comment's `Where` column, where "who" is the first thing a reviewer asks.

    `severity_raw` stays None on purpose. The other three parsers copy a severity their
    scanner assigned; nothing assigned one here, and inventing a table of them would be
    this module quietly doing the judging that triage.py exists to do.
    """
    groups: dict[tuple[str, str], dict] = {}
    for event in raw or []:
        rule = _audit_rule(event)
        if rule is None:
            continue
        rule_id, what = rule

        ref = event.get("objectRef") or {}
        # The resource stands in when there is no name, because a `create` has none in
        # its objectRef -- the name is in the request body, which Metadata level
        # deliberately does not carry -- and neither does a `list`. Found on the very
        # first real audit log: a refused `create clusterrolebinding` and a refused
        # `list secrets` both came out as `kubernetes-admin@cluster`, which is one
        # fingerprint for two unrelated refusals, and dedupe would keep one of them.
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
                # The count and the timestamp are in the title because that is what
                # reaches the model -- triage.py's prompt sends title and target, and a
                # runtime finding with neither "how often" nor "when" is unjudgeable.
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
