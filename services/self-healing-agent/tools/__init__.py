"""The tool registry: one `ToolSpec` per tool, filtered two ways rather than built as
two systems. Day 17's loop passes `READ_ONLY`; Day 18 passes `ALL`. Neither day changes
a signature here -- see docs/superpowers/specs/2026-08-11-week3-agent-design.md, decision 2.

`needs` exists only so a test can compare it against k8s/rbac.yaml's actual verbs --
written a week apart, a Role and a tool list drift, and the drift should fail a test
before it fails a 403 in front of a human on Day 21.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .external import get_recent_alerts, search_runbooks
from .k8s import get_pod_logs, get_recent_deploys, restart_pod, scale_deployment


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict  # JSON Schema for the arguments object
    fn: Callable[..., dict]  # not passed to the model, only dispatched by the loop
    write: bool
    needs: tuple[str, ...] = field(default_factory=tuple)


def submit_diagnosis(
    apis,
    *,
    summary: str,
    evidence: list[str],
    confidence: float,
    proposed_action: dict | None = None,
) -> dict:
    """The loop's exit condition. Day 17's agent.py intercepts this call by name before
    generic dispatch -- this body only runs if something calls it outside that loop."""
    return {
        "summary": summary,
        "evidence": evidence,
        "proposed_action": proposed_action,
        "confidence": confidence,
    }


_SPECS = [
    ToolSpec(
        name="get_pod_logs",
        description="Read recent log lines from one pod's container.",
        schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod": {"type": "string"},
                "container": {
                    "type": "string",
                    "description": "Omit for a single-container pod.",
                },
                "tail_lines": {"type": "integer", "default": 200},
            },
            "required": ["namespace", "pod"],
        },
        fn=get_pod_logs,
        write=False,
        needs=("core/pods/log:get",),
    ),
    ToolSpec(
        name="get_recent_alerts",
        description="List alerts firing or recently resolved, optionally for one service.",
        schema={
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "since_minutes": {"type": "integer", "default": 60},
            },
            "required": [],
        },
        fn=get_recent_alerts,
        write=False,
        needs=(),
    ),
    ToolSpec(
        name="get_recent_deploys",
        description="List recent ReplicaSet revisions for a Deployment.",
        schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "deployment": {"type": "string"},
            },
            "required": ["namespace", "deployment"],
        },
        fn=get_recent_deploys,
        write=False,
        needs=("apps/replicasets:list",),
    ),
    ToolSpec(
        name="restart_pod",
        description="Delete one pod by exact name; its ReplicaSet recreates it.",
        schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod": {"type": "string"},
            },
            "required": ["namespace", "pod"],
        },
        fn=restart_pod,
        write=True,
        needs=("core/pods:delete",),
    ),
    ToolSpec(
        name="scale_deployment",
        description="Scale a Deployment to a replica count, clamped to a safe range.",
        schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "deployment": {"type": "string"},
                "replicas": {"type": "integer"},
            },
            "required": ["namespace", "deployment", "replicas"],
        },
        fn=scale_deployment,
        write=True,
        needs=("apps/deployments/scale:patch",),
    ),
    ToolSpec(
        name="search_runbooks",
        description="Search runbooks and postmortems for relevant guidance.",
        schema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "k": {"type": "integer", "default": 4},
            },
            "required": ["question"],
        },
        fn=search_runbooks,
        write=False,
        needs=(),
    ),
    ToolSpec(
        name="submit_diagnosis",
        description="End the loop with a diagnosis: what's wrong, the evidence for it, "
        "an optional proposed action, and a confidence score.",
        schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "proposed_action": {"type": ["object", "null"]},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["summary", "evidence", "confidence"],
        },
        fn=submit_diagnosis,
        write=False,
        needs=(),
    ),
]

REGISTRY: dict[str, ToolSpec] = {spec.name: spec for spec in _SPECS}
READ_ONLY: tuple[str, ...] = tuple(
    name for name, spec in REGISTRY.items() if not spec.write
)
ALL: tuple[str, ...] = tuple(REGISTRY)


def as_model_tools(names: tuple[str, ...] = ALL) -> list[dict[str, Any]]:
    """`{name, description, schema}` dicts -- the shape provider.chat()'s `tools`
    parameter expects, with `fn`, `write` and `needs` left out because none of those
    are the model's business."""
    return [
        {
            "name": REGISTRY[name].name,
            "description": REGISTRY[name].description,
            "schema": REGISTRY[name].schema,
        }
        for name in names
    ]
