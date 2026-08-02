---
title: Pod OOMKilled and restarting
service: platform
doc_type: runbook
last_reviewed: 2026-07-15
---

## Symptom

Pods in a deployment restart on a loop. `kubectl get pods` shows a RESTARTS count
climbing every few minutes, and the pod alternates between Running and
CrashLoopBackOff. Users see intermittent 502s from the ingress while the replica
is unavailable.

## How to confirm

Run `kubectl describe pod <name>` and read the Last State block. An out-of-memory
kill shows `Reason: OOMKilled` with `Exit Code: 137` — the kernel's SIGKILL
(128 + 9). Note that this is a kill, not a crash: the process never got a chance
to log anything on the way out, so application logs end mid-sentence with no
stack trace. That silence is itself the signal.

Confirm against metrics before changing anything:

    container_memory_working_set_bytes{pod="<name>"}

Compare it to `kube_pod_container_resource_limits{resource="memory"}`. Working set
is what the kernel actually counts against the limit — resident set size alone
understates it, because page cache the container touched is included.

If the working set climbs steadily between restarts and never plateaus, that is a
leak. If it sits flat and only spikes under load, the limit is simply too low.

## Likely causes

- **Limit set too low for real traffic.** Common when limits were copied from a
  template or sized during a quiet period.
- **A genuine leak.** JVM heap growing without bound, an unbounded in-memory
  cache, goroutines or connections never released.
- **Runtime unaware of the cgroup limit.** An older JVM defaults its max heap to a
  fraction of *host* memory, not the container limit, so it allocates past the
  ceiling and gets killed.
- **A burst workload** — a large request body, an unpaginated query, a batch job
  sharing the pod.
- **Sidecar contention.** The limit is per-container, but a log shipper or mesh
  proxy in the same pod may be the one actually consuming.

## Resolution

Raising the limit is the fast mitigation, not the fix. Do it to stop the bleeding,
then find out which cause above applies.

1. Raise `resources.limits.memory` by roughly 50% and roll the deployment.
2. Watch the working set for one full traffic cycle. Flat means it was undersized.
   Still climbing means a leak, and you have only bought time.
3. For JVM workloads, set `-XX:MaxRAMPercentage=75` so heap is derived from the
   cgroup limit rather than host memory.
4. Keep the memory request equal to the limit for anything latency-sensitive. That
   puts the pod in the Guaranteed QoS class so it is evicted last under node
   pressure.

Never raise the limit past what the node can schedule. A pod that cannot fit
anywhere stays Pending, which is a louder outage than a restart loop.

## Escalation

If the working set doubles within an hour of a restart, treat it as a leak and
page the owning team rather than continuing to raise the limit. Attach a heap dump
or pprof profile taken before the next kill.
