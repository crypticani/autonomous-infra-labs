---
title: Container in CrashLoopBackOff
service: platform
doc_type: runbook
last_reviewed: 2026-06-28
---

## Symptom

A pod never reaches a steady Running state. `kubectl get pods` reports
CrashLoopBackOff and the restart count increases, with progressively longer gaps
between attempts — 10s, 20s, 40s, doubling up to a five-minute ceiling. That
backoff is deliberate: the kubelet is protecting the node from a pod that would
otherwise restart in a tight loop.

## How to confirm

CrashLoopBackOff is a *state*, not a cause. It only tells you the container keeps
exiting. Get the real reason from the previous container's logs:

    kubectl logs <pod> --previous

The `--previous` flag matters. Without it you get the container that is currently
starting, which is usually empty, and it is easy to conclude there is nothing to
see.

Then check the exit code in `kubectl describe pod <name>` under Last State:

- **0** — the process ran to completion and exited cleanly. Kubernetes restarted it
  because `restartPolicy: Always` expects a long-running process. Usually a
  command that returns instead of blocking.
- **1** or another small number — the application chose to exit. The logs say why.
- **137** — SIGKILL, almost always an out-of-memory kill. See the OOMKilled runbook.
- **143** — SIGTERM. Something asked it to stop.

## Likely causes

- **Missing or malformed configuration.** An absent environment variable, a
  ConfigMap key that was renamed, a secret that exists in one namespace but not
  this one. The app validates config at boot and exits.
- **A failing database migration** run in an init container or on startup.
- **A dependency not yet reachable** — the app dials a service at boot and exits
  rather than retrying.
- **A liveness probe misconfigured.** Too short an `initialDelaySeconds` will kill
  a slow-starting app before it is ready, forever.
- **Wrong command or entrypoint** after an image change.

## Resolution

1. Read `--previous` logs first. Most of the time the application states the
   problem plainly.
2. If the container exits before logging anything, override the entrypoint and
   inspect from the inside:

       kubectl run debug --rm -it --image=<same-image> --command -- sh

3. For probe-induced kills, raise `initialDelaySeconds` or switch to a
   `startupProbe`, which gives slow boots a generous window without loosening the
   liveness check afterwards.
4. For missing config, verify the ConfigMap and Secret exist in the *pod's*
   namespace and that key names match exactly:

       kubectl get secret <name> -n <namespace> -o yaml

## Escalation

If the image runs correctly locally with the same environment, suspect an
admission controller, a mutating webhook, or a PodSecurity policy altering the
spec. Compare `kubectl get pod <name> -o yaml` against what you applied.
