"""Rules fixed before the runs: tuned-configuration selection, H1, and the prototype gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from vpaexp.metrics import Summary, failed_rate, summarize
from vpaexp.records import Run

MEMORY_TIE = 0.05  # iterations within 5% of the lowest mean memory count as equal
METRICS_REFRESH_S = 15.0  # gate check 4


@dataclass(frozen=True)
class Candidate:
    """One tuning iteration's whole-run result."""

    name: str
    alloc_mem_bytes: float
    p99_s: float
    failed_rate: float  # timeouts and errors as a share of requests sent
    oom_kills: float


@dataclass(frozen=True)
class Selection:
    """The outcome of the selection rule, with the static bar it was judged against."""

    chosen: Candidate
    quality_kept: bool  # False: no iteration kept service quality, and this must be reported
    static_p99_s: float
    static_failed_rate: float
    candidates: list[Candidate]


def select_tuned(iterations: dict[str, Summary], static: list[Summary]) -> Selection:
    """Section 7 selection rule: the least memory that keeps service quality.

    `iterations` maps each tuning iteration's name to its whole-run summary; `static` holds
    the whole-run summary of every static repetition available.

    Service quality is kept when neither p99 nor the failed-request rate is worse than static.
    Static is the worst of its repetitions on each, so that static's own run-to-run spread
    does not reject an iteration. Among the iterations that keep service quality, the lowest
    mean allocated memory wins; iterations within 5% of the lowest count as equal on memory,
    and the one with the fewest OOM kills is taken. An iteration's OOM kills include kills of
    the whole container. If no iteration keeps service quality, the one with the fewest
    failed requests is chosen.
    """
    static_p99_s = max(s["p99_s"] for s in static)
    static_failed_rate = max(failed_rate(s) for s in static)
    candidates = [
        Candidate(name, s["alloc_mem_bytes"], s["p99_s"], failed_rate(s), s["oom_kills"])
        for name, s in iterations.items()
    ]
    eligible = [c for c in candidates if c.p99_s <= static_p99_s and c.failed_rate <= static_failed_rate]
    if eligible:
        lowest = min(c.alloc_mem_bytes for c in eligible)
        close = [c for c in eligible if c.alloc_mem_bytes <= (1 + MEMORY_TIE) * lowest]
        chosen = min(close, key=lambda c: (c.oom_kills, c.alloc_mem_bytes))
    else:
        chosen = min(candidates, key=lambda c: (c.failed_rate, c.p99_s))
    return Selection(chosen, bool(eligible), static_p99_s, static_failed_rate, candidates)


H1_METRICS = ("alloc_mem_bytes", "p99_s")


def h1(default: list[Summary], tuned: list[Summary]) -> dict[str, Any]:
    """H1: tuned beats default on mean memory allocation or p99 by more than the spread of the repetitions.

    A metric is beaten when the median improves by more than the larger of the two
    min-to-max spreads. H1 is supported if any of the metrics is beaten.
    """
    metrics = {}
    for key in H1_METRICS:
        d, t = [s[key] for s in default], [s[key] for s in tuned]
        gain = float(np.median(d) - np.median(t))
        spread = max(max(d) - min(d), max(t) - min(t))
        metrics[key] = {
            "default_median": float(np.median(d)),
            "tuned_median": float(np.median(t)),
            "gain": gain,
            "spread": spread,
            "beats": bool(gain > spread),
        }
    return {"supported": any(m["beats"] for m in metrics.values()), "metrics": metrics}


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def gate_checks(run: Run) -> list[Check]:
    """Prototype gate checks 1-3 (section 11)."""
    whole = summarize(run)["whole"]
    # A resize the kubelet refused or deferred and never applied has no applied timestamp to
    # capture. That is normal behaviour: it is reported, and needs only the other two.
    attempted = [r for r in run.resizes if r.outcome != "rejected"]
    refused = [r for r in attempted if r.outcome != "applied" and r.issues]
    captured = sum(r.rec_updated_t is not None if r in refused else r.complete for r in attempted)
    cpu_values = {cpu for p in run.pods for cpu in p.cpu_millicores}
    cpu_fixed = not cpu_values - {run.initial.cpu_millicores}
    return [
        Check(
            "1. at least one memory resize up and one down",
            whole["resize_mem_up"] >= 1 and whole["resize_mem_down"] >= 1,
            f"{whole['resize_mem_up']:.0f} up, {whole['resize_mem_down']:.0f} down",
        ),
        Check(
            "2. CPU is never resized",
            cpu_fixed,
            f"CPU request and limit stayed at {run.initial.cpu_millicores}m" if cpu_fixed
            else f"CPU took the values {sorted(cpu_values)}m",
        ),
        Check(
            "3. all three lag timestamps for every resize",
            bool(attempted) and captured == len(attempted),
            f"{captured} of {len(attempted)} resizes have their timestamps"
            + (f"; {len(refused)} of them not applied by the kubelet ({', '.join(sorted({i for r in refused for i in r.issues}))}); "
               "those need no applied timestamp" if refused else ""),
        ),
    ]


def metrics_refresh_check(polls: list[dict[str, Any]]) -> Check:
    """Gate check 4: the metrics API serves a fresh sample every 15s.

    `polls` are repeated reads of the pod's PodMetrics `timestamp` and `window`.
    """
    name = "4. metrics API refreshes every 15s"
    stamps = sorted({datetime.fromisoformat(p["timestamp"]).timestamp() for p in polls if p.get("timestamp")})
    if len(stamps) < 5:
        return Check(name, False, f"only {len(stamps)} distinct samples seen")
    gap = float(np.median(np.diff(stamps)))
    windows = sorted({str(p.get("window")) for p in polls})
    ok = abs(gap - METRICS_REFRESH_S) <= 0.2 * METRICS_REFRESH_S
    return Check(name, ok, f"median refresh {gap:.1f}s over {len(stamps)} samples; windows {windows[:5]}")
