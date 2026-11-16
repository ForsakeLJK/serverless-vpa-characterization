"""Per-run metrics, whole-run and per phase (spec section 8.1)."""

from __future__ import annotations

from typing import Any

import numpy as np

from vpaexp.config import Allocation
from vpaexp.records import Run
from vpaexp.sizing import CPU_WINDOW_S, Peaks
from vpaexp.trace import Floats

Summary = dict[str, float]


def phases(run: Run) -> dict[str, tuple[float, float]]:
    """Phase name to (start, end) in seconds from replay start."""
    out = {"whole": (0.0, run.duration_s)}
    boundary = run.meta.get("phase_boundary_s")
    if boundary is not None and boundary < run.duration_s:
        out["first_day"] = (0.0, float(boundary))
        out["warmed"] = (float(boundary), run.duration_s)
    return out


def allocation_steps(run: Run) -> tuple[Floats, Floats, Floats]:
    """The allocation in force over time, as step-change times with CPU (millicores) and memory (bytes)."""
    times, allocs = [-np.inf], [run.initial]
    for p in run.pods:
        current = p.status or p.spec
        if current is not None and current != allocs[-1]:
            times.append(p.t)
            allocs.append(current)
    return (
        np.array(times),
        np.array([a.cpu_millicores for a in allocs], dtype=np.float64),
        np.array([a.memory_bytes for a in allocs], dtype=np.float64),
    )


def _step_at(times: Floats, values: Floats, t: Floats) -> Floats:
    return np.asarray(values[np.searchsorted(times, t, side="right") - 1], dtype=np.float64)


def _step_mean(times: Floats, values: Floats, a: float, b: float) -> float:
    edges = np.clip(np.append(times, np.inf), a, b)
    return float(np.sum(values * np.diff(edges)) / (b - a))


def latencies(run: Run) -> tuple[Floats, Floats]:
    """(scheduled time, latency) of every request that is not an error; timeouts count at the timeout value."""
    c = run.client
    keep = c.status != "error"
    latency = np.where(c.status == "timeout", run.timeout_s, c.completed - c.scheduled)
    return c.scheduled[keep], latency[keep]


def summarize(run: Run) -> dict[str, Summary]:
    return {name: _summarize(run, run.t0 + a, run.t0 + b) for name, (a, b) in phases(run).items()}


def _summarize(run: Run, a: float, b: float) -> Summary:
    c, s = run.client, run.samples
    out: Summary = {}

    in_phase = (c.scheduled >= a) & (c.scheduled < b)
    status = c.status[in_phase]
    n = len(status)
    when, latency = latencies(run)
    latency = latency[(when >= a) & (when < b)]
    out["requests"] = n
    out["p50_s"] = float(np.percentile(latency, 50)) if len(latency) else float("nan")
    out["p99_s"] = float(np.percentile(latency, 99)) if len(latency) else float("nan")
    out["throughput_rps"] = float(np.sum(status == "ok")) / (b - a)
    out["timeout_rate"] = float(np.sum(status == "timeout")) / n if n else 0.0
    out["error_rate"] = float(np.sum(status == "error")) / n if n else 0.0

    times, cpu_m, mem = allocation_steps(run)
    out["alloc_cpu_m"] = _step_mean(times, cpu_m, a, b)
    out["alloc_mem_bytes"] = _step_mean(times, mem, a, b)

    m = (s.t > a) & (s.t <= b)
    mid = s.t[m] - 0.5
    out["usage_cpu_cores"] = _mean(s.cpu_cores[m])
    out["usage_mem_bytes"] = _mean(s.memory_bytes[m])
    out["util_cpu"] = _mean(s.cpu_cores[m] / (_step_at(times, cpu_m, mid) / 1000))
    out["util_mem"] = _mean(s.memory_bytes[m] / _step_at(times, mem, mid))
    out["oom_kills"] = float(np.sum(s.oom_kills[m]))
    out["throttled_periods"] = float(np.sum(s.throttled_periods[m]))
    out["throttled_s"] = float(np.sum(s.throttled_s[m]))

    # An OOM that kills the whole container takes its cgroup, and the cgroup's counter, with it.
    # Those kills are counted from the restarts the pod reports instead.
    restarts, oom_restarts, seen = 0, 0, max((p.restarts for p in run.pods if p.t < a), default=0)
    for p in run.pods:
        if a <= p.t < b and p.restarts > seen:
            restarts += p.restarts - seen
            oom_restarts += (p.restarts - seen) if p.last_exit == "OOMKilled" else 0
            seen = p.restarts
    out["container_restarts"] = float(restarts)
    out["oom_kills"] += oom_restarts

    here = [r for r in run.resizes if a <= r.patched_t < b]
    applied = [r for r in here if r.outcome == "applied"]
    out["resizes"] = len(applied)
    out["resize_cpu_up"] = sum(r.after.cpu_millicores > r.before.cpu_millicores for r in applied)
    out["resize_cpu_down"] = sum(r.after.cpu_millicores < r.before.cpu_millicores for r in applied)
    out["resize_mem_up"] = sum(r.after.memory_bytes > r.before.memory_bytes for r in applied)
    out["resize_mem_down"] = sum(r.after.memory_bytes < r.before.memory_bytes for r in applied)
    out["resizes_failed_or_deferred"] = sum(bool(r.issues) or r.outcome != "applied" for r in here)
    return out


