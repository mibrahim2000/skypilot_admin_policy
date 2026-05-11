"""SkyPilot admin policy.

Rules:
- Reject direct ``sky launch``.
- Require SAP labels and node-pool toleration on Kubernetes jobs.
- Require extra Kueue toleration and topology annotation for H200.
- Reject blocked workload types such as general_research_development.
- Restrict GPU types by Kubernetes cluster:
  - research-dev-hyperpod-use1: no GPU restriction; product-dev-hyperpod-use1: only L4 or A10G
  - research-dev-hyperpod-eus2: only H200 or L4; product-dev-hyperpod-eus2: only L4
  - research-dev-hyperpod-usw2, product-dev-hyperpod-usw2: only B200
"""

import logging
from typing import Any

import sky
from sky import exceptions
from sky.server.requests import request_names

# Pod labels: SAP code uppercase and Kueue queue name lowercase SAP code.
SAP_CODE_LABEL_KEY = "sapCode"
KUEUE_QUEUE_LABEL_KEY = "kueue.x-k8s.io/queue-name"

# Optional/common workload type label keys.
# Keep this flexible because different task templates may use different names.
WORKLOAD_TYPE_LABEL_KEYS = {
    "workloadType",
    "workloadtype",
    "workload-type",
    "workload_type",
}

# Node pool taint users must tolerate.
NODE_POOL_KEY = "node-pool"

# GPU node-pool toleration values.
NODE_POOL_H200_VALUE = "gpu-nvidia-h200"
NODE_POOL_B200_VALUE = "gpu-nvidia-b200"
NODE_POOL_L4_VALUE = "gpu-nvidia-l4"

# H200 node pool also requires tolerating workload-type=kueue.
WORKLOAD_TYPE_KEY = "workload-type"
WORKLOAD_TYPE_KUEUE_VALUE = "kueue"

# H200: Kueue podset topology annotation.
KUEUE_PODSET_TOPOLOGY_ANNOTATION_KEY = "kueue.x-k8s.io/podset-required-topology"
KUEUE_PODSET_TOPOLOGY_ANNOTATION_VALUE = "kubernetes.io/hostname"

# Blocked workload types.
# Normalization converts "_" to "-" and lowercases, so this also blocks:
# general_research_development, GENERAL_RESEARCH_DEVELOPMENT, etc.
BLOCKED_WORKLOAD_TYPES: set[str] = {
    "general-research-development",
}

