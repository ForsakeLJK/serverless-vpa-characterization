from pathlib import Path


from vpaexp.decisions import gate_checks, h1, metrics_refresh_check, select_tuned
from vpaexp.metrics import aggregate, measured_peaks, static_sizing_too_low
from vpaexp.records import load_run

from .synthetic import MI, audit_rec, audit_resize, pod, res, scenario, write_run

Named = tuple[str, dict[str, float]]


def whole(mem_mi: float = 0, p99: float = 0.40, failed: float = 0.0, ooms: int = 0) -> dict[str, float]:
    """A whole-run summary; the failed requests are split between timeouts and errors."""
    return {"alloc_mem_bytes": mem_mi * MI, "p99_s": p99, "timeout_rate": failed / 2, "error_rate": failed / 2, "oom_kills": ooms}


def cand(name: str, mem_mi: float, p99: float = 0.40, failed: float = 0.0, ooms: int = 0) -> Named:
    return name, whole(mem_mi, p99, failed, ooms)


def pick(candidates: list[Named], static_failed: float = 0.0) -> tuple[str, bool]:
    selection = select_tuned(dict(candidates), [whole(p99=0.4, failed=static_failed)])
    return selection.chosen.name, selection.quality_kept


def test_tuned_is_the_iteration_with_the_least_memory_that_keeps_service_quality() -> None:
    assert pick([
        cand("it1", 300, p99=0.41),  # least memory, but p99 worse than static's 0.40
        cand("it2", 350, failed=0.001),  # more failed requests than static
        cand("it3", 400, p99=0.38),
        cand("it4", 600),
    ]) == ("it3", True)


def test_oom_kills_do_not_disqualify_an_iteration_that_keeps_service_quality() -> None:
    assert pick([cand("it1", 400, ooms=3), cand("it2", 900)]) == ("it1", True)


def test_failed_requests_may_equal_but_not_exceed_the_static_rate() -> None:
    assert pick([cand("it1", 400, failed=0.010), cand("it2", 900)], static_failed=0.01) == ("it1", True)
    assert pick([cand("it1", 400, failed=0.011), cand("it2", 900)], static_failed=0.01) == ("it2", True)


def test_among_iterations_within_5_percent_on_memory_the_fewest_oom_kills_wins() -> None:
    assert pick([cand("it1", 400, ooms=4), cand("it2", 415, ooms=1), cand("it3", 430, ooms=0)]) == ("it2", True)


def test_when_no_iteration_keeps_service_quality_the_fewest_failed_requests_wins_and_it_is_reported() -> None:
    assert pick([cand("it1", 300, p99=0.9, failed=0.05), cand("it2", 300, p99=0.6, failed=0.02)]) == ("it2", False)


def test_static_is_as_good_as_its_worst_repetition_and_failed_requests_are_timeouts_plus_errors() -> None:
    static = [whole(p99=0.38), whole(p99=0.45, failed=0.002), whole(p99=0.40)]
    selection = select_tuned(dict([cand("it1", 400, p99=0.44, failed=0.002), cand("it2", 900)]), static)
    assert (selection.static_p99_s, selection.static_failed_rate) == (0.45, 0.002)
    assert selection.chosen.name == "it1" and selection.chosen.failed_rate == 0.002


def reps(mem: list[float], p99: list[float]) -> list[dict[str, float]]:
    return [{"alloc_mem_bytes": m, "p99_s": p} for m, p in zip(mem, p99)]


def test_h1_is_supported_when_tuned_beats_default_by_more_than_the_repetition_spread() -> None:
    default = reps([1000, 1040, 1020], [0.50, 0.52, 0.51])
    tuned = reps([800, 830, 810], [0.50, 0.52, 0.51])
    verdict = h1(default, tuned)
    assert verdict["supported"] is True
    assert verdict["metrics"]["alloc_mem_bytes"]["beats"] is True  # gap 210 > spread 40
    assert verdict["metrics"]["p99_s"]["beats"] is False


def test_h1_is_rejected_when_the_gain_is_inside_the_spread() -> None:
    default = reps([1000, 1300, 1020], [0.50, 0.52, 0.51])
    tuned = reps([800, 830, 810], [0.50, 0.52, 0.51])
    assert h1(default, tuned)["supported"] is False  # gap 210 < default spread 300


def test_repetitions_are_reported_as_median_with_range() -> None:
    agg = aggregate([{"p99_s": 0.5}, {"p99_s": 0.3}, {"p99_s": 0.9}])
    assert agg["p99_s"] == {"median": 0.5, "min": 0.3, "max": 0.9}


def test_validation_replay_yields_measured_peaks(tmp_path: Path) -> None:
    d = write_run(
        tmp_path,
        client=[(0.0, 1.0, "ok"), (0.5, 1.0, "ok"), (0.9, 1.0, "ok"), (1.0, 0.2, "ok")],
        sampler=[(float(i), 3.0 if 20 <= i < 35 else 1.0, 800.0 if i == 40 else 100.0, 0, 0, 0.0) for i in range(60)],
    )
    peaks = measured_peaks(load_run(d))
    assert peaks.concurrency == 3  # the first request completes as the fourth is sent
    assert peaks.memory_bytes == 800 * MI


