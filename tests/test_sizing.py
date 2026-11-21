import numpy as np
import pytest

from vpaexp.sizing import PayloadCost, Peaks, ToleranceExceeded, offline_demand, static_sizing, validation_slice
from vpaexp.trace import WINDOWS

MB = 1024 * 1024


def test_overlapping_requests_set_peak_concurrency_and_memory() -> None:
    d = offline_demand(np.array([0.0, 0.1, 0.2, 5.0]), PayloadCost(0.33, 115 * MB, 33 * MB))
    assert d.peaks.concurrency == 3
    assert d.peaks.memory_bytes == (33 + 3 * 115) * MB
    assert d.peak_at_s == pytest.approx(0.2)


def test_a_request_ending_as_another_starts_does_not_overlap() -> None:
    d = offline_demand(np.array([0.0, 1.0]), PayloadCost(1.0, MB, 0))
    assert d.peaks.concurrency == 1


def test_the_validation_hour_is_centred_on_the_peak_and_stays_inside_the_window() -> None:
    window = WINDOWS["A"]  # replays in 24h
    middle = validation_slice(window, peak_at_s=10 * 3600.0)
    assert middle.trace_start_s == window.trace_start_s + 9.5 * 3600 * 4 and middle.replay_duration_s() == 3600
    assert validation_slice(window, peak_at_s=60.0).trace_start_s == window.trace_start_s
    late = validation_slice(window, peak_at_s=24 * 3600.0 - 60)
    assert late.trace_start_s + late.trace_duration_s == window.trace_start_s + window.trace_duration_s


def test_when_the_window_replays_in_an_hour_the_validation_slice_is_the_whole_window() -> None:
    window = WINDOWS["A"]  # replays in 1h at k = 96
    for peak_at_s in (0.0, 1800.0, 3500.0):
        s = validation_slice(window, peak_at_s, k=96)
        assert (s.trace_start_s, s.trace_duration_s) == (window.trace_start_s, window.trace_duration_s)
        assert s.replay_duration_s(96) == 3600


def test_static_memory_adds_15_percent_and_concurrency_is_capped_at_4x_peak() -> None:
    s = static_sizing(Peaks(concurrency=10, memory_bytes=400 * MB))
    assert s.memory_bytes == 460 * MB
    assert s.concurrency_cap == 40
    assert s.peak_concurrency == 10


def test_the_larger_of_calculation_and_validation_is_used() -> None:
    calc = Peaks(concurrency=10, memory_bytes=400 * MB)
    measured = Peaks(concurrency=11, memory_bytes=420 * MB)
    s = static_sizing(calc, calc, measured)
    assert s.memory_bytes == 483 * MB
    assert s.concurrency_cap == 44


def test_disagreement_beyond_10_percent_demands_a_profiling_replay() -> None:
    calc = Peaks(concurrency=10, memory_bytes=400 * MB)
    measured = Peaks(concurrency=10, memory_bytes=445 * MB)
    with pytest.raises(ToleranceExceeded, match="memory"):
        static_sizing(calc, calc, measured)


def test_memory_overprediction_keeps_the_calculated_window_peak_and_headroom() -> None:
    window = Peaks(concurrency=10, memory_bytes=400 * MB)
    predicted = Peaks(concurrency=10, memory_bytes=200 * MB)
    measured = Peaks(concurrency=10, memory_bytes=165 * MB)
    assert static_sizing(window, predicted, measured) == static_sizing(window)


@pytest.mark.parametrize("concurrency", [8, 12])
def test_concurrency_disagreement_still_requires_profiling_in_either_direction(concurrency: int) -> None:
    calc = Peaks(concurrency=10, memory_bytes=400 * MB)
    with pytest.raises(ToleranceExceeded, match="concurrency"):
        static_sizing(calc, calc, Peaks(concurrency=concurrency, memory_bytes=330 * MB))


def test_memory_rounds_up_to_whole_mebibytes() -> None:
    s = static_sizing(Peaks(concurrency=1, memory_bytes=100 * MB + 1))
    assert s.memory_bytes == 116 * MB  # 115.000001 MiB rounded up


def test_validation_slice_that_misses_the_window_peak_scales_the_calculated_peak() -> None:
    # The 1h validation replay cannot contain every peak of a 24h window. What it measures
    # is how far reality exceeds the calculation; that excess is applied to the window's peak.
    window = Peaks(concurrency=10, memory_bytes=400 * MB)
    slice_calc = Peaks(concurrency=5, memory_bytes=200 * MB)
    s = static_sizing(window, slice_calc, Peaks(concurrency=5, memory_bytes=210 * MB))
    assert s.memory_bytes == 483 * MB  # 400 * 1.05 * 1.15
    assert s.peak_concurrency == 10
    below = static_sizing(window, slice_calc, Peaks(concurrency=5, memory_bytes=190 * MB))
    assert below.memory_bytes == 460 * MB  # measured below calculated: the calculation stands


def test_static_sizing_carries_no_cpu_because_cpu_is_fixed_by_the_profile() -> None:
    assert set(static_sizing(Peaks(concurrency=1, memory_bytes=MB)).to_json()) == {"memory_bytes", "concurrency_cap", "peak_concurrency"}
