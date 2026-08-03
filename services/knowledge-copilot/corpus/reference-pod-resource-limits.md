---
title: Reference — pod resource requests and limits
service: platform
doc_type: reference
last_reviewed: 2026-07-20
---

## Requests vs limits

A **request** is what the scheduler reserves; it decides which node a pod lands on. A **limit**
is the hard ceiling the kernel enforces at runtime. They are different knobs and are often set
wrong by being set equal without thinking, or by omitting one entirely.

- CPU is compressible: exceeding the CPU limit throttles the container, it does not kill it.
- Memory is not compressible: exceeding the memory limit gets the container OOMKilled
  (Exit Code 137). There is no throttling — it is a hard kill.

## QoS classes

Kubernetes derives a Quality of Service class from requests and limits, and it decides
eviction order under node pressure.

- **Guaranteed** — every container has requests equal to limits for both CPU and memory.
  Evicted last. Use for latency-sensitive workloads.
- **Burstable** — at least one request set, but not equal to limits. Evicted after BestEffort.
- **BestEffort** — no requests or limits at all. Evicted first. Avoid in production.

## Sizing memory

Size against `container_memory_working_set_bytes`, not RSS — the working set is what the kernel
counts against the limit, and it includes page cache the container has touched. Give headroom
above the observed peak; a limit at exactly peak will OOMKill on the next traffic spike.

For JVM workloads set `-XX:MaxRAMPercentage` so the heap derives from the cgroup limit rather
than host memory; older JVMs default to a fraction of *host* RAM and allocate straight past a
container limit.

## Common mistakes

- Copying limits from a template and never revisiting them (see the checkout OOM postmortem).
- Setting a memory limit far above what the node can schedule — the pod stays Pending, a
  louder outage than a restart loop.
- Omitting requests, landing the pod in BestEffort, and being surprised when it is evicted
  first under pressure.
