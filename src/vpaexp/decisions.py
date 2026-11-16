"""Rules fixed before the runs: tuned-configuration selection, H1, and the prototype gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from vpaexp.metrics import Summary, summarize
from vpaexp.records import Run

P99_ALLOWANCE = 1.1


@dataclass(frozen=True)
class Candidate:
    """One tuning iteration's whole-run result."""

    name: str
    alloc_mem_bytes: float
    p99_s: float
    oom_kills: float


def select_tuned(candidates: list[Candidate], static_p99_s: float) -> tuple[Candidate, bool]:
    """Section 7 selection rule. Returns the chosen iteration and whether the constraint was met."""
    eligible = [c for c in candidates if c.oom_kills == 0 and c.p99_s <= P99_ALLOWANCE * static_p99_s]
    if not eligible:
        return min(candidates, key=lambda c: c.p99_s), False
    return min(eligible, key=lambda c: c.alloc_mem_bytes), True


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
    s = summarize(run)["whole"]
    # A resize the kubelet refused or deferred and never applied has no applied timestamp to
    # capture; that is normal behaviour, so it is reported but not required to be complete.
    attempted = [r for r in run.resizes if r.outcome != "rejected"]
    refused = [r for r in attempted if r.outcome != "applied" and r.issues]
    required = [r for r in attempted if r not in refused]
    complete = sum(r.complete for r in required)
    cpu_values = {a.cpu_millicores for p in run.pods for a in (p.spec, p.status) if a is not None}
    return [
        Check(
            "1. at least one memory resize up and one down",
            s["resize_mem_up"] >= 1 and s["resize_mem_down"] >= 1,
            f"{s['resize_mem_up']:.0f} up, {s['resize_mem_down']:.0f} down",
        ),
        Check(
            "2. CPU is never resized",
            not cpu_values - {run.initial.cpu_millicores},
            f"CPU stayed at {run.initial.cpu_millicores}m" if not cpu_values - {run.initial.cpu_millicores}
            else f"CPU took the values {sorted(cpu_values)}m",
        ),
        Check(
            "3. all three lag timestamps for every resize",
            bool(required) and complete == len(required),
            f"{complete} of {len(required)} resizes complete"
            + (f"; {len(refused)} more not applied by the kubelet ({', '.join(sorted({i for r in refused for i in r.issues}))})"
               if refused else ""),
        ),
    ]


def metrics_refresh_check(polls: list[dict[str, Any]], expected_s: float = 15.0) -> Check:
    """Gate check 4: the metrics API serves a fresh sample every 15s.

    `polls` are repeated reads of the pod's PodMetrics `timestamp` and `window`.
    """
    name = "4. metrics API refreshes every 15s"
    stamps = sorted({datetime.fromisoformat(p["timestamp"]).timestamp() for p in polls if p.get("timestamp")})
    if len(stamps) < 5:
        return Check(name, False, f"only {len(stamps)} distinct samples seen")
    gap = float(np.median(np.diff(stamps)))
    windows = sorted({str(p.get("window")) for p in polls})
    ok = abs(gap - expected_s) <= 0.2 * expected_s
    return Check(name, ok, f"median refresh {gap:.1f}s over {len(stamps)} samples; windows {windows[:5]}")
