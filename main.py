"""SkyPilot admin policy: restrict direct ``sky launch``, require SAP labels and node-pool toleration on Kubernetes."""

import json
import logging

import sky
from sky import exceptions
from sky.server.requests import request_names

# Pod labels: SAP code (uppercase) and Kueue queue name (lowercase SAP code).
SAP_CODE_LABEL_KEY = "sapCode"
KUEUE_QUEUE_LABEL_KEY = "kueue.x-k8s.io/queue-name"

# Node pool taint users must tolerate (see cluster docs for CPU vs GPU pool values).
NODE_POOL_KEY = "node-pool"
NODE_POOL_H200_VALUE = "gpu-nvidia-h200"

# H200 node pool also requires tolerating workload-type=kueue (see cluster taints).
WORKLOAD_TYPE_KEY = "workload-type"
WORKLOAD_TYPE_KUEUE_VALUE = "kueue"

# H200: Kueue podset topology annotation (required with H200 node-pool when no other node-pool applies).
KUEUE_PODSET_TOPOLOGY_ANNOTATION_KEY = "kueue.x-k8s.io/podset-required-topology"
KUEUE_PODSET_TOPOLOGY_ANNOTATION_VALUE = "kubernetes.io/hostname"

# Blocked SAP codes (case-insensitive). These codes cannot be used for any workloads.
BLOCKED_SAP_CODES: set[str] = {"general-research-development"}

# GPU node-pool toleration values.
NODE_POOL_B200_VALUE = "gpu-nvidia-b200"
NODE_POOL_L4_VALUE = "gpu-nvidia-l4"

# Per-cluster GPU restrictions. Key: full Kubernetes context name.
# Value: set of allowed GPU node-pool toleration values.
# CPU-only node-pool values are always allowed regardless of cluster.
CLUSTER_GPU_RESTRICTIONS: dict[str, set[str]] = {
    "k8s/multiversecomputing.teleport.sh-research-dev-hyperpod-usw2": {NODE_POOL_B200_VALUE},
    "k8s/multiversecomputing.teleport.sh-research-dev-hyperpod-eus2": {NODE_POOL_H200_VALUE, NODE_POOL_L4_VALUE},
}

_DIRECT_LAUNCH_REJECTION = """Direct ``sky launch`` is disabled on this SkyPilot API server.

Use managed jobs instead:

  sky jobs launch <your_task.yaml>

``sky exec`` on existing clusters and workloads started via ``sky jobs launch`` (including controller-provisioned clusters) are unchanged."""

_REJECTION = """Skypilot Kubernetes jobs must declare:
- metadata.labels.sapCode (your SAP code, uppercase)
- metadata.labels.kueue.x-k8s.io/queue-name (your SAP code, lowercase)
- a node-pool toleration (Equal, NoSchedule, non-empty value) that matches the pool you need:
  - CPU-only workloads: use your cluster's CPU node-pool value (see internal cluster / node-pool docs).
  - GPU workloads: pick the GPU pool (examples below). CPU pools do not use these gpu-nvidia-* values.
- if node-pool value is gpu-nvidia-h200 (H200), an additional workload-type toleration:
  key=workload-type, operator=Equal, value=kueue, effect=NoSchedule
  and metadata.annotations kueue.x-k8s.io/podset-required-topology: kubernetes.io/hostname
  (Only when H200 is the targeted pool. Not required for cpu-only, A10G, L4, or any other node-pool value.
  If merged global config lists both H200 and another node-pool, the non-H200 entry exempts this rule.)

Add under SkyPilot config (e.g. task `config:`), for example:

config:
  kubernetes:
    pod_config:
      metadata:
        labels:
          sapCode: <YOUR-SAP-CODE-IN-UPPERCASE>
          kueue.x-k8s.io/queue-name: <your-sap-code-in-lowercase>
        # H200 only: required annotation for Kueue podset topology
        # annotations:
        #  kueue.x-k8s.io/podset-required-topology: kubernetes.io/hostname
      spec:
        tolerations:
          # Set node-pool to the taint value for the pool you use. Examples:
          # - CPU:  value: cpu-only
          # - H200: value: gpu-nvidia-h200
          # - A10G: value: gpu-nvidia-a10g
          # - L4:   value: gpu-nvidia-l4
          - key: node-pool
            operator: Equal
            value: gpu-nvidia-a10g
            effect: NoSchedule

          # Required only when node-pool value is gpu-nvidia-h200 (H200 GPU pool):
          # - key: workload-type
          #   operator: Equal
          #   value: kueue
          #   effect: NoSchedule


Required entries:
- labels: sapCode non-empty and all uppercase; kueue.x-k8s.io/queue-name non-empty and all lowercase;
  both must be the same SAP code (case-insensitive match)
- toleration: key=node-pool, operator=Equal, effect=NoSchedule, value non-empty (CPU or GPU pool value from your cluster)
- H200 only: require workload-type=kueue and the podset topology annotation above only when the targeted node-pool is gpu-nvidia-h200 (not for other GPU types or CPU)"""

