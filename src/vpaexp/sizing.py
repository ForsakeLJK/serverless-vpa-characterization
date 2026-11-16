"""Static sizing: offline demand calculation and the sizing rule (spec section 6.1)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from vpaexp.trace import Floats

MI = 1024 * 1024
CPU_WINDOW_S = 15.0
HEADROOM = 0.15
TOLERANCE = 0.10
CAP_FACTOR = 4


@dataclass(frozen=True)
class Peaks:
    concurrency: int
    cpu_cores: float  # highest mean CPU usage over any 15s window
    memory_bytes: float


@dataclass(frozen=True)
class Demand:
    """Calculated unthrottled peaks and the replay second at which each occurs."""

    peaks: Peaks
    concurrency_at_s: float
    cpu_at_s: float  # start of the peak 15s window
    memory_at_s: float

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Sizing:
    cpu_millicores: int
    memory_bytes: int
    concurrency_cap: int
    peak_concurrency: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(d: dict[str, Any]) -> Sizing:
        return Sizing(**d)


class ToleranceExceeded(Exception):
    """Calculation and validation replay disagree by more than the tolerance."""


def offline_demand(
    schedule: Floats,
    service_s: float,
    request_mem: float,
    baseline_mem: float,
    cpu_window_s: float = CPU_WINDOW_S,
) -> Demand:
    """Demand of the replay on a pod that never throttles.

    Every request runs for `service_s` on one core and holds `request_mem` while it runs.
    """
    starts = np.sort(schedule)
    ends = starts + service_s

    # Requests are served in arrival order with equal service time, so those in flight
    # at any instant are a contiguous index range [ended, started).
    in_flight = np.arange(1, len(starts) + 1) - np.searchsorted(ends, starts, side="right")
    i = int(np.argmax(in_flight))
    peak_conc = int(in_flight[i])

    prefix = np.concatenate([[0.0], np.cumsum(starts)])

    def work_done(t: Floats) -> Floats:
        """Core-seconds consumed up to time t."""
        ended = np.searchsorted(ends, t, side="right")
        started = np.searchsorted(starts, t, side="right")
        running = t * (started - ended) - (prefix[started] - prefix[ended])
        return np.asarray(service_s * ended + running, dtype=np.float64)

    # The busiest window either begins as a request starts or finishes as one ends.
    window_starts = np.concatenate([starts, ends - cpu_window_s])
    work = work_done(window_starts + cpu_window_s) - work_done(window_starts)
    j = int(np.argmax(work))

    return Demand(
        peaks=Peaks(peak_conc, float(work[j]) / cpu_window_s, baseline_mem + peak_conc * request_mem),
        concurrency_at_s=float(starts[i]),
        cpu_at_s=float(window_starts[j]),
        memory_at_s=float(starts[i]),
    )


def static_sizing(calc: Peaks, slice_calc: Peaks | None = None, slice_measured: Peaks | None = None) -> Sizing:
    """Peak plus 15%, taking the larger of calculation and validation replay.

    `calc` is the calculated peak of the whole window. The validation replay covers a slice
    of it: `slice_calc` is the calculation for that slice and `slice_measured` what the replay
    showed. They must agree within 10%. Where the replay measured more than calculated, the
    window peak is raised by the same ratio, since the slice need not contain every window peak.
    """
    if slice_calc is None or slice_measured is None:
        return sizing_from_peaks(calc)
    raised: dict[str, float] = {}
    for field in ("concurrency", "cpu_cores", "memory_bytes"):
        c, m = getattr(slice_calc, field), getattr(slice_measured, field)
        if abs(m - c) > TOLERANCE * c:
            raise ToleranceExceeded(
                f"{field}: calculated {c:g}, measured {m:g}, differ by more than {TOLERANCE:.0%}; "
                "this window needs a full profiling replay"
            )
        raised[field] = getattr(calc, field) * max(1.0, m / c)
    return sizing_from_peaks(Peaks(_ceil(raised["concurrency"]), raised["cpu_cores"], raised["memory_bytes"]))


def sizing_from_peaks(peaks: Peaks) -> Sizing:
    """The sizing rule alone; used directly after a full profiling replay."""
    return Sizing(
        cpu_millicores=_ceil(peaks.cpu_cores * (1 + HEADROOM) * 1000),
        memory_bytes=_ceil(peaks.memory_bytes * (1 + HEADROOM) / MI) * MI,
        concurrency_cap=CAP_FACTOR * peaks.concurrency,
        peak_concurrency=peaks.concurrency,
    )


def _ceil(x: float) -> int:
    return math.ceil(round(x, 6))  # absorb float noise such as 2300.0000000000005
