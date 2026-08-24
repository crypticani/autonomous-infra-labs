"""The tool registry: one `ToolSpec` per tool, filtered two ways rather than built as two
systems. The loop passes `READ_ONLY`, the approval gate passes `ALL`, and no signature
here changes between them.
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
        # `get` belongs here even though scale_deployment never calls it: guardrails.py reads it
        # before letting the tool run, and test_rbac.py compares this tuple against the Role.
        needs=("apps/deployments/scale:get", "apps/deployments/scale:patch"),
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
]

# Derived, never written twice: submit_diagnosis constrains `proposed_action.tool` to
# these by enum, so adding a write tool cannot leave the model offered the old list. That
# failure would be silent -- an unknown name is refused and simply never becomes a button.
WRITE: tuple[str, ...] = tuple(spec.name for spec in _SPECS if spec.write)

_SPECS.append(
    ToolSpec(
        name="submit_diagnosis",
        description="End the loop with a diagnosis: what's wrong, the evidence for it, "
        "an optional proposed action, and a confidence score.",
        schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                # Stated in both the schema and its description: the enum is the real
                # constraint where the provider honours it, the prose survives one that
                # treats nested schemas loosely. Without it the model answered "raise the
                # memory limit", which is not a tool, so no button appeared.
                "proposed_action": {
                    "type": ["object", "null"],
                    "description": (
                        "The single remediation to propose for human approval, as "
                        '{"tool": <one of ' + ", ".join(WRITE) + '>, "args": {...}}, '
                        "where args match that tool's own schema. Use null when no tool "
                        "in that list would fix this -- null is a correct answer, not a "
                        "failure to find one."
                    ),
                    "properties": {
                        "tool": {"type": "string", "enum": list(WRITE)},
                        "args": {
                            "type": "object",
                            "description": "Arguments for `tool`, matching its schema.",
                        },
                    },
                    "required": ["tool", "args"],
                },
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["summary", "evidence", "confidence"],
        },
        fn=submit_diagnosis,
        write=False,
        needs=(),
    )
)

REGISTRY: dict[str, ToolSpec] = {spec.name: spec for spec in _SPECS}
READ_ONLY: tuple[str, ...] = tuple(
    name for name, spec in REGISTRY.items() if not spec.write
)
ALL: tuple[str, ...] = tuple(REGISTRY)


def as_model_tools(names: tuple[str, ...] = ALL) -> list[dict[str, Any]]:
    """`{name, description, schema}` dicts -- what provider.chat()'s `tools` parameter expects.
    `fn`, `write` and `needs` are left out because none is the model's business.
    """
    return [
        {
            "name": REGISTRY[name].name,
            "description": REGISTRY[name].description,
            "schema": REGISTRY[name].schema,
        }
        for name in names
    ]