def test_a_static_run_with_an_oom_kill_flags_the_sizing(tmp_path: Path) -> None:
    assert static_sizing_too_low(load_run(scenario(tmp_path / "a"))) == "1 OOM kill"
    busy = write_run(
        tmp_path / "b",
        client=[(0.0, 0.1, "ok")],
        sampler=[(float(i), 1.0, 100.0, 0, 0, 0.0) for i in range(30)],
        pods=[pod(0.0, res(1000), res(1000))],
    )
    assert static_sizing_too_low(load_run(busy)) is None  # CPU at its fixed value says nothing about the memory sizing


def test_gate_passes_when_memory_moved_both_ways_cpu_never_moved_and_every_lag_is_complete(tmp_path: Path) -> None:
    checks = gate_checks(load_run(scenario(tmp_path)))
    assert [c.passed for c in checks] == [True, True, True]


def test_gate_fails_if_vpa_ever_changes_the_cpu(tmp_path: Path) -> None:
    start, moved = res(1000), res(2000, cpu_m=500)
    d = write_run(
        tmp_path,
        client=[(1.0, 0.1, "ok")],
        pods=[pod(0.0, start, start), pod(22.0, moved, moved)],
        audit=[audit_rec(10.0, moved), audit_resize(20.0, moved)],
    )
    check = gate_checks(load_run(d))[1]
    assert not check.passed and "500" in check.detail


def test_gate_fails_if_only_the_cpu_limit_in_the_pod_spec_changes(tmp_path: Path) -> None:
    start = res(1000)
    changed = pod(22.0, start, start)
    changed["object"]["spec"]["containers"][0]["resources"]["limits"] = res(1000, cpu_m=750)
    d = write_run(tmp_path, client=[(1.0, 0.1, "ok")], pods=[pod(0.0, start, start), changed])
    check = gate_checks(load_run(d))[1]
    assert not check.passed and "750" in check.detail


def _run_with_unapplied_resize(d: Path, conditions: list[dict[str, str]], recommended: bool = True) -> Path:
    """One applied memory increase, then a decrease that never shows up as applied."""
    start, big, low = res(1000), res(2000), res(500)
    return write_run(
        d,
        client=[(1.0, 0.1, "ok")],
        pods=[pod(0.0, start, start), pod(22.0, big, big), pod(60.1, low, big, conditions)],
        audit=([audit_rec(10.0, big)] if recommended else [])
        + [audit_resize(20.0, big), audit_rec(58.0, low), audit_resize(60.0, low)],
    )


REFUSED = [{"type": "PodResizeInProgress", "status": "True", "reason": "Error"}]


def test_a_resize_the_kubelet_refused_does_not_fail_the_lag_check(tmp_path: Path) -> None:
    check = gate_checks(load_run(_run_with_unapplied_resize(tmp_path, REFUSED)))[2]
    assert check.passed and "2 of 2" in check.detail and "Error" in check.detail


def test_a_refused_resize_still_needs_its_recommendation_timestamp(tmp_path: Path) -> None:
    start, low = res(1000), res(500)
    d = write_run(  # the only recommendation write in the audit log comes after the patch
        tmp_path,
        client=[(1.0, 0.1, "ok")],
        pods=[pod(0.0, start, start), pod(60.1, low, start, REFUSED)],
        audit=[audit_resize(60.0, low), audit_rec(70.0, low)],
    )
    check = gate_checks(load_run(d))[2]
    assert not check.passed and "0 of 1" in check.detail


def test_a_resize_that_silently_never_applies_still_fails_the_lag_check(tmp_path: Path) -> None:
    check = gate_checks(load_run(_run_with_unapplied_resize(tmp_path, [])))[2]
    assert not check.passed and "1 of 2" in check.detail


def test_gate_fails_without_any_resize(tmp_path: Path) -> None:
    d = write_run(tmp_path, client=[(0.0, 0.1, "ok")])
    assert [c.passed for c in gate_checks(load_run(d))] == [False, True, False]  # CPU untouched; no memory resize, no lag


def poll(t: float, stamp_s: int, window: str = "15.004s") -> dict[str, object]:
    return {"t": t, "timestamp": f"2026-11-16T10:{stamp_s // 60:02d}:{stamp_s % 60:02d}Z", "window": window}


def test_metrics_api_refreshing_every_15s_passes() -> None:
    polls = [poll(float(t), 15 * (t // 15)) for t in range(0, 120, 2)]
    assert metrics_refresh_check(polls).passed


def test_metrics_api_refreshing_every_60s_fails() -> None:
    polls = [poll(float(t), 60 * (t // 60), "60.1s") for t in range(0, 600, 2)]
    assert not metrics_refresh_check(polls).passed
