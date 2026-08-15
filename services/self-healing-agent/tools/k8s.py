"""The four tools that touch a cluster. Every function takes `apis` -- the
`(CoreV1Api, AppsV1Api)` pair from k8s_client.get_apis() -- as its first argument rather
than importing that module itself, so every test here passes a fake pair and none needs
a real cluster.

None of these wrap their result as {"output": ...} or {"error": ...}: that convention is
the loop's (Day 17), applied uniformly to whatever a tool returns or raises. A tool
returns its data, or raises the typed error that says what actually went wrong.
"""

import os

from kubernetes.client.exceptions import ApiException

from errors import K8sError

# Read once at import, matching retrieval.py's SIMILARITY_FLOOR pattern: a bad value
# fails at startup, not on the first scale_deployment call.
MIN_REPLICAS = int(os.getenv("SHA_MIN_REPLICAS", "1"))
MAX_REPLICAS = int(os.getenv("SHA_MAX_REPLICAS", "10"))

# tail_lines is a model-chosen argument. Clamping it server-side, rather than trusting
# it, means a hallucinated 10_000_000 costs one clamp instead of one very large log pull.
MAX_TAIL_LINES = 2000

# A revision list is for spotting what recently changed, not a full history. A busy
# deployment can accumulate hundreds of ReplicaSets; the model needs the last few.
MAX_REVISIONS = 5


def get_pod_logs(
    apis,
    *,
    namespace: str,
    pod: str,
    container: str | None = None,
    tail_lines: int = 200,
) -> dict:
    core, _ = apis
    clamped = max(1, min(tail_lines, MAX_TAIL_LINES))
    try:
        logs = core.read_namespaced_pod_log(
            name=pod, namespace=namespace, container=container, tail_lines=clamped
        )
    except ApiException as e:
        raise K8sError(
            f"could not read logs for pod {pod!r} in {namespace!r}: {e.reason}",
            status=e.status or 502,
        ) from e
    return {
        "namespace": namespace,
        "pod": pod,
        "container": container,
        "tail_lines": clamped,
        "logs": logs,
    }


def _owned_by_deployment(replica_set, deployment: str) -> bool:
    return any(
        owner.kind == "Deployment" and owner.name == deployment
        for owner in (replica_set.metadata.owner_references or [])
    )


def _summarize_replicaset(replica_set) -> dict:
    containers = replica_set.spec.template.spec.containers or []
    created = replica_set.metadata.creation_timestamp
    return {
        "revision": (replica_set.metadata.annotations or {}).get(
            "deployment.kubernetes.io/revision"
        ),
        "replicas": replica_set.spec.replicas,
        "ready_replicas": replica_set.status.ready_replicas,
        "image": containers[0].image if containers else None,
        "created_at": created.isoformat() if created else None,
    }


def get_recent_deploys(apis, *, namespace: str, deployment: str) -> dict:
    """Real rollout history from ReplicaSet revisions, not a changelog that can go stale.

    Filtered by ownerReferences rather than the Deployment's own selector, because
    reading that selector needs `deployments: get` -- a verb this tool has no other use
    for, and one more thing the Role would have to grant.
    """
    _, apps = apis
    try:
        listing = apps.list_namespaced_replica_set(namespace=namespace)
    except ApiException as e:
        raise K8sError(
            f"could not list replicasets in {namespace!r}: {e.reason}",
            status=e.status or 502,
        ) from e

    owned = [rs for rs in listing.items if _owned_by_deployment(rs, deployment)]
    owned.sort(key=lambda rs: rs.metadata.creation_timestamp, reverse=True)
    return {
        "namespace": namespace,
        "deployment": deployment,
        "revisions": [_summarize_replicaset(rs) for rs in owned[:MAX_REVISIONS]],
    }


def restart_pod(apis, *, namespace: str, pod: str) -> dict:
    """Deletes one pod by exact name; the ReplicaSet recreates it.

    Not `kubectl rollout restart`: that patches the Deployment's pod template, which
    needs `patch` on `deployments` -- the same verb that can change an image. Deleting
    one pod needs only `delete` on `pods`, and cannot do anything else.
    """
    core, _ = apis
    try:
        core.delete_namespaced_pod(name=pod, namespace=namespace)
    except ApiException as e:
        raise K8sError(
            f"could not delete pod {pod!r} in {namespace!r}: {e.reason}",
            status=e.status or 502,
        ) from e
    return {"namespace": namespace, "pod": pod}


def current_replicas(apis, *, namespace: str, deployment: str) -> int:
    """Not a tool -- it is not in _SPECS, so the model never sees it. guardrails.py calls
    it to compare an approved scale against the cluster as it is at click time.

    Reads the scale subresource rather than the Deployment, which is why the Role can
    grant `get` on deployments/scale and still expose no image, no env var, no mounted
    secret. The one number a guardrail needs is the only number this can read.
    """
    _, apps = apis
    try:
        scale = apps.read_namespaced_deployment_scale(
            name=deployment, namespace=namespace
        )
    except ApiException as e:
        raise K8sError(
            f"could not read scale for deployment {deployment!r} in {namespace!r}: "
            f"{e.reason}",
            status=e.status or 502,
        ) from e
    return int(scale.spec.replicas or 0)


def scale_deployment(apis, *, namespace: str, deployment: str, replicas: int) -> dict:
    """Clamped, not rejected: a model-chosen replica count outside the sane range is
    corrected to the nearest bound rather than failing the whole diagnosis over it.

    The clamp is for a number the model invented; guardrails._check_replica_floor is the
    harder stop for a number a human is about to approve, and it refuses rather than
    corrects -- silently turning "scale to 0" into "scale to 1" would execute something
    nobody clicked.
    """
    _, apps = apis
    clamped = max(MIN_REPLICAS, min(replicas, MAX_REPLICAS))
    try:
        apps.patch_namespaced_deployment_scale(
            name=deployment, namespace=namespace, body={"spec": {"replicas": clamped}}
        )
    except ApiException as e:
        raise K8sError(
            f"could not scale deployment {deployment!r} in {namespace!r}: {e.reason}",
            status=e.status or 502,
        ) from e
    return {
        "namespace": namespace,
        "deployment": deployment,
        "requested_replicas": replicas,
        "replicas": clamped,
    }
