"""SkyPilot admin policy: require a workload-type pod toleration on Kubernetes tasks."""

import sky
from sky import exceptions

# Taint key users must tolerate with their SAP code (see cluster / node-pool setup).
WORKLOAD_TYPE_KEY = "workload-type"

_REJECTION = """Skypilot jobs must declare a workload-type toleration with your SAP code.

Add under SkyPilot config (e.g. task `config:` or ~/.sky/config.yaml), for example:

config:
  kubernetes:
    pod_config:
      spec:
        tolerations:
          - key: workload-type
            operator: Equal
            value: <YOUR-SAP-CODE>
            effect: NoSchedule

Required entry: key=workload-type, operator=Equal, effect=NoSchedule, value non-empty."""


def _tolerations_from_pod_config(pod_config: dict | None) -> list:
    if not isinstance(pod_config, dict):
        return []
    spec = pod_config.get("spec") or {}
    if not isinstance(spec, dict):
        return []
    raw = spec.get("tolerations")
    if raw is None:
        return []
    return raw if isinstance(raw, list) else []


def _collect_tolerations(user_request: sky.UserRequest) -> list[dict]:
    """Tolerations from merged SkyPilot config and per-resource kubernetes.pod_config overrides."""
    found: list[dict] = []

    pod_global = user_request.skypilot_config.get_nested(("kubernetes", "pod_config"), None)
    if isinstance(pod_global, dict):
        for t in _tolerations_from_pod_config(pod_global):
            if isinstance(t, dict):
                found.append(t)

    for res in list(user_request.task.resources):
        overrides = getattr(res, "cluster_config_overrides", None) or {}
        if not isinstance(overrides, dict):
            continue
        k8s = overrides.get("kubernetes") or {}
        if not isinstance(k8s, dict):
            continue
        pod = k8s.get("pod_config")
        if not isinstance(pod, dict):
            continue
        for t in _tolerations_from_pod_config(pod):
            if isinstance(t, dict):
                found.append(t)

    return found


def _operator_is_equal(op) -> bool:
    if op is None:
        return True
    return str(op).strip().lower() == "equal"


def _is_kubernetes_resources(resources: dict) -> bool:
    """SkyPilot uses `infra` in serialized resource config; older paths may use `cloud`."""
    for key in ("cloud", "infra"):
        val = resources.get(key)
        if val is not None and str(val).startswith("kubernetes"):
            return True
    return False


def _has_workload_type_toleration(tolerations: list[dict]) -> bool:
    for t in tolerations:
        if t.get("key") != WORKLOAD_TYPE_KEY:
            continue
        if not _operator_is_equal(t.get("operator")):
            continue
        if t.get("effect") != "NoSchedule":
            continue
        val = t.get("value")
        if val is not None and str(val).strip():
            return True
    return False


class WorkloadTypeTolerationPolicy(sky.AdminPolicy):
    """Rejects Kubernetes tasks that do not specify a workload-type toleration (SAP code)."""

    @classmethod
    def validate_and_mutate(cls, user_request: sky.UserRequest) -> sky.MutatedUserRequest:
        resources = user_request.task.get_resource_config()
        if not _is_kubernetes_resources(resources):
            return sky.MutatedUserRequest(user_request.task, user_request.skypilot_config)

        tolerations = _collect_tolerations(user_request)
        if _has_workload_type_toleration(tolerations):
            return sky.MutatedUserRequest(user_request.task, user_request.skypilot_config)

        raise exceptions.UserRequestRejectedByPolicy(_REJECTION)
