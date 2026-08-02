---
title: Expired TLS certificate on ingress
service: edge
doc_type: runbook
last_reviewed: 2026-07-21
---

## Symptom

Users report the site is not secure. Browsers show NET::ERR_CERT_DATE_INVALID and
refuse to load the page. API clients fail with certificate verification errors, and
anything with strict TLS validation — mobile apps, third-party webhooks,
service-to-service calls — starts failing before humans notice, because those
clients have no "proceed anyway" button.

## How to confirm

Check what the edge is actually serving, from outside the cluster:

    echo | openssl s_client -connect <host>:443 -servername <host> 2>/dev/null \
      | openssl x509 -noout -dates -issuer -subject

`notAfter` in the past confirms it. Two details worth noting: always pass
`-servername`, because without SNI you may be shown a default certificate that is
perfectly valid and not the one users are getting; and check `-issuer`, because a
Let's Encrypt staging issuer means renewal succeeded against the wrong ACME
endpoint.

If cert-manager manages the certificate:

    kubectl get certificate -A
    kubectl describe certificate <name> -n <namespace>
    kubectl get certificaterequest,order,challenge -n <namespace>

The failure is almost always visible on the Challenge or Order resource, not on the
Certificate.

## Likely causes

- **HTTP-01 challenge blocked.** An ingress rule, WAF, or auth middleware intercepts
  `/.well-known/acme-challenge/` before it reaches the solver pod. Anything forcing
  authentication site-wide breaks renewal silently.
- **DNS-01 credentials expired** or the zone moved to another provider.
- **Rate limited by Let's Encrypt** after repeated failed attempts — the limit is per
  registered domain per week, and retries burn it fast.
- **Certificate renewed but not reloaded.** The Secret was updated; the ingress
  controller cached the old one.
- **A manually issued certificate** nobody owns, with no renewal at all.

## Resolution

1. If cert-manager is stuck, read the Challenge resource first — it states the
   validation error verbatim. Fix that, then delete the Challenge to force a retry.
2. Verify the ACME solver path is reachable unauthenticated from the public
   internet. Curl it from outside, not from inside the cluster.
3. Confirm the renewed Secret and the served certificate agree. If the Secret is
   fresh but `openssl s_client` still shows the old one, restart the ingress
   controller pods.
4. In an emergency, a certificate from an alternate issuer or a temporary
   provider-managed certificate at the load balancer restores service faster than
   debugging ACME under pressure.

## Escalation

If renewal has been failing silently for weeks, the gap is monitoring, not TLS.
Alert on `certmanager_certificate_expiration_timestamp_seconds` at 21 days
remaining, so expiry becomes a ticket rather than an incident.
