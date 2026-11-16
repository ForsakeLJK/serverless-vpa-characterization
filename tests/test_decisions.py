from pathlib import Path

import pytest

from vpaexp.config import Allocation
from vpaexp.decisions import Candidate, gate_checks, h1, metrics_refresh_check, select_tuned
from vpaexp.metrics import aggregate, measured_peaks, static_sizing_too_low
from vpaexp.records import load_run

from .synthetic import MI, pod, res, scenario, write_run

STATIC = Allocation(cpu_millicores=3000, memory_bytes=2000 * MI)


def cand(name: str, cpu: float, mem_mi: float, p99: float, ooms: int = 0) -> Candidate:
    return Candidate(name, cpu, mem_mi * MI, p99, ooms)


def test_tuned_is_the_cheapest_iteration_that_keeps_p99_within_1_1x_static_and_has_no_ooms() -> None:
    chosen, met = select_tuned(
        [
            cand("it1", 500, 300, p99=0.50),  # cheapest, but p99 above 1.1 x 0.4
            cand("it2", 600, 300, p99=0.44, ooms=1),  # OOM kill
            cand("it3", 900, 400, p99=0.44),
            cand("it4", 1500, 600, p99=0.40),
        ],
        static_p99_s=0.4,
        static=STATIC,
    )
    assert (chosen.name, met) == ("it3", True)


def test_cpu_and_memory_are_weighed_as_fractions_of_the_static_allocation() -> None:
    # it1 saves 10% of static CPU over it2 but costs 25% more of static memory.
    chosen, _ = select_tuned([cand("it1", 600, 1000, 0.4), cand("it2", 900, 500, 0.4)], 0.4, STATIC)
    assert chosen.name == "it2"


def test_when_no_iteration_meets_the_constraint_the_lowest_p99_wins_and_it_is_reported() -> None:
    chosen, met = select_tuned([cand("it1", 500, 300, 0.9), cand("it2", 800, 300, 0.6, ooms=2)], 0.4, STATIC)
    assert (chosen.name, met) == ("it2", False)


def reps(cpu: list[float], mem: list[float], p99: list[float]) -> list[dict[str, float]]:
    return [{"alloc_cpu_m": c, "alloc_mem_bytes": m, "p99_s": p} for c, m, p in zip(cpu, mem, p99)]


def test_h1_is_supported_when_tuned_beats_default_by_more_than_the_repetition_spread() -> None:
    default = reps([1000, 1040, 1020], [500, 500, 500], [0.50, 0.52, 0.51])
    tuned = reps([800, 830, 810], [500, 500, 500], [0.50, 0.52, 0.51])
    verdict = h1(default, tuned)
    assert verdict["supported"] is True
    assert verdict["metrics"]["alloc_cpu_m"]["beats"] is True  # gap 210 > spread 40
    assert verdict["metrics"]["p99_s"]["beats"] is False


def test_h1_is_rejected_when_the_gain_is_inside_the_spread() -> None:
    default = reps([1000, 1300, 1020], [500, 500, 500], [0.50, 0.52, 0.51])
    tuned = reps([800, 830, 810], [500, 500, 500], [0.50, 0.52, 0.51])
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
    assert peaks.cpu_cores == pytest.approx(3.0)
    assert peaks.memory_bytes == 800 * MI


def test_a_static_run_with_an_oom_kill_or_pinned_cpu_flags_the_sizing(tmp_path: Path) -> None:
    assert static_sizing_too_low(load_run(scenario(tmp_path / "a"))) == ["OOM kill"]
    pinned = write_run(
        tmp_path / "b",
        client=[(0.0, 0.1, "ok")],
        sampler=[(float(i), 1.0, 100.0, 0, 0, 0.0) for i in range(30)],
        pods=[pod(0.0, res(1000, 1000), res(1000, 1000))],
    )
    assert static_sizing_too_low(load_run(pinned)) == ["CPU usage at the limit for a whole 15s window"]


def test_gate_passes_when_cpu_moved_both_ways_memory_moved_and_every_lag_is_complete(tmp_path: Path) -> None:
    checks = gate_checks(load_run(scenario(tmp_path)))
    assert [c.passed for c in checks] == [True, True, True]


def test_gate_fails_without_any_resize(tmp_path: Path) -> None:
    d = write_run(tmp_path, client=[(0.0, 0.1, "ok")])
    assert [c.passed for c in gate_checks(load_run(d))] == [False, False, False]


def poll(t: float, stamp_s: int, window: str = "15.004s") -> dict[str, object]:
    return {"t": t, "timestamp": f"2026-11-16T10:{stamp_s // 60:02d}:{stamp_s % 60:02d}Z", "window": window}


def test_metrics_api_refreshing_every_15s_passes() -> None:
    polls = [poll(float(t), 15 * (t // 15)) for t in range(0, 120, 2)]
    assert metrics_refresh_check(polls).passed


def test_metrics_api_refreshing_every_60s_fails() -> None:
    polls = [poll(float(t), 60 * (t // 60), "60.1s") for t in range(0, 600, 2)]
    assert not metrics_refresh_check(polls).passed