_BLOCKED_SAP_CODE_REJECTION = """The SAP code "GENERAL-RESEARCH-DEVELOPMENT" is not allowed for workloads.

Please use a project-specific SAP code instead."""

_CLUSTER_GPU_REJECTION = """GPU type not allowed on this cluster.

Cluster: {cluster}
Allowed GPU types: {allowed}
Requested: {requested}

Please select a GPU type that is available on your target cluster."""


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


def _labels_from_pod_config(pod_config: dict | None) -> dict:
    if not isinstance(pod_config, dict):
        return {}
    metadata = pod_config.get("metadata") or {}
    if not isinstance(metadata, dict):
        return {}
    labels = metadata.get("labels") or {}
    return labels if isinstance(labels, dict) else {}


def _collect_labels(user_request: sky.UserRequest) -> list[dict]:
    """Labels from merged SkyPilot config and per-resource kubernetes.pod_config overrides."""
    found: list[dict] = []

    pod_global = user_request.skypilot_config.get_nested(("kubernetes", "pod_config"), None)
    if isinstance(pod_global, dict):
        labels = _labels_from_pod_config(pod_global)
        if labels:
            found.append(labels)

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
        labels = _labels_from_pod_config(pod)
        if labels:
            found.append(labels)

    return found


def _annotations_from_pod_config(pod_config: dict | None) -> dict:
    if not isinstance(pod_config, dict):
        return {}
    metadata = pod_config.get("metadata") or {}
    if not isinstance(metadata, dict):
        return {}
    annotations = metadata.get("annotations") or {}
    return annotations if isinstance(annotations, dict) else {}


def _collect_annotations(user_request: sky.UserRequest) -> list[dict]:
    """Annotations from merged SkyPilot config and per-resource kubernetes.pod_config overrides."""
    found: list[dict] = []

    pod_global = user_request.skypilot_config.get_nested(("kubernetes", "pod_config"), None)
    if isinstance(pod_global, dict):
        ann = _annotations_from_pod_config(pod_global)
        if ann:
            found.append(ann)

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
        ann = _annotations_from_pod_config(pod)
        if ann:
            found.append(ann)

    return found


def _shallow_merge_dicts(dict_layers: list[dict]) -> dict:
    merged: dict = {}
    for d in dict_layers:
        if isinstance(d, dict):
            merged.update(d)
    return merged


def _merged_labels(labels_list: list[dict]) -> dict:
    return _shallow_merge_dicts(labels_list)


def _operator_is_equal(op) -> bool:
    if op is None:
        return True
    return str(op).strip().lower() == "equal"


def _is_kubernetes_resources(resources: dict) -> bool:
    """SkyPilot uses `infra` in serialized resource config; older paths may use `cloud`."""
    candidates = [resources, resources.get("resources") or {}]
    for d in candidates:
        if not isinstance(d, dict):
            continue
        for key in ("cloud", "infra"):
            val = d.get(key)
            if val is not None:
                s_val = str(val).strip()
                if s_val.startswith("kubernetes") or s_val.startswith("k8s/"):
                    return True
    return False


def _has_node_pool_toleration(tolerations: list[dict]) -> bool:
    for t in tolerations:
        if t.get("key") != NODE_POOL_KEY:
            continue
        if not _operator_is_equal(t.get("operator")):
            continue
        if t.get("effect") != "NoSchedule":
            continue
        val = t.get("value")
        if val is not None and str(val).strip():
            return True
    return False


