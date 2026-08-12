from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests
from kubernetes.client.exceptions import ApiException

import tools.external as external
import tools.k8s as k8s_tools
from errors import K8sError, RunbookError, UpstreamError


class FakeCoreV1Api:
    """Records every call it's asked to make, so a test can assert on the exact pod
    named rather than trusting the tool ran something."""

    def __init__(self) -> None:
        self.log_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.log_response = "line one\nline two\n"
        self.raises: Exception | None = None

    def read_namespaced_pod_log(
        self, name, namespace, container=None, tail_lines=None, **kwargs
    ):
        self.log_calls.append(
            {
                "name": name,
                "namespace": namespace,
                "container": container,
                "tail_lines": tail_lines,
            }
        )
        if self.raises:
            raise self.raises
        return self.log_response

    def delete_namespaced_pod(self, name, namespace, **kwargs):
        self.delete_calls.append({"name": name, "namespace": namespace})
        if self.raises:
            raise self.raises


class FakeAppsV1Api:
    def __init__(self) -> None:
        self.list_calls: list[dict] = []
        self.patch_calls: list[dict] = []
        self.replica_sets: list = []
        self.raises: Exception | None = None

    def list_namespaced_replica_set(self, namespace, **kwargs):
        self.list_calls.append({"namespace": namespace})
        if self.raises:
            raise self.raises
        return SimpleNamespace(items=self.replica_sets)

    def patch_namespaced_deployment_scale(self, name, namespace, body, **kwargs):
        self.patch_calls.append({"name": name, "namespace": namespace, "body": body})
        if self.raises:
            raise self.raises


def make_replicaset(deployment, revision, replicas, ready, image, created):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            owner_references=[SimpleNamespace(kind="Deployment", name=deployment)],
            annotations={"deployment.kubernetes.io/revision": revision},
            creation_timestamp=created,
        ),
        spec=SimpleNamespace(
            replicas=replicas,
            template=SimpleNamespace(
                spec=SimpleNamespace(containers=[SimpleNamespace(image=image)])
            ),
        ),
        status=SimpleNamespace(ready_replicas=ready),
    )


@pytest.fixture
def apis():
    return FakeCoreV1Api(), FakeAppsV1Api()


def test_get_pod_logs_calls_the_named_pod_and_clamps_tail_lines(apis):
    core, _ = apis
    result = k8s_tools.get_pod_logs(
        apis, namespace="sandbox", pod="api-7f9", tail_lines=999_999
    )

    assert core.log_calls == [
        {
            "name": "api-7f9",
            "namespace": "sandbox",
            "container": None,
            "tail_lines": k8s_tools.MAX_TAIL_LINES,
        }
    ]
    assert result["logs"] == core.log_response
    assert result["tail_lines"] == k8s_tools.MAX_TAIL_LINES


def test_get_pod_logs_wraps_the_api_error_with_its_status(apis):
    core, _ = apis
    core.raises = ApiException(status=404, reason="Not Found")

    with pytest.raises(K8sError) as exc:
        k8s_tools.get_pod_logs(apis, namespace="sandbox", pod="ghost")
    assert exc.value.status == 404


def test_restart_pod_deletes_exactly_the_named_pod(apis):
    core, _ = apis
    result = k8s_tools.restart_pod(apis, namespace="sandbox", pod="api-7f9")

    assert core.delete_calls == [{"name": "api-7f9", "namespace": "sandbox"}]
    assert result == {"namespace": "sandbox", "pod": "api-7f9"}


def test_get_recent_deploys_filters_by_owner_and_sorts_newest_first(apis):
    _, apps = apis
    older = make_replicaset(
        "checkout-api",
        "1",
        2,
        2,
        "checkout:v1",
        datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    newer = make_replicaset(
        "checkout-api",
        "2",
        3,
        1,
        "checkout:v2",
        datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    unrelated = make_replicaset(
        "other-api", "1", 1, 1, "other:v1", datetime(2026, 8, 5, tzinfo=timezone.utc)
    )
    apps.replica_sets = [older, unrelated, newer]

    result = k8s_tools.get_recent_deploys(
        apis, namespace="sandbox", deployment="checkout-api"
    )

    assert [r["revision"] for r in result["revisions"]] == ["2", "1"]
    assert result["revisions"][0]["image"] == "checkout:v2"


def test_scale_deployment_clamps_to_the_configured_range(apis):
    _, apps = apis
    result = k8s_tools.scale_deployment(
        apis, namespace="sandbox", deployment="checkout-api", replicas=999
    )

    assert result["replicas"] == k8s_tools.MAX_REPLICAS
    assert result["requested_replicas"] == 999
    assert apps.patch_calls[0]["body"] == {"spec": {"replicas": k8s_tools.MAX_REPLICAS}}


def test_scale_deployment_wraps_the_api_error_with_its_status(apis):
    _, apps = apis
    apps.raises = ApiException(status=403, reason="Forbidden")

    with pytest.raises(K8sError) as exc:
        k8s_tools.scale_deployment(
            apis, namespace="kube-system", deployment="coredns", replicas=1
        )
    assert exc.value.status == 403


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.exceptions.HTTPError(f"{self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self.payload


def test_get_recent_alerts_filters_by_service_and_time_window(monkeypatch):
    now = datetime.now(timezone.utc)
    alerts = [
        {
            "labels": {
                "alertname": "PodOOMKilled",
                "service": "checkout-api",
                "severity": "critical",
            },
            "annotations": {"summary": "checkout-api OOMKilled"},
            "startsAt": (now - timedelta(minutes=5)).isoformat(),
            "status": {"state": "active"},
        },
        {
            # Different service -- must be filtered out even though it's recent.
            "labels": {"alertname": "DiskFull", "service": "other-api"},
            "annotations": {},
            "startsAt": (now - timedelta(minutes=5)).isoformat(),
            "status": {"state": "active"},
        },
        {
            # Same service, but outside the window.
            "labels": {"alertname": "Stale", "service": "checkout-api"},
            "annotations": {},
            "startsAt": (now - timedelta(minutes=120)).isoformat(),
            "status": {"state": "active"},
        },
    ]
    monkeypatch.setattr(external.requests, "get", lambda *a, **k: FakeResponse(alerts))

    result = external.get_recent_alerts(None, service="checkout-api", since_minutes=60)

    assert [a["alertname"] for a in result["alerts"]] == ["PodOOMKilled"]


def test_get_recent_alerts_upstream_failure_raises_upstream_error(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("no route to host")

    monkeypatch.setattr(external.requests, "get", boom)

    with pytest.raises(UpstreamError) as exc:
        external.get_recent_alerts(None)
    assert exc.value.provider == "alertmanager"


def test_search_runbooks_returns_the_copilots_hits(monkeypatch):
    monkeypatch.setattr(
        external.requests,
        "post",
        lambda *a, **k: FakeResponse({"hits": [{"text": "raise the memory limit"}]}),
    )

    result = external.search_runbooks(None, question="why is checkout OOMKilled")

    assert result["hits"][0]["text"] == "raise the memory limit"


def test_search_runbooks_propagates_the_copilots_status(monkeypatch):
    monkeypatch.setattr(
        external.requests, "post", lambda *a, **k: FakeResponse({}, status_code=503)
    )

    with pytest.raises(RunbookError) as exc:
        external.search_runbooks(None, question="why is checkout OOMKilled")
    assert exc.value.status == 503
