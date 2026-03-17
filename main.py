import copy

import sky
from sky.utils import config_utils

# SkyPilot sets skypilot-workspace and skypilot-cluster-name on the pod. This policy adds
# labels sapCode (= skypilot-workspace value) and jobName (= skypilot-cluster-name value)
# plus tolerations/nodeSelector.

# Map accelerator type from task to your cluster's node-pool value (node-pool label).
GPU_TYPE_TO_NODE_POOL = {
    "h200": "gpu-nvidia-h200",
    "a10g": "gpu-nvidia-a10g",
    "l4": "gpu-nvidia-l4",
    "a100": "gpu-nvidia-a100",
    "v100": "gpu-nvidia-v100",
}

# workload-type when workspace is default (no specific SAP Code).
DEFAULT_WORKLOAD_TYPE = "general_research_development"


def _get_workspace_id(user_request: sky.UserRequest) -> str:
    """Return workspace for sapCode label — same value as skypilot-workspace.
    Uses active_workspace from skypilot config (client’s workspace); falls back
    to user name/id on server, then 'default'."""
    # SkyPilot sets skypilot-workspace from active_workspace in config
    workspace = user_request.skypilot_config.get_nested(("active_workspace",), None)
    if workspace is not None and str(workspace).strip():
        return str(workspace).strip()
    if user_request.user is not None:
        return str(user_request.user.name or user_request.user.id or "default")
    return "default"


def _workload_type(workspace_id: str) -> str:
    """workload-type label: SAP Code (workspace) or general_research_development."""
    if not workspace_id or workspace_id.strip().lower() == "default":
        return DEFAULT_WORKLOAD_TYPE
    return workspace_id.strip()


def _node_pool_value(gpu_type: str) -> str | None:
    """Return node-pool value (e.g. gpu-nvidia-h200) for the given GPU type, or None."""
    key = gpu_type.lower() if isinstance(gpu_type, str) else None
    if not key or key not in GPU_TYPE_TO_NODE_POOL:
        return None
    return GPU_TYPE_TO_NODE_POOL[key]


def _node_pool_toleration(gpu_type: str) -> dict | None:
    """Return node-pool toleration for the given GPU type, or None if not mapped."""
    value = _node_pool_value(gpu_type)
    if value is None:
        return None
    return {
        "key": "node-pool",
        "operator": "Equal",
        "value": value,
        "effect": "NoSchedule",
    }


