"""Arrival times from the Azure Functions 2021 trace, and their replay schedule (spec section 4)."""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

K = 4
DAY_S = 86_400.0
FUNC_PREFIX = "cc5bb2108cc7"

Floats = NDArray[np.float64]


@dataclass(frozen=True)
class Slice:
    """A span of trace time to replay."""

    name: str
    trace_start_s: float
    trace_duration_s: float

    def replay_duration_s(self, k: int = K) -> float:
        return self.trace_duration_s / k


def days(name: str, first_day: int, count: int) -> Slice:
    return Slice(name, first_day * DAY_S, count * DAY_S)


WINDOWS = {"A": days("A", 2, 4), "B": days("B", 9, 4)}


def extract_arrivals(trace_path: Path, func_prefix: str = FUNC_PREFIX) -> Floats:
    """Sorted arrival times (end_timestamp - duration) of the function whose id starts with func_prefix."""
    out: list[float] = []
    needle = "," + func_prefix
    with open(trace_path) as f:
        for line in f:
            if needle not in line:
                continue
            _app, func, end, duration = line.rstrip("\n").split(",")
            if func.startswith(func_prefix):
                out.append(float(end) - float(duration))
    return np.sort(np.array(out, dtype=np.float64))


def save_arrivals(path: Path, arrivals: Floats) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        f.writelines(f"{t!r}\n" for t in arrivals.tolist())


def load_arrivals(path: Path) -> Floats:
    with gzip.open(path, "rt") as f:
        return np.array([float(line) for line in f], dtype=np.float64)


def replay_schedule(arrivals: Floats, s: Slice, k: int = K) -> Floats:
    """Send times in seconds from replay start, for the arrivals inside the slice."""
    lo, hi = np.searchsorted(arrivals, [s.trace_start_s, s.trace_start_s + s.trace_duration_s])
    return (arrivals[lo:hi] - s.trace_start_s) / k


def busiest_slice(arrivals: Floats, within: Slice, length_s: float, name: str) -> Slice:
    """The span of `length_s` trace seconds inside `within` holding the most arrivals.

    Candidate spans start at an arrival, which is where the count can peak.
    """
    end = within.trace_start_s + within.trace_duration_s
    lo, hi = np.searchsorted(arrivals, [within.trace_start_s, end])
    inside = arrivals[lo:hi]
    starts = inside[inside + length_s <= end]
    if len(starts) == 0:
        return Slice(name, within.trace_start_s, length_s)
    counts = np.searchsorted(inside, starts + length_s) - np.searchsorted(inside, starts)
    return Slice(name, float(starts[int(np.argmax(counts))]), length_s)
