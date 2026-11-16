"""Static sizing: offline demand calculation and the sizing rule (spec section 6.1)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from vpaexp.trace import Floats

MI = 1024 * 1024
HEADROOM = 0.15
TOLERANCE = 0.10
CAP_FACTOR = 4


@dataclass(frozen=True)
class Peaks:
    concurrency: int
    memory_bytes: float


@dataclass(frozen=True)
class Demand:
    """Calculated peaks on a pod with ample CPU, and the replay second at which each occurs."""

    peaks: Peaks
    concurrency_at_s: float
    memory_at_s: float

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Sizing:
    """The static memory allocation and the concurrency cap. CPU is fixed by the cluster profile."""

    memory_bytes: int
    concurrency_cap: int
    peak_concurrency: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(d: dict[str, Any]) -> Sizing:
        return Sizing(d["memory_bytes"], d["concurrency_cap"], d["peak_concurrency"])


class ToleranceExceeded(Exception):
    """Calculation and validation replay disagree by more than the tolerance."""


def offline_demand(schedule: Floats, service_s: float, request_mem: float, baseline_mem: float) -> Demand:
    """Demand of the replay on a pod with ample CPU.

    Every request runs for `service_s` and holds `request_mem` while it runs.
    """
    starts = np.sort(schedule)
    ends = starts + service_s

    # Requests are served in arrival order with equal service time, so those in flight
    # at any instant are a contiguous index range [ended, started).
    in_flight = np.arange(1, len(starts) + 1) - np.searchsorted(ends, starts, side="right")
    i = int(np.argmax(in_flight))
    peak_conc = int(in_flight[i])
    return Demand(
        peaks=Peaks(peak_conc, baseline_mem + peak_conc * request_mem),
        concurrency_at_s=float(starts[i]),
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
    for field in ("concurrency", "memory_bytes"):
        c, m = getattr(slice_calc, field), getattr(slice_measured, field)
        if abs(m - c) > TOLERANCE * c:
            raise ToleranceExceeded(
                f"{field}: calculated {c:g}, measured {m:g}, differ by more than {TOLERANCE:.0%}; "
                "this window needs a full profiling replay"
            )
        raised[field] = getattr(calc, field) * max(1.0, m / c)
    return sizing_from_peaks(Peaks(_ceil(raised["concurrency"]), raised["memory_bytes"]))


def sizing_from_peaks(peaks: Peaks) -> Sizing:
    """The sizing rule alone; used directly after a full profiling replay."""
    return Sizing(
        memory_bytes=_ceil(peaks.memory_bytes * (1 + HEADROOM) / MI) * MI,
        concurrency_cap=CAP_FACTOR * peaks.concurrency,
        peak_concurrency=peaks.concurrency,
    )


def _ceil(x: float) -> int:
    return math.ceil(round(x, 6))  # absorb float noise such as 460.0000000000005