class GpuWorkspacePolicy(sky.AdminPolicy):
    @classmethod
    def validate_and_mutate(cls, user_request: sky.UserRequest) -> sky.MutatedUserRequest:
        # Only apply to Kubernetes tasks
        resources = user_request.task.get_resource_config()
        if not resources.get("cloud", "").startswith("kubernetes"):
            return sky.MutatedUserRequest(user_request.task, user_request.skypilot_config)

        # Get GPU type from resources
        accelerator = resources.get("accelerators", {}) or {}
        gpu_type = None
        if isinstance(accelerator, dict) and accelerator:
            gpu_type = list(accelerator.keys())[0]
        elif accelerator:
            gpu_type = accelerator

        # Cluster name for jobName — same source as skypilot-cluster-name (may not include
        # the internal hash suffix SkyPilot adds when creating the pod).
        cluster_name = "sky-cluster"
        if user_request.request_options is not None and user_request.request_options.cluster_name:
            cluster_name = user_request.request_options.cluster_name
        elif getattr(user_request.task, "name", None):
            cluster_name = user_request.task.name

        workspace_id = _get_workspace_id(user_request)
        workload_type_label = _workload_type(workspace_id)

        # Build pod config mutations (labels, nodeSelector, affinity, tolerations)
        # sapCode = skypilot-workspace value; jobName = skypilot-cluster-name value
        pod_config_mutations = {
            "metadata": {
                "labels": {
                    "sapCode": workspace_id,
                    "jobName": cluster_name,
                }
            },
            "spec": {},
        }

        # nodeSelector + affinity for GPU: node-pool=gpu-nvidia-<h200|a10g|l4>, workload-type=<SAP Code|general_research_development>
        node_pool_val = _node_pool_value(gpu_type) if gpu_type else None
        if node_pool_val is not None:
            pod_config_mutations["spec"]["nodeSelector"] = {
                "node-pool": node_pool_val,
                "workload-type": workload_type_label,
            }
            workload_values_required = [DEFAULT_WORKLOAD_TYPE]
            if workload_type_label != DEFAULT_WORKLOAD_TYPE:
                workload_values_required.append(workload_type_label)
            pod_config_mutations["spec"]["affinity"] = {
                "nodeAffinity": {
                    "preferredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "preference": {
                                "matchExpressions": [
                                    {"key": "workload-type", "operator": "In", "values": [workload_type_label]}
                                ]
                            },
                            "weight": 100,
                        },
                        {
                            "preference": {
                                "matchExpressions": [
                                    {"key": "workload-type", "operator": "In", "values": [DEFAULT_WORKLOAD_TYPE]}
                                ]
                            },
                            "weight": 10,
                        },
                    ],
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {"key": "node-pool", "operator": "In", "values": [node_pool_val]},
                                    {"key": "workload-type", "operator": "In", "values": workload_values_required},
                                ]
                            }
                        ]
                    },
                }
            }

        # Tolerations: nvidia.com/gpu, node-pool (for your GPU pools), and workspace
        tolerations = []
        if gpu_type:
            tolerations.append({
                "key": "nvidia.com/gpu",
                "operator": "Exists",
                "effect": "NoSchedule",
            })
            node_pool = _node_pool_toleration(gpu_type)
            if node_pool is not None:
                tolerations.append(node_pool)
        tolerations.append({
            "key": f"workspace/{workspace_id}",
            "operator": "Equal",
            "value": "true",
            "effect": "NoSchedule",
        })
        pod_config_mutations["spec"]["tolerations"] = tolerations

        # Apply mutations to skypilot_config (deep copy so we don't mutate the original)
        mutated_config = config_utils.Config.from_dict(
            copy.deepcopy(dict(user_request.skypilot_config))
        )
        k8s_config = mutated_config.get_nested(("kubernetes",), {})
        if not isinstance(k8s_config, dict):
            k8s_config = {}
        existing_pod_config = k8s_config.get("pod_config", {}) or {}
        merged_pod_config = copy.deepcopy(existing_pod_config)
        config_utils.merge_k8s_configs(merged_pod_config, pod_config_mutations)

        k8s_config["pod_config"] = merged_pod_config
        mutated_config["kubernetes"] = k8s_config

        # Also inject labels via the task's resources so they are applied when the pod
        # is created (API server may not use mutated_config when building the pod).
        # 1) Resource labels — on K8s these map to pod labels.
        # 2) cluster_config_overrides — inject pod_config.metadata.labels so they
        #    are merged into the config used for pod creation.
        task = user_request.task
        extra_labels = {"sapCode": workspace_id, "jobName": cluster_name}
        pod_config_override = {
            "metadata": {"labels": extra_labels},
        }

        new_resources_list = []
        for res in list(task.resources):
            # Merge our labels into resource labels (task-level labels become pod labels on K8s)
            existing_labels = dict(res.labels) if res.labels else {}
            new_labels = {**existing_labels, **extra_labels}
            # Merge our pod_config into cluster_config_overrides so pod creation sees it
            existing_overrides = dict(res.cluster_config_overrides) if res.cluster_config_overrides else {}
            k8s_overrides = existing_overrides.get("kubernetes", {}) or {}
            existing_pod = k8s_overrides.get("pod_config", {}) or {}
            merged_pod = copy.deepcopy(existing_pod)
            config_utils.merge_k8s_configs(merged_pod, pod_config_override)
            new_k8s = {**k8s_overrides, "pod_config": merged_pod}
            new_overrides = {**existing_overrides, "kubernetes": new_k8s}
            new_resources_list.append(res.copy(labels=new_labels, _cluster_config_overrides=new_overrides))
        task.set_resources(type(task.resources)(new_resources_list))

        return sky.MutatedUserRequest(task, mutated_config)