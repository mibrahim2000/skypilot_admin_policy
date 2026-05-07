"""Unit tests for admin policy helpers in main.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sky.server.requests import request_names

import main


def _np(value: str, *, operator: str = "Equal", effect: str = "NoSchedule") -> dict:
    return {
        "key": main.NODE_POOL_KEY,
        "operator": operator,
        "value": value,
        "effect": effect,
    }


def _wt_kueue(*, operator: str = "Equal", effect: str = "NoSchedule") -> dict:
    return {
        "key": main.WORKLOAD_TYPE_KEY,
        "operator": operator,
        "value": main.WORKLOAD_TYPE_KUEUE_VALUE,
        "effect": effect,
    }


def _h200_topology_annotations() -> dict:
    return {main.KUEUE_PODSET_TOPOLOGY_ANNOTATION_KEY: main.KUEUE_PODSET_TOPOLOGY_ANNOTATION_VALUE}


class TestHasNodePoolToleration:
    def test_accepts_equal_noschedule_non_empty_value(self) -> None:
        tol = [_np("cpu-only")]
        assert main._has_node_pool_toleration(tol) is True

    def test_rejects_wrong_key(self) -> None:
        tol = [{"key": "other", "operator": "Equal", "value": "x", "effect": "NoSchedule"}]
        assert main._has_node_pool_toleration(tol) is False

    def test_rejects_wrong_effect(self) -> None:
        tol = [_np("cpu-only", effect="NoExecute")]
        assert main._has_node_pool_toleration(tol) is False


class TestHasNonH200NodePoolToleration:
    def test_cpu_only(self) -> None:
        assert main._has_non_h200_node_pool_toleration([_np("cpu-only")]) is True

    def test_a10g(self) -> None:
        assert main._has_non_h200_node_pool_toleration([_np("gpu-nvidia-a10g")]) is True

    def test_l4(self) -> None:
        assert main._has_non_h200_node_pool_toleration([_np("gpu-nvidia-l4")]) is True

    def test_h200_only(self) -> None:
        assert main._has_non_h200_node_pool_toleration([_np(main.NODE_POOL_H200_VALUE)]) is False

    def test_merged_h200_and_a10g_prefers_non_h200(self) -> None:
        tols = [_np(main.NODE_POOL_H200_VALUE), _np("gpu-nvidia-a10g")]
        assert main._has_non_h200_node_pool_toleration(tols) is True


class TestSelectsH200NodePool:
    def test_true_when_h200(self) -> None:
        assert main._selects_h200_node_pool([_np(main.NODE_POOL_H200_VALUE)]) is True

    def test_false_when_a10g(self) -> None:
        assert main._selects_h200_node_pool([_np("gpu-nvidia-a10g")]) is False


class TestHasWorkloadTypeKueueToleration:
    def test_present(self) -> None:
        assert main._has_workload_type_kueue_toleration([_wt_kueue()]) is True

    def test_absent(self) -> None:
        assert main._has_workload_type_kueue_toleration([_np("cpu-only")]) is False


class TestTolerationsPass:
    """Core behaviour: non-H200 node-pool exempts H200 workload-type=kueue requirement."""

    def test_cpu_only_without_workload_type_passes(self) -> None:
        assert main._tolerations_pass([_np("cpu-only")]) is True

    def test_a10g_without_workload_type_passes(self) -> None:
        assert main._tolerations_pass([_np("gpu-nvidia-a10g")]) is True

    def test_h200_without_kueue_fails(self) -> None:
        assert main._tolerations_pass([_np(main.NODE_POOL_H200_VALUE)]) is False

    def test_h200_with_kueue_passes(self) -> None:
        tols = [_np(main.NODE_POOL_H200_VALUE), _wt_kueue()]
        assert main._tolerations_pass(tols) is True

    def test_merged_global_h200_plus_task_a10g_passes_without_kueue(self) -> None:
        tols = [_np(main.NODE_POOL_H200_VALUE), _np("gpu-nvidia-a10g")]
        assert main._tolerations_pass(tols) is True

    def test_no_node_pool_fails(self) -> None:
        assert main._tolerations_pass([_wt_kueue()]) is False


class TestH200AnnotationPass:
    def test_h200_requires_topology_annotation(self) -> None:
        tols = [_np(main.NODE_POOL_H200_VALUE), _wt_kueue()]
        assert main._h200_annotation_pass(tols, {}) is False
        assert main._h200_annotation_pass(tols, _h200_topology_annotations()) is True

    def test_h200_wrong_annotation_value_fails(self) -> None:
        tols = [_np(main.NODE_POOL_H200_VALUE), _wt_kueue()]
        bad = {main.KUEUE_PODSET_TOPOLOGY_ANNOTATION_KEY: "wrong"}
        assert main._h200_annotation_pass(tols, bad) is False

    def test_a10g_ignores_missing_annotation(self) -> None:
        tols = [_np("gpu-nvidia-a10g")]
        assert main._h200_annotation_pass(tols, {}) is True

    def test_merged_h200_and_a10g_exempt(self) -> None:
        tols = [_np(main.NODE_POOL_H200_VALUE), _np("gpu-nvidia-a10g")]
        assert main._h200_annotation_pass(tols, {}) is True


class TestHasSapLabels:
    def test_valid_matching_case(self) -> None:
        labels = [{"sapCode": "ABC", "kueue.x-k8s.io/queue-name": "abc"}]
        assert main._has_sap_labels(labels) is True

    def test_mismatched_codes_fail(self) -> None:
        labels = [{"sapCode": "ABC", "kueue.x-k8s.io/queue-name": "xyz"}]
        assert main._has_sap_labels(labels) is False

    def test_queue_not_lowercase_fails(self) -> None:
        labels = [{"sapCode": "ABC", "kueue.x-k8s.io/queue-name": "Abc"}]
        assert main._has_sap_labels(labels) is False


class TestIsKubernetesResources:
    def test_infra_kubernetes(self) -> None:
        assert main._is_kubernetes_resources({"infra": "kubernetes"}) is True

    def test_cloud_kubernetes_prefix(self) -> None:
        assert main._is_kubernetes_resources({"cloud": "kubernetes"}) is True

    def test_aws_skipped(self) -> None:
        assert main._is_kubernetes_resources({"cloud": "aws"}) is False


class TestValidateAndMutateIntegration:
    """Smoke tests with mocked UserRequest (no full SkyPilot stack)."""

    @staticmethod
    def _make_request(
        *,
        resource_config: dict,
        global_pod_config: dict | None,
        resource_overrides: dict | None,
        request_name: request_names.AdminPolicyRequestName = (
            request_names.AdminPolicyRequestName.OPTIMIZE
        ),
    ) -> MagicMock:
        res = MagicMock()
        res.cluster_config_overrides = resource_overrides or {}

        task = MagicMock()
        task.resources = [res]
        task.get_resource_config.return_value = resource_config

        cfg = MagicMock()
        cfg.get_nested = MagicMock(return_value=global_pod_config)

        ur = MagicMock()
        ur.task = task
        ur.skypilot_config = cfg
        ur.request_name = request_name
        return ur

    def test_non_kubernetes_always_accepts(self) -> None:
        ur = self._make_request(
            resource_config={"cloud": "aws"},
            global_pod_config=None,
            resource_overrides=None,
        )
        out = main.WorkloadTypeTolerationPolicy.validate_and_mutate(ur)
        assert out.task is ur.task
        assert out.skypilot_config is ur.skypilot_config

    def test_cluster_launch_rejected_even_for_aws(self) -> None:
        ur = self._make_request(
            resource_config={"cloud": "aws"},
            global_pod_config=None,
            resource_overrides=None,
            request_name=request_names.AdminPolicyRequestName.CLUSTER_LAUNCH,
        )
        with pytest.raises(main.exceptions.UserRequestRejectedByPolicy) as exc:
            main.WorkloadTypeTolerationPolicy.validate_and_mutate(ur)
        assert "sky jobs launch" in str(exc.value).lower()

    def test_jobs_launch_cluster_allowed_with_valid_kubernetes_pod(self) -> None:
        pod = {
            "metadata": {
                "labels": {
                    "sapCode": "FOO",
                    "kueue.x-k8s.io/queue-name": "foo",
                }
            },
            "spec": {"tolerations": [_np("gpu-nvidia-a10g")]},
        }
        ur = self._make_request(
            resource_config={"infra": "kubernetes"},
            global_pod_config=None,
            resource_overrides={"kubernetes": {"pod_config": pod}},
            request_name=request_names.AdminPolicyRequestName.JOBS_LAUNCH_CLUSTER,
        )
        out = main.WorkloadTypeTolerationPolicy.validate_and_mutate(ur)
        assert out.task is ur.task

    def test_kubernetes_passes_with_overrides_only(self) -> None:
        pod = {
            "metadata": {
                "labels": {
                    "sapCode": "FOO",
                    "kueue.x-k8s.io/queue-name": "foo",
                }
            },
            "spec": {
                "tolerations": [
                    _np("gpu-nvidia-a10g"),
                ]
            },
        }
        ur = self._make_request(
            resource_config={"infra": "kubernetes"},
            global_pod_config=None,
            resource_overrides={"kubernetes": {"pod_config": pod}},
        )
        out = main.WorkloadTypeTolerationPolicy.validate_and_mutate(ur)
        assert out.task is ur.task

    def test_kubernetes_rejects_missing_labels(self) -> None:
        pod = {
            "metadata": {"labels": {}},
            "spec": {"tolerations": [_np("cpu-only")]},
        }
        ur = self._make_request(
            resource_config={"infra": "kubernetes"},
            global_pod_config=None,
            resource_overrides={"kubernetes": {"pod_config": pod}},
        )
        with pytest.raises(main.exceptions.UserRequestRejectedByPolicy):
            main.WorkloadTypeTolerationPolicy.validate_and_mutate(ur)

    def test_kubernetes_h200_without_topology_rejected(self) -> None:
        pod = {
            "metadata": {
                "labels": {
                    "sapCode": "FOO",
                    "kueue.x-k8s.io/queue-name": "foo",
                }
            },
            "spec": {
                "tolerations": [
                    _np(main.NODE_POOL_H200_VALUE),
                    _wt_kueue(),
                ]
            },
        }
        ur = self._make_request(
            resource_config={"infra": "kubernetes"},
            global_pod_config=None,
            resource_overrides={"kubernetes": {"pod_config": pod}},
        )
        with pytest.raises(main.exceptions.UserRequestRejectedByPolicy):
            main.WorkloadTypeTolerationPolicy.validate_and_mutate(ur)

    def test_kubernetes_h200_with_topology_accepted(self) -> None:
        pod = {
            "metadata": {
                "labels": {
                    "sapCode": "FOO",
                    "kueue.x-k8s.io/queue-name": "foo",
                },
                "annotations": _h200_topology_annotations(),
            },
            "spec": {
                "tolerations": [
                    _np(main.NODE_POOL_H200_VALUE),
                    _wt_kueue(),
                ]
            },
        }
        ur = self._make_request(
            resource_config={"infra": "kubernetes"},
            global_pod_config=None,
            resource_overrides={"kubernetes": {"pod_config": pod}},
        )
        out = main.WorkloadTypeTolerationPolicy.validate_and_mutate(ur)
        assert out.task is ur.task

    def test_kubernetes_h200_plus_a10g_no_topology_ok(self) -> None:
        pod = {
            "metadata": {
                "labels": {
                    "sapCode": "FOO",
                    "kueue.x-k8s.io/queue-name": "foo",
                }
            },
            "spec": {
                "tolerations": [
                    _np(main.NODE_POOL_H200_VALUE),
                    _np("gpu-nvidia-a10g"),
                ]
            },
        }
        ur = self._make_request(
            resource_config={"infra": "kubernetes"},
            global_pod_config=None,
            resource_overrides={"kubernetes": {"pod_config": pod}},
        )
        out = main.WorkloadTypeTolerationPolicy.validate_and_mutate(ur)
        assert out.task is ur.task
