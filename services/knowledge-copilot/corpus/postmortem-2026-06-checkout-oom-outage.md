---
title: Postmortem — checkout service OOM outage
service: platform
doc_type: postmortem
last_reviewed: 2026-06-14
---

## Summary

On 2026-06-11 between 14:02 and 14:47 UTC the checkout service returned intermittent 502s.
Root cause was an OOMKill loop on the `checkout` deployment after a release raised per-request
memory use without a matching limit increase. Impact: ~12% of checkout attempts failed for 45
minutes. No data loss.

## Timeline

- 14:02 — Deploy of checkout v4.19 completes. Memory limit unchanged at 512Mi.
- 14:06 — First `OOMKilled` (Exit Code 137). Pod restarts; RESTARTS count begins climbing.
- 14:11 — Alertmanager fires `KubePodCrashLooping`. On-call paged.
- 14:25 — Working set confirmed pinned at the 512Mi ceiling under normal traffic.
- 14:38 — Limit raised to 768Mi and deployment rolled.
- 14:47 — Restart loop stops; 502 rate returns to baseline.

## Root cause

v4.19 added an in-memory response cache with no size bound. Under production traffic the
working set exceeded the 512Mi limit the pod had carried since it was first sized in a quiet
period. The kernel SIGKILLed the container (137 = 128 + 9); because it was a kill, not a
crash, application logs ended mid-request with no stack trace — the silence matched the
`oomkilled-pod` runbook exactly.

## What went wrong beyond the bug

The limit had been copied from a template and never revisited. There was no memory headroom
alert, so the first signal was user-facing 502s rather than a rising working set.

## Follow-ups

- Bound the response cache and add a regression test on peak RSS. (done)
- Alert on `working_set / limit > 0.85` for latency-sensitive deployments. (done)
- Set memory request equal to limit on checkout for Guaranteed QoS. (done)
- Add a release checklist item: re-check limits when a change touches caching or buffering.
