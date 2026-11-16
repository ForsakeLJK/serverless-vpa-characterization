import pytest

from vpaexp.config import (
    Allocation,
    UndeclaredKnob,
    deployment_manifest,
    recommender_args,
    updater_args,
    vpa_flags,
    vpa_manifest,
)

MI = 1024 * 1024


def test_default_divides_every_time_based_upstream_setting_by_k() -> None:
    f = vpa_flags(k=4)
    assert f.recommender == {
        "cpu-histogram-decay-half-life": "6h0m0s",
        "memory-histogram-decay-half-life": "6h0m0s",
        "memory-aggregation-interval": "6h0m0s",
        "memory-aggregation-interval-count": "8",
        "confidence-interval-cpu": "6h0m0s",
        "confidence-interval-memory": "6h0m0s",
        "recommender-interval": "15s",
        "target-cpu-percentile": "0.9",
        "target-memory-percentile": "0.9",
        "recommendation-margin-fraction": "0.15",
        "pod-recommendation-min-cpu-millicores": "25",
        "pod-recommendation-min-memory-mb": "250",
    }
    assert f.updater == {
        "in-recommendation-bounds-eviction-lifetime-threshold": "3h0m0s",
        "evict-after-oom-threshold": "2m30s",
        "updater-interval": "15s",
        "pod-update-threshold": "0.1",
    }


def test_unscaled_flags_are_the_upstream_defaults() -> None:
    f = vpa_flags(k=1)
    assert f.recommender["cpu-histogram-decay-half-life"] == "24h0m0s"
    assert f.updater["evict-after-oom-threshold"] == "10m0s"
    assert f.updater["updater-interval"] == "1m0s"


def test_tuning_overrides_a_declared_knob_and_leaves_the_rest() -> None:
    f = vpa_flags(k=4, overrides={"memory-histogram-decay-half-life": "1h0m0s", "pod-update-threshold": "0.05"})
    assert f.recommender["memory-histogram-decay-half-life"] == "1h0m0s"
    assert f.updater["pod-update-threshold"] == "0.05"
    assert f.recommender["memory-aggregation-interval"] == "6h0m0s"


@pytest.mark.parametrize("knob", ["recommender-interval", "updater-interval", "evict-after-oom-threshold", "nope",
                                  "cpu-histogram-decay-half-life", "target-cpu-percentile", "pod-recommendation-min-cpu-millicores"])
def test_tuning_may_not_touch_anything_outside_the_declared_knobs(knob: str) -> None:
    with pytest.raises(UndeclaredKnob):
        vpa_flags(k=4, overrides={knob: "1s"})


def test_updater_runs_with_the_in_place_feature_gate() -> None:
    f = vpa_flags(k=4)
    assert "--feature-gates=InPlace=true" in updater_args(f)
    assert "--updater-interval=15s" in updater_args(f)
    assert "--recommender-interval=15s" in recommender_args(f)
    assert not any("feature-gates" in a for a in recommender_args(f))


def test_function_pod_is_guaranteed_and_never_restarts_on_resize() -> None:
    d = deployment_manifest("ns", "img:1", Allocation(cpu_millicores=3138, memory_bytes=2287 * MI), concurrency_cap=68)
    assert d["spec"]["replicas"] == 1
    (c,) = d["spec"]["template"]["spec"]["containers"]
    assert c["resources"]["requests"] == c["resources"]["limits"] == {"cpu": "3138m", "memory": "2287Mi"}
    assert c["resizePolicy"] == [
        {"resourceName": "cpu", "restartPolicy": "NotRequired"},
        {"resourceName": "memory", "restartPolicy": "NotRequired"},
    ]
    assert {"name": "CONCURRENCY_CAP", "value": "68"} in c["env"]


def test_vpa_object_resizes_memory_only_in_place_and_keeps_requests_equal_to_limits() -> None:
    v = vpa_manifest("ns", max_memory_bytes=200 * 1024 * MI)
    assert v["spec"]["targetRef"] == {"apiVersion": "apps/v1", "kind": "Deployment", "name": "function"}
    assert v["spec"]["updatePolicy"]["updateMode"] == "InPlace"
    (policy,) = v["spec"]["resourcePolicy"]["containerPolicies"]
    assert policy["controlledValues"] == "RequestsAndLimits"
    assert policy["controlledResources"] == ["memory"]
    assert policy["maxAllowed"] == {"memory": "204800Mi"}
