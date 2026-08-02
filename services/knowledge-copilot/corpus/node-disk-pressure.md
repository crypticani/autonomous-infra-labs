---
title: Node under DiskPressure, pods being evicted
service: platform
doc_type: runbook
last_reviewed: 2026-06-19
---

## Symptom

Pods are evicted from a node without anyone deploying anything. `kubectl get nodes`
shows the node still Ready, but `kubectl describe node <name>` lists a
`DiskPressure` condition set to True. Evicted pods appear with status Evicted and a
message about the node being low on resources.

## How to confirm

    kubectl describe node <name> | grep -A5 Conditions
    kubectl get events --field-selector reason=Evicted -A

The kubelet raises DiskPressure when free space or free inodes on the filesystem
backing the container runtime crosses its eviction threshold — by default 10% free
for a soft eviction and 5% for a hard one.

On the node itself, the distinction that matters is *which* filesystem:

    df -h /var/lib/containerd /var/lib/kubelet
    df -i /var/lib/containerd

Running out of inodes while bytes are plentiful is a real and easily missed case,
usually caused by millions of tiny files in emptyDir volumes or logs.

## Likely causes

- **Image cache growth.** Nodes that have run many deploys accumulate old image
  layers. Garbage collection is threshold-driven, so it may not have kicked in.
- **Container logs never rotated.** A chatty pod writing to stdout fills
  `/var/log/pods`. Without `containerLogMaxSize` set, this grows unbounded.
- **emptyDir volumes** used as scratch space by a job that does not clean up.
- **A pod writing to its own container filesystem** instead of a volume.
- **Inode exhaustion** rather than byte exhaustion.

## Resolution

Understand the eviction order before intervening, because the kubelet has already
chosen victims: BestEffort pods go first, then Burstable pods exceeding their
requests, then Guaranteed pods. If your critical workload was evicted, its QoS
class is the real finding.

1. Reclaim space immediately:

       crictl rmi --prune
       journalctl --vacuum-size=200M

2. Find the actual consumer rather than guessing:

       du -xh /var/lib --max-depth=2 | sort -h | tail -20

3. Set log rotation in the kubelet config — `containerLogMaxSize: 50Mi` and
   `containerLogMaxFiles: 3` — so a single noisy pod cannot fill the disk again.
4. Give scratch-heavy pods `ephemeral-storage` requests and limits. Without them
   the scheduler has no idea they need space, and the kubelet cannot evict the
   right pod when they overrun.
5. If the node is chronically tight, grow the disk or move to a larger instance.
   Repeated manual pruning is a signal, not a fix.

## Escalation

If DiskPressure recurs on multiple nodes within a week, stop treating it per node.
Look for a recently deployed workload with unbounded logging or scratch usage — the
pattern is almost always one new tenant, not gradual drift.
