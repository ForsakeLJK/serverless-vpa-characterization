from pathlib import Path

import pytest

from vpaexp.metrics import cpu_was_limiting, oom_kill_times, summarize
from vpaexp.records import load_run

from .synthetic import MI, T0, audit_rec, audit_resize, audit_status, pod, res, scenario, write_run


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
    # 1000 Mi until 22s, 2000 Mi until 65s, 500 Mi to the end
    assert s["whole"]["alloc_mem_bytes"] == pytest.approx(1255 * MI)
    assert s["first_day"]["alloc_mem_bytes"] == pytest.approx(1450 * MI)
    assert s["warmed"]["alloc_mem_bytes"] == pytest.approx(1125 * MI)


def test_utilization_is_usage_over_the_request_in_force(tmp_path: Path) -> None:
    s = summarize(load_run(scenario(tmp_path)))
    # 500 Mi against 1000 Mi, then 2000 Mi, then 500 Mi
    assert s["whole"]["util_mem"] == pytest.approx((22 * 0.5 + 43 * 0.25 + 35 * 1.0) / 100, abs=0.01)
    assert s["whole"]["usage_mem_bytes"] == pytest.approx(500 * MI)


def test_each_resize_carries_three_timestamps(tmp_path: Path) -> None:
    up, down = load_run(scenario(tmp_path)).resizes
    assert up.rec_updated_t == T0 + 10  # first write of the target that was applied, not the repeat at 15s
    assert up.patched_t == T0 + 20
    assert up.applied_t == T0 + 22
    assert (down.rec_updated_t, down.patched_t, down.applied_t) == (T0 + 58, T0 + 60, T0 + 65)
    assert up.complete and down.complete


def test_resizes_are_counted_by_direction(tmp_path: Path) -> None:
    s = summarize(load_run(scenario(tmp_path)))
    w = s["whole"]
    assert (w["resize_mem_up"], w["resize_mem_down"]) == (1, 1)
    assert w["resizes"] == 2
    assert s["first_day"]["resizes"] == 1 and s["warmed"]["resize_mem_down"] == 1


def test_a_resize_the_kubelet_deferred_is_counted_as_deferred(tmp_path: Path) -> None:
    run = load_run(scenario(tmp_path))
    assert run.resizes[0].issues == () and run.resizes[1].issues == ("Deferred",)
    assert summarize(run)["whole"]["resizes_failed_or_deferred"] == 1


def test_a_rejected_patch_and_a_never_applied_resize_both_count_as_failed(tmp_path: Path) -> None:
    start, big = res(1000), res(9000)
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


def test_oom_kills_come_from_the_cgroup_counter(tmp_path: Path) -> None:
    s = summarize(load_run(scenario(tmp_path)))
    assert s["whole"]["oom_kills"] == 1 and s["first_day"]["oom_kills"] == 0 and s["warmed"]["oom_kills"] == 1


def test_cpu_is_not_a_result_but_throttling_flags_a_run_whose_cpu_was_not_generous(tmp_path: Path) -> None:
    throttled = load_run(scenario(tmp_path / "a"))
    whole = summarize(throttled)["whole"]
    assert not any("cpu" in key and key != "cpu_throttled_s" for key in whole)
    assert whole["cpu_throttled_s"] == pytest.approx(0.25)
    assert cpu_was_limiting(throttled) == "CPU was throttled for 0.2s (0.2% of the run)"
    quiet = write_run(tmp_path / "b", client=[(1.0, 0.1, "ok")], sampler=[(float(i), 0.1, 100.0, 0, 0, 0.0) for i in range(5)])
    assert cpu_was_limiting(load_run(quiet)) is None


