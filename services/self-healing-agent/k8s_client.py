"""Building the two API clients this service needs, in one place.

Every tool in tools/ takes `(core, apps)` as its first argument rather than importing
this module itself -- the same shape store.py plays for the copilot. That is what makes
every tool testable with a fake client and no cluster, and it is why this module is a
leaf: nothing else in the service constructs an ApiClient.
"""

import os
from functools import lru_cache

from kubernetes import client, config

# The path the in-cluster ServiceAccount token is mounted at. Its presence is what
# tells this process whether it is running inside the cluster (Day 20's Deployment)
# or on a laptop against a kubeconfig (every day before that) -- there is no env var
# for this that isn't just re-deriving the same fact less reliably.
_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


@lru_cache(maxsize=1)
def get_apis() -> tuple[client.CoreV1Api, client.AppsV1Api]:
    """The process-wide API clients. Cached: loading config is not free, and every
    tool call would otherwise re-parse a kubeconfig or re-read the SA token file."""
    if os.path.exists(_SA_TOKEN_PATH):
        config.load_incluster_config()
    else:
        config.load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api()
