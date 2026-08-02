---
title: In-cluster DNS resolution failing
service: platform
doc_type: runbook
last_reviewed: 2026-07-11
---

## Symptom

Applications intermittently fail to reach other services by name. Logs show "no such
host", "temporary failure in name resolution", or connection timeouts to a hostname
that clearly exists. The failures are often partial — some pods are fine, some
requests succeed on retry — which makes it look like a network problem rather than a
DNS one.

## How to confirm

Test resolution from inside the cluster, from a pod in the affected namespace:

    kubectl run dnstest --rm -it --image=busybox:1.36 --restart=Never -- sh
    nslookup kubernetes.default.svc.cluster.local
    nslookup <service>.<namespace>.svc.cluster.local

Then check the resolvers themselves:

    kubectl -n kube-system get pods -l k8s-app=kube-dns
    kubectl -n kube-system logs -l k8s-app=kube-dns --tail=100

Restart counts on the CoreDNS pods are the detail people skip. A CoreDNS pod that has
restarted recently was probably OOMKilled, and the intermittency is explained by
requests landing on a replica that is currently down.

## Likely causes

- **CoreDNS under-resourced.** The default memory limit is modest and does not scale
  with cluster size or query volume. Under load it gets OOMKilled, and because there
  are usually only two replicas, losing one halves capacity.
- **`ndots:5` amplification.** The default pod `resolv.conf` sets `ndots:5`, so any
  name with fewer than five dots — including every external hostname like
  `api.stripe.com` — is first tried against each cluster search domain. One external
  lookup becomes four or five queries. This is normal, and it is also why DNS load is
  often far higher than anyone expects.
- **Upstream resolver slow or unreachable.** CoreDNS forwards anything it does not
  own; if the upstream times out, in-cluster lookups queue behind it.
- **NetworkPolicy blocking egress to kube-dns** after a default-deny policy was
  applied to a namespace. Easy to miss, because the policy looks correct for
  application traffic.
- **Node-level conntrack exhaustion** dropping UDP responses.

## Resolution

1. If CoreDNS pods are restarting, raise their memory limit and increase the replica
   count. Two replicas is not enough for a busy cluster.
2. For workloads making many external calls, set `dnsConfig.options` with `ndots: 1`
   on the pod spec, or use fully qualified names ending in a dot so the search list is
   skipped entirely.
3. Enable the CoreDNS `cache` plugin if it is not already on, and confirm `forward`
   points at a healthy upstream with a sane timeout.
4. If a default-deny NetworkPolicy is in place, add an explicit egress rule allowing
   both UDP and TCP port 53 to the kube-system namespace. Both protocols — large
   responses fall back to TCP.
5. For conntrack drops, consider NodeLocal DNSCache, which keeps lookups on the node
   and avoids the UDP round trip across the overlay.

## Escalation

If resolution fails cluster-wide rather than per namespace, treat it as a control
plane incident. Check whether the kube-dns Service has endpoints at all —
`kubectl -n kube-system get endpoints kube-dns`. No endpoints means the CoreDNS pods
are not passing readiness, and every workload in the cluster is affected.