def test_recommendation_series_is_read_from_the_vpa_watch(tmp_path: Path) -> None:
    recs = load_run(scenario(tmp_path)).recommendations
    assert [(r.t - T0, r.target // MI, r.lower // MI, r.upper // MI) for r in recs] == [
        (10.0, 2000, 900, 4000),
        (58.0, 500, 400, 700),
    ]


def test_applied_time_comes_from_the_kubelets_status_write_when_the_audit_log_has_it(tmp_path: Path) -> None:
    start, big = res(1000), res(2000)
    d = write_run(
        tmp_path,
        client=[(1.0, 0.1, "ok")],
        pods=[pod(0.0, start, start), pod(20.3, big, start), pod(29.0, big, big)],  # the watch saw it late
        audit=[
            audit_rec(10.0, big),
            audit_resize(20.0, big),
            audit_status(20.5, big, start, [{"type": "PodResizeInProgress", "status": "True"}]),
            audit_status(21.5, big, big),
        ],
    )
    (resize,) = load_run(d).resizes
    assert resize.applied_t == T0 + 21.5


def test_a_resize_is_not_applied_while_the_kubelet_still_reports_it_in_progress(tmp_path: Path) -> None:
    start, big = res(1000), res(2000)
    d = write_run(
        tmp_path,
        client=[(1.0, 0.1, "ok")],
        pods=[
            pod(0.0, start, start),
            pod(20.1, big, big, [{"type": "PodResizeInProgress", "status": "True"}]),
            pod(24.0, big, big),
        ],
        audit=[audit_rec(10.0, big), audit_resize(20.0, big)],
    )
    (resize,) = load_run(d).resizes
    assert resize.applied_t == T0 + 24.0


def test_container_restarts_are_counted_and_flag_a_static_run(tmp_path: Path) -> None:
    from vpaexp.metrics import static_sizing_too_low

    a = res(1000)
    d = write_run(
        tmp_path,
        client=[(1.0, 0.1, "ok")],
        sampler=[(float(i), 0.1, 100.0, 0, 0, 0.0) for i in range(30)],
        pods=[pod(0.0, a, a), pod(50.0, a, a, restarts=1, last_exit="OOMKilled"), pod(51.0, a, a, restarts=1, last_exit="OOMKilled")],
    )
    run = load_run(d)
    s = summarize(run)
    assert s["whole"]["container_restarts"] == 1
    assert s["whole"]["oom_kills"] == 1  # the container-level OOM never reaches the cgroup counter we sample
    assert s["first_day"]["container_restarts"] == 0 and s["warmed"]["container_restarts"] == 1
    assert static_sizing_too_low(run) == ["container restart"]  # (the cgroup counter itself stayed at zero)


def test_audit_events_of_other_objects_on_the_cluster_are_ignored(tmp_path: Path) -> None:
    start, big = res(1000), res(2000)
    other = audit_rec(15.0, big)
    other["objectRef"]["name"] = "primes-vpa"
    d = write_run(
        tmp_path,
        client=[(1.0, 0.1, "ok")],
        pods=[pod(0.0, start, start), pod(20.1, big, big)],
        audit=[audit_rec(10.0, start), other, audit_resize(20.0, big)],
    )
    (resize,) = load_run(d).resizes
    assert resize.rec_updated_t == T0 + 10  # the last write to our VPA, not the other one's at 15s


def test_oom_kill_times_cover_kills_inside_the_container_and_of_the_container(tmp_path: Path) -> None:
    a = res(1000)
    d = write_run(
        tmp_path,
        client=[(1.0, 0.1, "ok")],
        sampler=[(float(i), 0.1, 100.0, 1 if i >= 20 else 0, 0, 0.0) for i in range(30)],
        pods=[pod(0.0, a, a), pod(50.0, a, a, restarts=1, last_exit="OOMKilled"), pod(70.0, a, a, restarts=2, last_exit="Error")],
    )
    assert list(oom_kill_times(load_run(d)) - T0) == [20.0, 50.0]


def test_a_container_oom_kill_is_timed_at_the_kill_not_at_the_restart_after_back_off(tmp_path: Path) -> None:
    a = res(1000)
    d = write_run(
        tmp_path,
        client=[(1.0, 0.1, "ok")],
        sampler=[(float(i), 0.1, 100.0, 0, 0, 0.0) for i in range(30)],
        pods=[pod(0.0, a, a), pod(65.0, a, a, restarts=1, last_exit="OOMKilled", exited_at=50.0)],
    )
    assert list(oom_kill_times(load_run(d)) - T0) == [50.0]