# Cluster GPU restrictions by cluster suffix.
#
# The real SkyPilot Kubernetes region usually looks like:
# multiversecomputing.teleport.sh-research-dev-hyperpod-eus2
#
# We match by the meaningful cluster suffix to avoid depending on the full
# Teleport/context prefix.
#
# None means the cluster is recognized but has no GPU restrictions.
CLUSTER_GPU_RESTRICTIONS: dict[str, set[str] | None] = {
    "research-dev-hyperpod-use1": None,
    "research-dev-hyperpod-eus2": {"h200", "l4"},
    "research-dev-hyperpod-usw2": {"b200"},
    "product-dev-hyperpod-use1": {"l4", "a10g"},
    "product-dev-hyperpod-eus2": {"l4"},
    "product-dev-hyperpod-usw2": {"b200"},
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
  (Only when H200 is the targeted pool. Not required for cpu-only, A10G, L4, B200, or any other node-pool value.
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
        #   kueue.x-k8s.io/podset-required-topology: kubernetes.io/hostname
      spec:
        tolerations:
          # Set node-pool to the taint value for the pool you use. Examples:
          # - CPU:  value: cpu-only
          # - H200: value: gpu-nvidia-h200
          # - B200: value: gpu-nvidia-b200
          # - L4:   value: gpu-nvidia-l4
          - key: node-pool
            operator: Equal
            value: gpu-nvidia-l4
            effect: NoSchedule

          # Required only when node-pool value is gpu-nvidia-h200:
          # - key: workload-type
          #   operator: Equal
          #   value: kueue
          #   effect: NoSchedule

Required entries:
- labels: sapCode non-empty and all uppercase; kueue.x-k8s.io/queue-name non-empty and all lowercase;
  both must be the same SAP code, case-insensitive
- toleration: key=node-pool, operator=Equal, effect=NoSchedule, value non-empty
- H200 only: require workload-type=kueue and the podset topology annotation above only when the targeted node-pool is gpu-nvidia-h200"""

_BLOCKED_WORKLOAD_TYPE_REJECTION = """The workload type "GENERAL_RESEARCH_DEVELOPMENT" is not allowed.

Please use a valid project-specific workload type / SAP code instead."""

_CLUSTER_GPU_REJECTION = """GPU type not allowed on this cluster.

Cluster: {cluster}
Allowed GPU types: {allowed}
Requested: {requested}

Cluster GPU policy:
- research-dev-hyperpod-use1: no GPU restriction
- product-dev-hyperpod-use1: only L4 or A10G
- research-dev-hyperpod-eus2: only H200 or L4
- product-dev-hyperpod-eus2: only L4
- research-dev-hyperpod-usw2, product-dev-hyperpod-usw2: only B200

Please select a GPU type that is available on your target cluster."""


def _safe_get_nested(config: Any, keys: tuple[str, ...], default: Any = None) -> Any:
    """Safely read SkyPilot config nested values.

    SkyPilot config normally has get_nested(), but this helper also supports plain dicts.
    """
    if config is None:
        return default

    get_nested = getattr(config, "get_nested", None)
    if callable(get_nested):
        return get_nested(keys, default)

    cur = config
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


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
    """Collect tolerations from merged SkyPilot config and resource overrides."""
    found: list[dict] = []

    pod_global = _safe_get_nested(
        user_request.skypilot_config,
        ("kubernetes", "pod_config"),
        None,
    )
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
    """Collect labels from merged SkyPilot config and resource overrides."""
    found: list[dict] = []

    pod_global = _safe_get_nested(
        user_request.skypilot_config,
        ("kubernetes", "pod_config"),
        None,
    )
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
    """Collect annotations from merged SkyPilot config and resource overrides."""
    found: list[dict] = []

    pod_global = _safe_get_nested(
        user_request.skypilot_config,
        ("kubernetes", "pod_config"),
        None,
    )
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


def _operator_is_equal(op: Any) -> bool:
    # Kubernetes tolerations default operator to Equal when omitted.
    if op is None:
        return True
    return str(op).strip().lower() == "equal"


def _is_kubernetes_resources(user_request: sky.UserRequest) -> bool:
    """True if any resource in the task is a Kubernetes resource.

    Robust against SkyPilot versions where get_resource_config() does not expose
    cloud/infra the same way as the printed UserRequest.
    """
    for res in list(user_request.task.resources):
        cloud = getattr(res, "cloud", None)
        if cloud is not None:
            cloud_str = str(cloud).strip().lower()
            if "kubernetes" in cloud_str or cloud_str.startswith("k8s"):
                return True

        res_str = str(res).strip().lower()
        if "kubernetes" in res_str or res_str.startswith("k8s"):
            return True

    try:
        resources = user_request.task.get_resource_config()
    except Exception:
        resources = {}

    if not isinstance(resources, dict):
        return False

    candidates = [resources]
    for v in resources.values():
        if isinstance(v, dict):
            candidates.append(v)

    for d in candidates:
        for key in ("cloud", "infra"):
            val = d.get(key)
            if val is None:
                continue

            s_val = str(val).strip().lower()
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
    """True if any node-pool toleration targets a pool other than H200."""
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
    """When H200 is the effective node pool, require Kueue topology annotation."""
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


def _normalize_cluster_name(value: Any) -> str | None:
    if value is None:
        return None

    s = str(value).strip()
    if not s:
        return None

    for prefix in ("kubernetes:", "kubernetes/", "k8s/"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()

    return s or None


def _extract_kubernetes_contexts(user_request: sky.UserRequest) -> list[str]:
    """Extract Kubernetes context/region names from task resources.

    In the UserRequest you pasted, the cluster appears as:

        Kubernetes(..., region=multiversecomputing.teleport.sh-...-eus2)

    Therefore resource.region is the main source of truth.
    """
    contexts: list[str] = []

    for res in list(user_request.task.resources):
        for attr in ("region", "zone"):
            ctx = _normalize_cluster_name(getattr(res, attr, None))
            if ctx:
                contexts.append(ctx)

        try:
            res_config = res.to_yaml_config()
        except Exception:
            res_config = None

        if isinstance(res_config, dict):
            for key in ("region", "zone", "infra", "cloud"):
                ctx = _normalize_cluster_name(res_config.get(key))
                if ctx:
                    contexts.append(ctx)

    # Fallback to task resource config.
    try:
        resources = user_request.task.get_resource_config()
    except Exception:
        resources = {}

    if isinstance(resources, dict):
        candidates = [resources]
        for v in resources.values():
            if isinstance(v, dict):
                candidates.append(v)

        for d in candidates:
            for key in ("region", "zone", "infra", "cloud"):
                ctx = _normalize_cluster_name(d.get(key))
                if ctx:
                    contexts.append(ctx)

    return list(dict.fromkeys(contexts))


def _normalize_gpu_name(value: Any) -> str | None:
    if value is None:
        return None

    s = str(value).strip()
    if not s:
        return None

    # Handles "L4:1", "l4:1", "NVIDIA-L4:1", etc.
    s = s.split(":")[0].strip().lower()

    # Keep the canonical names used in the policy.
    # SkyPilot usually gives names like "L4", "H200", "B200".
    aliases = {
        "l4": "l4",
        "h200": "h200",
        "b200": "b200",
        "nvidia-l4": "l4",
        "nvidia-h200": "h200",
        "nvidia-b200": "b200",
        "gpu-nvidia-l4": "l4",
        "gpu-nvidia-h200": "h200",
        "gpu-nvidia-b200": "b200",
    }

    return aliases.get(s, s)


def _extract_requested_gpus(user_request: sky.UserRequest) -> list[str]:
    """Extract requested GPU accelerator names from task resources.

    This reads from resource.accelerators, which matches the real UserRequest:

        Kubernetes(..., {'L4': 1}, region=...)
    """
    gpus: list[str] = []

    for res in list(user_request.task.resources):
        accelerators = getattr(res, "accelerators", None)

        if isinstance(accelerators, dict):
            for name in accelerators.keys():
                gpu = _normalize_gpu_name(name)
                if gpu:
                    gpus.append(gpu)

        elif isinstance(accelerators, str):
            gpu = _normalize_gpu_name(accelerators)
            if gpu:
                gpus.append(gpu)

        try:
            res_config = res.to_yaml_config()
        except Exception:
            res_config = None

        if isinstance(res_config, dict):
            acc = res_config.get("accelerators")

            if isinstance(acc, dict):
                for name in acc.keys():
                    gpu = _normalize_gpu_name(name)
                    if gpu:
                        gpus.append(gpu)

            elif isinstance(acc, str):
                gpu = _normalize_gpu_name(acc)
                if gpu:
                    gpus.append(gpu)

    # Fallback to task resource config.
    try:
        resources = user_request.task.get_resource_config()
    except Exception:
        resources = {}

    if isinstance(resources, dict):
        candidates = [resources]
        for v in resources.values():
            if isinstance(v, dict):
                candidates.append(v)

        for d in candidates:
            acc = d.get("accelerators")

            if isinstance(acc, dict):
                for name in acc.keys():
                    gpu = _normalize_gpu_name(name)
                    if gpu:
                        gpus.append(gpu)

            elif isinstance(acc, str):
                gpu = _normalize_gpu_name(acc)
                if gpu:
                    gpus.append(gpu)

    return list(dict.fromkeys(gpus))


def _matched_cluster_policy_key(context: str) -> str | None:
    """Return the policy cluster key matched by a Kubernetes context/region."""
    ctx = context.lower().strip()

    for cluster_key in CLUSTER_GPU_RESTRICTIONS:
        key = cluster_key.lower()
        if ctx == key or ctx.endswith(key):
            return cluster_key

    return None


def _validate_cluster_gpu_restrictions(
    contexts: list[str],
    requested_gpus: list[str],
) -> str | None:
    """Return a rejection message if requested GPUs violate cluster restrictions."""
    if not contexts:
        return None

    if not requested_gpus:
        return None

    for context in contexts:
        cluster_key = _matched_cluster_policy_key(context)
        if cluster_key is None:
            continue

        allowed_gpus = CLUSTER_GPU_RESTRICTIONS[cluster_key]

        # Recognized cluster with no GPU restriction, e.g. use1.
        if allowed_gpus is None:
            continue

        disallowed = [gpu for gpu in requested_gpus if gpu not in allowed_gpus]
        if not disallowed:
            continue

        return _CLUSTER_GPU_REJECTION.format(
            cluster=context,
            allowed=", ".join(sorted(g.upper() for g in allowed_gpus)),
            requested=", ".join(sorted(g.upper() for g in set(disallowed))),
        )

    return None


def _normalize_workload_type(value: Any) -> str:
    if value is None:
        return ""

    return str(value).strip().lower().replace("_", "-")


def _is_blocked_workload_type(labels_list: list[dict]) -> bool:
    """True if labels contain a blocked workload type.

    Supports:
    - sapCode
    - workloadType
    - workloadtype
    - workload-type
    - workload_type

    This keeps compatibility with your previous SAP-code-based policy while also
    supporting explicit workload type labels.
    """
    merged = _merged_labels(labels_list)

    candidate_values: list[Any] = []

    sap = merged.get(SAP_CODE_LABEL_KEY)
    if sap is not None:
        candidate_values.append(sap)

    queue = merged.get(KUEUE_QUEUE_LABEL_KEY)
    if queue is not None:
        candidate_values.append(queue)

    for key in WORKLOAD_TYPE_LABEL_KEYS:
        val = merged.get(key)
        if val is not None:
            candidate_values.append(val)

    for value in candidate_values:
        if _normalize_workload_type(value) in BLOCKED_WORKLOAD_TYPES:
            return True

    return False


class WorkloadTypeTolerationPolicy(sky.AdminPolicy):
    """Reject invalid Kubernetes managed jobs according to internal cluster policy."""

    @classmethod
    def validate_and_mutate(cls, user_request: sky.UserRequest) -> sky.MutatedUserRequest:
        logger = logging.getLogger(__name__)

        if user_request.request_name == request_names.AdminPolicyRequestName.CLUSTER_LAUNCH:
            raise exceptions.UserRequestRejectedByPolicy(_DIRECT_LAUNCH_REJECTION)

        if not _is_kubernetes_resources(user_request):
            return sky.MutatedUserRequest(
                user_request.task,
                user_request.skypilot_config,
            )

        tolerations = _collect_tolerations(user_request)
        labels = _collect_labels(user_request)
        merged_annotations = _shallow_merge_dicts(_collect_annotations(user_request))

        contexts = _extract_kubernetes_contexts(user_request)
        requested_gpus = _extract_requested_gpus(user_request)

        logger.info(
            "Admin policy request parsed: contexts=%s requested_gpus=%s tolerations=%s labels=%s annotations=%s",
            contexts,
            requested_gpus,
            tolerations,
            labels,
            merged_annotations,
        )

        # Rule: blocked workload types.
        if _is_blocked_workload_type(labels):
            raise exceptions.UserRequestRejectedByPolicy(_BLOCKED_WORKLOAD_TYPE_REJECTION)

        # Rule: cluster-specific GPU restrictions.
        gpu_rejection = _validate_cluster_gpu_restrictions(contexts, requested_gpus)
        if gpu_rejection is not None:
            raise exceptions.UserRequestRejectedByPolicy(gpu_rejection)

        # Rule: Kubernetes pod config requirements.
        if (
            _tolerations_pass(tolerations)
            and _has_sap_labels(labels)
            and _h200_annotation_pass(tolerations, merged_annotations)
        ):
            return sky.MutatedUserRequest(
                user_request.task,
                user_request.skypilot_config,
            )

        raise exceptions.UserRequestRejectedByPolicy(_REJECTION)