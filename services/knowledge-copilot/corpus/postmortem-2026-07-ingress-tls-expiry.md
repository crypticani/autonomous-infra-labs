---
title: Postmortem — ingress TLS certificate expiry
service: edge
doc_type: postmortem
last_reviewed: 2026-07-05
---

## Summary

On 2026-07-03 at 08:14 UTC the public ingress began serving an expired TLS certificate.
Browsers showed `NET::ERR_CERT_DATE_INVALID` and all HTTPS traffic to the apex domain failed
for 22 minutes. Root cause was a cert-manager renewal that had silently failed nine days
earlier because of a misconfigured ACME solver.

## Timeline

- 2026-06-24 — cert-manager renewal attempt fails; the DNS-01 solver could not create the
  challenge record. The existing cert was still valid, so nothing user-facing broke.
- 07-03 08:14 — Certificate reaches `notAfter`. Ingress serves the expired cert.
- 08:16 — Synthetic HTTPS probe fails; on-call paged.
- 08:22 — `kubectl get certificate` shows `READY=False`, last renewal 9 days stale.
- 08:31 — ACME solver credentials fixed; renewal forced.
- 08:36 — New certificate issued and picked up by the ingress. Traffic recovers.

## Root cause

The renewal failure produced a `CertificateRequest` in a failed state but no alert. cert-manager
kept retrying quietly. Because certificates renew at two-thirds of their lifetime, there was a
30-day window where the failure was invisible — and it stayed invisible until expiry.

## Follow-ups

- Alert on any `Certificate` with `READY=False` for more than 1 hour. (done)
- Alert on `certmanager_certificate_expiration_timestamp_seconds` < 7 days. (done)
- Runbook cross-link: see `tls-cert-expiry` for the manual renewal steps used at 08:31.
