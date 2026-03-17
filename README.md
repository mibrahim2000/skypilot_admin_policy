# SkyPilot Admin Policy

Adds labels (`sapCode`, `jobName`), `nodeSelector`, affinity, and tolerations to Kubernetes pods for SkyPilot tasks.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

## Install (uv)

From the project root:

```bash
uv sync
```

This creates a `.venv`, installs dependencies from `uv.lock`, and installs this package in editable mode. Use this environment when running SkyPilot (e.g. `uv run sky launch ...` or activate `.venv` and run `sky`).

To add the package to an existing environment:

```bash
uv pip install -e .
```

## Use as SkyPilot admin policy

1. **Configure SkyPilot** to load this policy. In your SkyPilot config file (`~/.sky/config.yaml` or the config passed to the API server), set:

   ```yaml
   admin_policy: main.GpuWorkspacePolicy
   ```

   The value is the **import path** to your policy class: `module.ClassName`. Here the module is `main` (from `main.py`) and the class is `GpuWorkspacePolicy`.

2. **Client-side**: run SkyPilot from the environment where you ran `uv sync` (or where `skypilot-admin-policy` is installed), with `admin_policy: main.GpuWorkspacePolicy` in `~/.sky/config.yaml`.

3. **Server-side**: install this package in the API server’s Python environment (e.g. via a wheel) and set `admin_policy: main.GpuWorkspacePolicy` in the server’s SkyPilot config.

## Build a wheel (optional)

To build a wheel for another environment or the server:

```bash
uv build
```

The wheel is in `dist/`. Install it with:

```bash
uv pip install dist/skypilot_admin_policy-0.1.0-*.whl
```

Then set `admin_policy: main.GpuWorkspacePolicy` in the SkyPilot config on that machine.

## What the policy does

- **Labels:** `sapCode` (from skypilot-workspace / active_workspace) and `jobName` (from skypilot-cluster-name).
- **nodeSelector (GPU tasks):** `node-pool=gpu-nvidia-<h200|a10g|l4|...>`, `workload-type=<SAP Code|general_research_development>`.
- **Affinity:** Preferred/required node affinity for `workload-type` and `node-pool` (aligned with your coder setup).
- **Tolerations:** `nvidia.com/gpu`, `node-pool`, and `workspace/<id>`.

Only Kubernetes tasks are modified; other clouds are unchanged.
