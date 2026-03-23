# SkyPilot Admin Policy — workload-type toleration

Validates that **Kubernetes** SkyPilot tasks declare a **`workload-type` toleration** with a non-empty value (your SAP code). Jobs without this toleration are **rejected** before scheduling.

Non-Kubernetes tasks are unchanged.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

## Install (uv)

From the project root:

```bash
uv sync
```

This creates a `.venv`, installs dependencies from `uv.lock`, and installs this package in editable mode. Use this environment when running SkyPilot (for example `uv run sky launch ...`, or activate `.venv` and run `sky`).

To add the package to an existing environment:

```bash
uv pip install -e .
```

## Configure SkyPilot

In your SkyPilot config (`~/.sky/config.yaml`, task `config:` block, or API server config), set:

```yaml
admin_policy: main.WorkloadTypeTolerationPolicy
```

The value is the import path to the policy class (`module.ClassName`).

**Client-side:** run SkyPilot from the environment where the package is installed, with `admin_policy` set as above.

**Server-side:** install this package in the API server Python environment and set the same `admin_policy` in the server SkyPilot config (for example under `apiService.config` in Helm).

## What users must add

The policy looks for a toleration with:

- `key: workload-type`
- `operator: Equal` (optional to omit; Kubernetes defaults to Equal)
- `effect: NoSchedule`
- `value:` set to your SAP code (non-empty string)

Tolerations are read from:

- `kubernetes.pod_config.spec.tolerations` in the merged SkyPilot config, and
- each resource’s `cluster_config_overrides.kubernetes.pod_config.spec.tolerations`.

Example (global config or task `config:`):

```yaml
config:
  kubernetes:
    pod_config:
      spec:
        tolerations:
          - key: node-pool
            operator: Equal
            value: gpu-nvidia-h200
            effect: NoSchedule
          - key: workload-type
            operator: Equal
            value: <YOUR-SAP-CODE>
            effect: NoSchedule
```

You typically also need the **`node-pool`** toleration (and matching selectors) that your cluster expects; this policy only **enforces** the **`workload-type`** entry.

## Build a wheel (optional)

```bash
uv build
```

Install the wheel elsewhere:

```bash
uv pip install dist/skypilot_admin_policy-0.1.0-*.whl
```

## References

- [SkyPilot admin policy docs](https://skypilot.readthedocs.io/en/latest/cloud-setup/policy.html)
- Example policies: [skypilot-org/skypilot `examples/admin_policy`](https://github.com/skypilot-org/skypilot/tree/master/examples/admin_policy/example_policy)