def _has_non_h200_node_pool_toleration(tolerations: list[dict]) -> bool:
    """True if any node-pool toleration targets a pool other than H200 (CPU, A10G, L4, ...).

    Used so merged config cannot force the H200 workload-type rule when the task also specifies
    a different node-pool value.
    """
    for t in tolerations:
        if t.get("key") != NODE_POOL_KEY:
            continue
        if not _operator_is_equal(t.get("operator")):
            continue
        if t.get("effect") != "NoSchedule":
            continue
        val = t.get("value")
        if val is None or not str(val).strip():
            continue
        if str(val).strip() != NODE_POOL_H200_VALUE:
            return True
    return False


def _selects_h200_node_pool(tolerations: list[dict]) -> bool:
    for t in tolerations:
        if t.get("key") != NODE_POOL_KEY:
            continue
        if not _operator_is_equal(t.get("operator")):
            continue
        if t.get("effect") != "NoSchedule":
            continue
        val = t.get("value")
        if val is not None and str(val).strip() == NODE_POOL_H200_VALUE:
            return True
    return False


def _has_workload_type_kueue_toleration(tolerations: list[dict]) -> bool:
    for t in tolerations:
        if t.get("key") != WORKLOAD_TYPE_KEY:
            continue
        if not _operator_is_equal(t.get("operator")):
            continue
        if t.get("effect") != "NoSchedule":
            continue
        val = t.get("value")
        if val is not None and str(val).strip() == WORKLOAD_TYPE_KUEUE_VALUE:
            return True
    return False


def _tolerations_pass(tolerations: list[dict]) -> bool:
    if not _has_node_pool_toleration(tolerations):
        return False
    if _has_non_h200_node_pool_toleration(tolerations):
        return True
    if _selects_h200_node_pool(tolerations) and not _has_workload_type_kueue_toleration(tolerations):
        return False
    return True


def _has_podset_topology_annotation(merged_annotations: dict) -> bool:
    val = merged_annotations.get(KUEUE_PODSET_TOPOLOGY_ANNOTATION_KEY)
    if val is None:
        return False
    return str(val).strip() == KUEUE_PODSET_TOPOLOGY_ANNOTATION_VALUE


def _h200_annotation_pass(tolerations: list[dict], merged_annotations: dict) -> bool:
    """When H200 is the effective node pool, require kueue podset-required-topology annotation."""
    if _has_non_h200_node_pool_toleration(tolerations):
        return True
    if not _selects_h200_node_pool(tolerations):
        return True
    return _has_podset_topology_annotation(merged_annotations)


def _is_all_uppercase(value: str) -> bool:
    s = str(value).strip()
    return bool(s) and s == s.upper()


def _is_all_lowercase(value: str) -> bool:
    s = str(value).strip()
    return bool(s) and s == s.lower()


def _has_sap_labels(labels_list: list[dict]) -> bool:
    merged = _merged_labels(labels_list)
    sap = merged.get(SAP_CODE_LABEL_KEY)
    queue = merged.get(KUEUE_QUEUE_LABEL_KEY)
    if sap is None or queue is None:
        return False
    if not _is_all_uppercase(sap):
        return False
    if not _is_all_lowercase(queue):
        return False
    if str(sap).strip().lower() != str(queue).strip().lower():
        return False
    return True


def _extract_kubernetes_context(resources: dict) -> str | None:
    """Extract the Kubernetes context name from the resource config, if any.

    ``get_resource_config()`` may nest the infra/cloud key under a ``resources``
    sub-dict, so we check both the top-level dict and that nested dict.
    """
    candidates = [resources, resources.get("resources") or {}]
    for d in candidates:
        if not isinstance(d, dict):
            continue
        for key in ("infra", "cloud"):
            val = d.get(key)
            if val is None:
                continue
            s_val = str(val).strip()
            # Handle "kubernetes:<context>" format
            if s_val.startswith("kubernetes:"):
                parts = s_val.split(":", 1)
                if len(parts) == 2 and parts[1].strip():
                    return parts[1].strip()
            # Handle "k8s/" prefix or direct context names
            if s_val.startswith("k8s/") or s_val in CLUSTER_GPU_RESTRICTIONS:
                return s_val
    return None