def _mean(x: Floats) -> float:
    return float(np.mean(x)) if len(x) else float("nan")


def rolling_cpu_peak(run: Run, window_s: float = CPU_WINDOW_S) -> float:
    """Highest mean CPU usage over any window of 15 consecutive 1s samples."""
    w = int(window_s)
    cpu = run.samples.cpu_cores
    if len(cpu) < w:
        return _mean(cpu)
    return float(np.max(np.convolve(cpu, np.ones(w) / w, mode="valid")))


def measured_peaks(run: Run) -> Peaks:
    """What a generously sized replay showed, for comparison with the offline calculation."""
    c = run.client
    events = np.concatenate([c.sent, c.completed])
    delta = np.concatenate([np.ones(len(c.sent)), -np.ones(len(c.completed))])
    order = np.lexsort((delta, events))  # at equal times, completions first
    concurrency = int(np.max(np.cumsum(delta[order]))) if len(events) else 0
    s = run.samples
    memory = float(max(np.max(s.memory_bytes), np.max(s.memory_peak_bytes))) if len(s.t) else float("nan")
    return Peaks(concurrency, rolling_cpu_peak(run), memory)


def static_sizing_too_low(run: Run) -> list[str]:
    """Section 6.1 campaign check: an OOM kill, or CPU at the limit for a whole 15s window."""
    problems = []
    if np.sum(run.samples.oom_kills) > 0:
        problems.append("OOM kill")
    if any(p.restarts for p in run.pods):
        problems.append("container restart")
    if rolling_cpu_peak(run) >= 0.99 * run.initial.cpu_millicores / 1000:
        problems.append("CPU usage at the limit for a whole 15s window")
    return problems


def aggregate(summaries: list[Summary]) -> dict[str, dict[str, float]]:
    """Median with min-to-max range over repetitions, per metric."""
    out = {}
    for key in summaries[0]:
        values = [s[key] for s in summaries]
        out[key] = {"median": float(np.median(values)), "min": min(values), "max": max(values)}
    return out


def as_json(run: Run) -> dict[str, Any]:
    return {
        "setting": run.meta.get("setting"),
        "window": run.meta.get("window"),
        "rep": run.meta.get("rep"),
        "phases": summarize(run),
        "resizes": [
            {
                "patched_t": r.patched_t,
                "rec_updated_t": r.rec_updated_t,
                "applied_t": r.applied_t,
                "before": _alloc_json(r.before),
                "after": _alloc_json(r.after),
                "issues": list(r.issues),
                "outcome": r.outcome,
            }
            for r in run.resizes
        ],
    }


def _alloc_json(a: Allocation) -> dict[str, int]:
    return {"cpu_millicores": a.cpu_millicores, "memory_bytes": a.memory_bytes}
