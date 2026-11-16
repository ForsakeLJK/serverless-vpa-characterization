from pathlib import Path

import pytest

from vpaexp.metrics import summarize
from vpaexp.records import load_run

from .synthetic import MI, T0, audit_rec, audit_resize, pod, res, scenario, write_run


def test_latency_counts_timeouts_at_the_timeout_value_and_reports_errors_separately(tmp_path: Path) -> None:
    s = summarize(load_run(scenario(tmp_path)))["warmed"]
    assert s["requests"] == 100
    assert s["p50_s"] == pytest.approx(0.1)
    assert s["p99_s"] == pytest.approx(3.3)  # 97 ok at 0.1s and 2 timeouts at 3.3s
    assert s["timeout_rate"] == pytest.approx(0.02)
    assert s["error_rate"] == pytest.approx(0.01)
    assert s["throughput_rps"] == pytest.approx(97 / 60)


def test_metrics_are_split_into_the_first_scaled_day_and_the_rest(tmp_path: Path) -> None:
    s = summarize(load_run(scenario(tmp_path)))
    assert s["first_day"]["requests"] == 10 and s["first_day"]["p99_s"] == pytest.approx(0.2)
    assert s["whole"]["requests"] == 110
    assert s["whole"]["throughput_rps"] == pytest.approx(107 / 100)


def test_a_run_without_a_phase_boundary_reports_only_the_whole_run(tmp_path: Path) -> None:
    s = summarize(load_run(scenario(tmp_path, phase_boundary_s=None)))
    assert set(s) == {"whole"}


def test_allocation_is_the_time_weighted_mean_of_what_the_pod_was_given(tmp_path: Path) -> None:
    s = summarize(load_run(scenario(tmp_path)))
    # 1000m until 22s, 2000m until 65s, 500m to the end
    assert s["whole"]["alloc_cpu_m"] == pytest.approx(1255)
    assert s["first_day"]["alloc_cpu_m"] == pytest.approx(1450)
    assert s["warmed"]["alloc_cpu_m"] == pytest.approx(1125)
    assert s["whole"]["alloc_mem_bytes"] == pytest.approx((1000 * 65 + 500 * 35) / 100 * MI)


def test_utilization_is_usage_over_the_request_in_force(tmp_path: Path) -> None:
    s = summarize(load_run(scenario(tmp_path)))
    # 0.5 cores against 1000m, then 2000m, then 500m
    assert s["whole"]["util_cpu"] == pytest.approx((22 * 0.5 + 43 * 0.25 + 35 * 1.0) / 100, abs=0.01)
    assert s["whole"]["usage_cpu_cores"] == pytest.approx(0.5)


def test_each_resize_carries_three_timestamps(tmp_path: Path) -> None:
    up, down = load_run(scenario(tmp_path)).resizes
    assert up.rec_updated_t == T0 + 10  # first write of the target that was applied, not the repeat at 15s
    assert up.patched_t == T0 + 20
    assert up.applied_t == T0 + 22
    assert (down.rec_updated_t, down.patched_t, down.applied_t) == (T0 + 58, T0 + 60, T0 + 65)
    assert up.complete and down.complete


def test_resizes_are_counted_by_resource_and_direction(tmp_path: Path) -> None:
    s = summarize(load_run(scenario(tmp_path)))
    w = s["whole"]
    assert (w["resize_cpu_up"], w["resize_cpu_down"], w["resize_mem_up"], w["resize_mem_down"]) == (1, 1, 0, 1)
    assert w["resizes"] == 2
    assert s["first_day"]["resizes"] == 1 and s["warmed"]["resize_cpu_down"] == 1


def test_a_resize_the_kubelet_deferred_is_counted_as_deferred(tmp_path: Path) -> None:
    run = load_run(scenario(tmp_path))
    assert run.resizes[0].issues == () and run.resizes[1].issues == ("Deferred",)
    assert summarize(run)["whole"]["resizes_failed_or_deferred"] == 1


def test_a_rejected_patch_and_a_never_applied_resize_both_count_as_failed(tmp_path: Path) -> None:
    start, big = res(1000, 1000), res(9000, 1000)
    d = write_run(
        tmp_path,
        client=[(1.0, 0.1, "ok")],
        pods=[
            pod(0.0, start, start),
            pod(30.1, big, start, [{"type": "PodResizePending", "status": "True", "reason": "Infeasible"}]),
        ],
        audit=[audit_rec(5.0, big), audit_resize(20.0, big, code=422), audit_resize(30.0, big)],
    )
    run = load_run(d)
    rejected, infeasible = run.resizes
    assert rejected.outcome == "rejected"
    assert infeasible.outcome == "pending" and infeasible.applied_t is None and infeasible.issues == ("Infeasible",)
    w = summarize(run)["whole"]
    assert w["resizes"] == 0 and w["resizes_failed_or_deferred"] == 2


def test_oom_kills_and_throttling_come_from_the_cgroup_counters(tmp_path: Path) -> None:
    s = summarize(load_run(scenario(tmp_path)))
    assert s["whole"]["oom_kills"] == 1 and s["first_day"]["oom_kills"] == 0 and s["warmed"]["oom_kills"] == 1
    assert s["whole"]["throttled_periods"] == 3
    assert s["whole"]["throttled_s"] == pytest.approx(0.25)


def test_recommendation_series_is_read_from_the_vpa_watch(tmp_path: Path) -> None:
    recs = load_run(scenario(tmp_path)).recommendations
    assert [(r.t - T0, r.target.cpu_millicores, r.lower.cpu_millicores, r.upper.cpu_millicores) for r in recs] == [
        (10.0, 2000, 900, 4000),
        (58.0, 500, 400, 700),
    ]
    assert recs[0].upper.memory_bytes == 2000 * MI
