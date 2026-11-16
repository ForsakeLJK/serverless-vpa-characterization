import numpy as np
import pytest

from vpaexp.sizing import Peaks, ToleranceExceeded, offline_demand, static_sizing

MB = 1024 * 1024


def test_overlapping_requests_set_peak_concurrency_and_memory() -> None:
    d = offline_demand(np.array([0.0, 0.1, 0.2, 5.0]), service_s=0.33, request_mem=115 * MB, baseline_mem=33 * MB)
    assert d.peaks.concurrency == 3
    assert d.concurrency_at_s == pytest.approx(0.2)
    assert d.peaks.memory_bytes == (33 + 3 * 115) * MB
    assert d.memory_at_s == pytest.approx(0.2)


def test_a_request_ending_as_another_starts_does_not_overlap() -> None:
    d = offline_demand(np.array([0.0, 1.0]), service_s=1.0, request_mem=MB, baseline_mem=0)
    assert d.peaks.concurrency == 1


def test_cpu_is_core_seconds_of_work_over_the_15s_window() -> None:
    d = offline_demand(np.array([0.0, 0.1, 0.2]), service_s=0.33, request_mem=MB, baseline_mem=0)
    assert d.peaks.cpu_cores == pytest.approx(0.99 / 15)


def test_cpu_peak_uses_a_sliding_window_not_aligned_buckets() -> None:
    # 15 core-seconds at t=10..11 and again at t=20..21: aligned buckets [0,15) and [15,30)
    # each see 1 core, but the window [10,25] sees both bursts.
    schedule = np.array([10.0] * 15 + [20.0] * 15)
    d = offline_demand(schedule, service_s=1.0, request_mem=MB, baseline_mem=0)
    assert d.peaks.cpu_cores == pytest.approx(2.0)
    assert 6.0 <= d.cpu_at_s <= 10.0  # start of a window covering both bursts


def test_static_allocation_adds_15_percent_and_caps_concurrency_at_4x_peak() -> None:
    s = static_sizing(Peaks(concurrency=10, cpu_cores=2.0, memory_bytes=400 * MB))
    assert s.cpu_millicores == 2300
    assert s.memory_bytes == 460 * MB
    assert s.concurrency_cap == 40
    assert s.peak_concurrency == 10


def test_the_larger_of_calculation_and_validation_is_used() -> None:
    calc = Peaks(concurrency=10, cpu_cores=2.0, memory_bytes=400 * MB)
    measured = Peaks(concurrency=11, cpu_cores=1.9, memory_bytes=420 * MB)
    s = static_sizing(calc, calc, measured)
    assert s.cpu_millicores == 2300
    assert s.memory_bytes == 483 * MB
    assert s.concurrency_cap == 44


def test_disagreement_beyond_10_percent_demands_a_profiling_replay() -> None:
    calc = Peaks(concurrency=10, cpu_cores=2.0, memory_bytes=400 * MB)
    measured = Peaks(concurrency=10, cpu_cores=2.0, memory_bytes=445 * MB)
    with pytest.raises(ToleranceExceeded, match="memory"):
        static_sizing(calc, calc, measured)


def test_allocation_rounds_up_to_whole_millicores_and_mebibytes() -> None:
    s = static_sizing(Peaks(concurrency=1, cpu_cores=0.0661, memory_bytes=100 * MB + 1))
    assert s.cpu_millicores == 77  # 76.015 rounded up
    assert s.memory_bytes == 116 * MB  # 115.000001 MiB rounded up


def test_validation_slice_that_misses_the_window_peak_scales_the_calculated_peak() -> None:
    # The 1h validation replay cannot contain every peak of a 24h window. What it measures
    # is how far reality exceeds the calculation; that excess is applied to the window's peak.
    window = Peaks(concurrency=10, cpu_cores=2.0, memory_bytes=400 * MB)
    slice_calc = Peaks(concurrency=5, cpu_cores=1.0, memory_bytes=200 * MB)
    slice_measured = Peaks(concurrency=5, cpu_cores=1.05, memory_bytes=190 * MB)
    s = static_sizing(window, slice_calc, slice_measured)
    assert s.cpu_millicores == 2415  # 2.0 * 1.05 * 1.15
    assert s.memory_bytes == 460 * MB  # measured below calculated: calculation stands
    assert s.peak_concurrency == 10
