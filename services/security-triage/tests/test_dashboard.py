"""The committed Grafana dashboard, checked against the metrics it queries."""

import json
import re
from pathlib import Path

import pytest

import metrics

DASHBOARD = (
    Path(__file__).resolve().parents[1]
    / "observability/grafana/dashboards/security-triage.json"
)

# prometheus_client renames on the wire: `_total` on counters, _bucket/_count/_sum
# on histograms.
_SUFFIXES = {
    "Counter": {"_total": set(), "_created": set()},
    "Histogram": {"_bucket": {"le"}, "_count": set(), "_sum": set()},
    "Gauge": {"": set()},
}


def exposed() -> dict[str, set[str]]:
    """{metric name on the wire: its label names}."""
    out: dict[str, set[str]] = {}
    for obj in vars(metrics).values():
        for suffix, extra in _SUFFIXES.get(type(obj).__name__, {}).items():
            out[obj._name + suffix] = set(obj._labelnames) | extra
    return out


def expressions() -> list[tuple[str, str]]:
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    return [
        (panel["title"], target["expr"])
        for panel in dashboard["panels"]
        for target in panel["targets"]
    ]


def test_the_dashboard_exists_and_has_panels():
    assert DASHBOARD.is_file(), f"{DASHBOARD} is missing"
    assert expressions(), "the dashboard queries nothing"


@pytest.mark.parametrize("title,expr", expressions())
def test_every_panel_queries_a_metric_this_service_exports(title, expr):
    names = set(re.findall(r"\bst_[a-z_]+\b", expr))
    assert names, f"{title}: no st_ metric in the expression at all"
    unknown = names - set(exposed())
    assert not unknown, f"{title}: {unknown} are not exported by metrics.py"


@pytest.mark.parametrize("title,expr", expressions())
def test_every_label_a_panel_uses_exists_on_the_metric(title, expr):
    """A `by (repo)` on a metric without that label collapses every series into one."""
    names = set(re.findall(r"\bst_[a-z_]+\b", expr))
    available = {label for name in names for label in exposed().get(name, ())}

    used = set()
    for selector in re.findall(r"st_[a-z_]+\{([^}]*)\}", expr):
        used |= set(re.findall(r"(\w+)\s*[=!~]", selector))
    for group in re.findall(r"by \(([^)]*)\)", expr):
        used |= {part.strip() for part in group.split(",") if part.strip()}

    assert used <= available, f"{title}: {used - available} absent from {names}"


def test_the_datasource_is_a_placeholder_not_a_hardcoded_uid():
    """A real uid imports against nothing elsewhere, and the panels are silently empty."""
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    assert dashboard["__inputs"][0]["name"] == "DS_PROMETHEUS"
    for panel in dashboard["panels"]:
        assert panel["datasource"] == "${DS_PROMETHEUS}", panel["title"]
        for target in panel["targets"]:
            assert target["datasource"] == "${DS_PROMETHEUS}", panel["title"]


def test_every_panel_says_why_it_is_there():
    """It is the only place a reader learns a low number is sometimes the healthy one."""
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    for panel in dashboard["panels"]:
        assert panel.get("description", "").strip(), panel["title"]