def _get_gpu_node_pool_values(tolerations: list[dict]) -> list[str]:
    """Return all GPU node-pool toleration values (values starting with 'gpu-')."""
    values: list[str] = []
    for t in tolerations:
        if t.get("key") != NODE_POOL_KEY:
            continue
        if not _operator_is_equal(t.get("operator")):
            continue
        if t.get("effect") != "NoSchedule":
            continue
        val = t.get("value")
        if val is not None and str(val).strip().startswith("gpu-"):
            values.append(str(val).strip())
    return values


def _is_blocked_sap_code(labels_list: list[dict]) -> bool:
    """True if the SAP code label matches any blocked SAP code."""
    merged = _merged_labels(labels_list)
    sap = merged.get(SAP_CODE_LABEL_KEY)
    if sap is None:
        return False
    return str(sap).strip().lower() in BLOCKED_SAP_CODES


def _validate_cluster_gpu_restrictions(context: str | None, tolerations: list[dict]) -> str | None:
    """Return a rejection message if GPU selection violates cluster restrictions, else None."""
    if context is None:
        return None
    ctx_lower = context.lower()

    matching_cluster: str | None = None
    allowed_gpus: set[str] | None = None
    for cluster_ctx, gpus in CLUSTER_GPU_RESTRICTIONS.items():
        if ctx_lower == cluster_ctx.lower():
            matching_cluster = cluster_ctx
            allowed_gpus = gpus
            break

    if matching_cluster is None or allowed_gpus is None:
        return None

    gpu_values = _get_gpu_node_pool_values(tolerations)
    if not gpu_values:
        return None  # No GPU tolerations -> nothing to restrict

    disallowed = [v for v in gpu_values if v not in allowed_gpus]
    if not disallowed:
        return None

    allowed_display = ", ".join(sorted(allowed_gpus))
    disallowed_display = ", ".join(sorted(set(disallowed)))
    return _CLUSTER_GPU_REJECTION.format(
        cluster=matching_cluster,
        allowed=allowed_display,
        requested=disallowed_display,
    )


class WorkloadTypeTolerationPolicy(sky.AdminPolicy):
    """Rejects direct ``sky launch``; rejects Kubernetes tasks missing SAP labels, node-pool toleration, H200 kueue toleration, or H200 topology annotation."""

    @classmethod
    def validate_and_mutate(cls, user_request: sky.UserRequest) -> sky.MutatedUserRequest:
        logger = logging.getLogger(__name__)

        if user_request.request_name == request_names.AdminPolicyRequestName.CLUSTER_LAUNCH:
            raise exceptions.UserRequestRejectedByPolicy(_DIRECT_LAUNCH_REJECTION)

        resources = user_request.task.get_resource_config()
        if not _is_kubernetes_resources(resources):
            return sky.MutatedUserRequest(user_request.task, user_request.skypilot_config)

        tolerations = _collect_tolerations(user_request)
        labels = _collect_labels(user_request)
        merged_annotations = _shallow_merge_dicts(_collect_annotations(user_request))
        context = _extract_kubernetes_context(resources)

        # --- Debug dump ---
        logger.info(
            "[AdminPolicy] Incoming request dump:\n"
            "  request_name: %s\n"
            "  resource_config: %s\n"
            "  extracted_context: %s\n"
            "  tolerations: %s\n"
            "  labels: %s\n"
            "  merged_annotations: %s\n"
            "  gpu_node_pool_values: %s",
            user_request.request_name,
            json.dumps(resources, indent=2, default=str),
            context,
            json.dumps(tolerations, indent=2, default=str),
            json.dumps(labels, indent=2, default=str),
            json.dumps(merged_annotations, indent=2, default=str),
            _get_gpu_node_pool_values(tolerations),
        )

        # Rule: blocked SAP codes
        if _is_blocked_sap_code(labels):
            raise exceptions.UserRequestRejectedByPolicy(_BLOCKED_SAP_CODE_REJECTION)

        # Rule: cluster-specific GPU restrictions
        gpu_rejection = _validate_cluster_gpu_restrictions(context, tolerations)
        if gpu_rejection is not None:
            raise exceptions.UserRequestRejectedByPolicy(gpu_rejection)

        if (
            _tolerations_pass(tolerations)
            and _has_sap_labels(labels)
            and _h200_annotation_pass(tolerations, merged_annotations)
        ):
            return sky.MutatedUserRequest(user_request.task, user_request.skypilot_config)

        raise exceptions.UserRequestRejectedByPolicy(_REJECTION)
