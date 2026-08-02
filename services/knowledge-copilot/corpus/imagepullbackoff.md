---
title: Pod stuck in ImagePullBackOff
service: platform
doc_type: runbook
last_reviewed: 2026-07-02
---

## Symptom

A newly created or newly rolled pod never starts. `kubectl get pods` shows
ErrImagePull briefly, then settles into ImagePullBackOff. No application logs
exist at all, because no container was ever created.

## How to confirm

The Events section of `kubectl describe pod <name>` carries the actual error from
the container runtime. Read it literally — the four common messages point at four
different problems:

- `manifest unknown` — the tag does not exist in the registry. A typo, or a CI job
  that failed to push before the deploy ran.
- `unauthorized` / `authentication required` — the node has no valid credentials
  for a private registry.
- `denied` — credentials were accepted but lack pull permission for that repo.
- `toomanyrequests` — a registry rate limit, most often anonymous Docker Hub pulls.

Events age out. If describe shows nothing useful, this will still have it:

    kubectl get events --sort-by=.lastTimestamp -n <namespace>

## Likely causes

- **Tag does not exist.** Deploying `:latest` when CI pushed a SHA tag, or a deploy
  that raced ahead of the image build.
- **Missing `imagePullSecrets`.** The secret exists but is not referenced by the pod
  spec or attached to the ServiceAccount. Image pull secrets are namespaced — a
  secret in `default` does nothing for a pod in `staging`.
- **Expired registry credentials.** ECR tokens last 12 hours; a statically created
  `docker-registry` secret for ECR starts failing overnight.
- **Docker Hub rate limiting** on unauthenticated pulls, which bites hardest when a
  node is recycled and its image cache is cold.
- **Node cannot reach the registry** — egress firewall, missing NAT, or a proxy the
  runtime is not configured to use.

## Resolution

1. Confirm the tag exists before touching anything else:

       crane manifest <registry>/<repo>:<tag>

2. For auth failures, verify the secret is both present and referenced:

       kubectl get secret <name> -n <namespace>
       kubectl get sa default -n <namespace> -o yaml

   Attaching it to the ServiceAccount is usually preferable to repeating it in
   every pod spec.

3. For ECR, do not create the secret by hand. Use IRSA or instance-profile based
   auth, or a controller that refreshes the token before it expires.

4. For rate limits, authenticate the pull even for public images, or mirror the
   image into your own registry. Pinning by digest also helps, since a cached
   digest will not be re-pulled.

## Escalation

If one node pulls successfully and another does not, the problem is that node's
network or credentials, not the manifest. Cordon it and compare its egress path
before draining.
