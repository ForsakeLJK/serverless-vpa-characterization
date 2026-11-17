import pytest

from vpaexp.config import (
    Allocation,
    UndeclaredKnob,
    deployment_manifest,
    recommender_args,
    scaled_duration_s,
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


def test_at_k_4_every_scaled_flag_is_exactly_upstream_over_k() -> None:
    assert vpa_flags(k=4).scaling == {}


def test_rehearsal_k_rounds_to_whole_seconds_and_floors_loop_intervals_at_the_metrics_resolution() -> None:
    f = vpa_flags(k=96)
    assert f.recommender["memory-histogram-decay-half-life"] == "15m0s"  # 86400 / 96, exact
    assert f.recommender["memory-aggregation-interval"] == "15m0s"
    assert f.updater["in-recommendation-bounds-eviction-lifetime-threshold"] == "7m30s"  # 43200 / 96, exact
    assert f.updater["evict-after-oom-threshold"] == "6s"  # 6.25s, rounded
    assert f.recommender["recommender-interval"] == f.updater["updater-interval"] == "15s"  # 0.625s, floored
    assert f.scaling == {
        "recommender-interval": {"exact_s": 0.625, "used_s": 15, "reason": "floored at the metrics resolution"},
        "updater-interval": {"exact_s": 0.625, "used_s": 15, "reason": "floored at the metrics resolution"},
        "evict-after-oom-threshold": {"exact_s": 6.25, "used_s": 6, "reason": "rounded to whole seconds"},
    }


@pytest.mark.parametrize("upstream_s, k, expected", [(600, 96, 6), (600, 80, 8), (600, 48, 13), (600, 1000, 1)])
def test_scaled_durations_round_halves_up_and_never_reach_zero(upstream_s: int, k: int, expected: int) -> None:
    assert scaled_duration_s("evict-after-oom-threshold", upstream_s, k) == expected


def test_the_floor_applies_to_loop_intervals_only() -> None:
    assert scaled_duration_s("updater-interval", 60, 8) == 15  # 7.5s
    assert scaled_duration_s("evict-after-oom-threshold", 600, 96) == 6  # below 15s, but not a loop


def test_k_must_be_positive() -> None:
    with pytest.raises(ValueError):
        scaled_duration_s("updater-interval", 60, 0)


def test_an_overridden_knob_has_no_scaling_note() -> None:
    f = vpa_flags(k=96, overrides={"evict-after-oom-threshold": "30s"}, restrict_to_declared=False)
    assert f.updater["evict-after-oom-threshold"] == "30s"
    assert "evict-after-oom-threshold" not in f.scaling


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
