from pathlib import Path

import numpy as np
import pytest

from vpaexp.trace import (
    WINDOWS,
    Slice,
    busiest_slice,
    extract_arrivals,
    load_arrivals,
    replay_schedule,
    save_arrivals,
)

DATA = Path(__file__).parent.parent / "data" / "arrivals.txt.gz"


def test_arrival_is_end_minus_duration_for_the_chosen_function_only(tmp_path: Path) -> None:
    trace = tmp_path / "trace.txt"
    trace.write_text(
        "app,func,end_timestamp,duration\n"
        "a,cc5bb2108cc7zzz,10.5,0.5\n"
        "a,other,3.0,1.0\n"
        "a,cc5bb2108cc7zzz,4.0,3.0\n"
    )
    arrivals = extract_arrivals(trace)
    assert arrivals.tolist() == [1.0, 10.0]


def test_arrivals_round_trip_through_the_data_file(tmp_path: Path) -> None:
    path = tmp_path / "a.txt.gz"
    save_arrivals(path, np.array([1.25, 7.5]))
    assert load_arrivals(path).tolist() == [1.25, 7.5]


def test_replay_compresses_time_by_k_and_starts_at_the_slice() -> None:
    arrivals = np.array([50.0, 100.0, 140.0, 300.0, 500.0])
    schedule = replay_schedule(arrivals, Slice("s", 100.0, 400.0), k=4)
    assert schedule.tolist() == [0.0, 10.0, 50.0]


def test_busiest_slice_finds_the_densest_span() -> None:
    arrivals = np.array([0.0, 10.0, 20.0, 100.0, 101.0, 102.0, 103.0, 200.0])
    s = busiest_slice(arrivals, Slice("day", 0.0, 300.0), length_s=5.0, name="p")
    assert s.trace_start_s == 100.0 and s.trace_duration_s == 5.0


@pytest.mark.skipif(not DATA.exists(), reason="extracted arrivals not present")
def test_windows_match_the_invocation_counts_in_the_spec() -> None:
    arrivals = load_arrivals(DATA)
    assert len(arrivals) == 142_856
    a = replay_schedule(arrivals, WINDOWS["A"], k=4)
    b = replay_schedule(arrivals, WINDOWS["B"], k=4)
    assert (len(a), len(b)) == (52_092, 57_511)
    assert a.max() < 24 * 3600
    # spec section 4: replayed peak minute, window A = 8.1 rps
    per_min = np.bincount((a // 15).astype(int))  # one trace minute = 15s replayed
    assert per_min.max() == 122
