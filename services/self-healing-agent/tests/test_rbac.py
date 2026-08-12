from pathlib import Path

import yaml

from tools import REGISTRY

RBAC_PATH = Path(__file__).resolve().parents[1] / "k8s" / "rbac.yaml"


def _needs_from_role(docs: list[dict]) -> set[str]:
    role = next(doc for doc in docs if doc and doc.get("kind") == "Role")
    needs = set()
    for rule in role["rules"]:
        for group in rule["apiGroups"]:
            group_name = "core" if group == "" else group
            for resource in rule["resources"]:
                for verb in rule["verbs"]:
                    needs.add(f"{group_name}/{resource}:{verb}")
    return needs


def test_role_grants_exactly_what_the_registry_needs():
    """k8s/rbac.yaml and tools/__init__.py's ToolSpec.needs are written from the same
    tool list but as two files, a week apart on the calendar per the spec's day-by-day
    plan. This is the check that catches the drift before a 403 does, live, on Day 21.
    """
    docs = list(yaml.safe_load_all(RBAC_PATH.read_text()))
    granted = _needs_from_role(docs)
    required = {need for spec in REGISTRY.values() for need in spec.needs}

    assert granted == required
