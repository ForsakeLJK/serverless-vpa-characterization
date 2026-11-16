"""Per-run metrics, whole-run and per phase (spec section 8.1)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Final

import numpy as np

from vpaexp.records import Run
from vpaexp.sizing import PayloadCost, Peaks
from vpaexp.trace import Floats

Summary = dict[str, float]
PROTOTYPE: Final = "prototype"  # the one setting exempt from the CPU validity check (section 11)


@dataclass(frozen=True)
class Restart:
    """One restart of the function container."""

    exited_t: float  # the kubelet's finishedAt; the time the restart was first seen if that is missing
    oom: bool  # the container was OOM-killed


def phases(run: Run) -> dict[str, tuple[float, float]]:
    """Phase name to (start, end) in seconds from replay start."""
    out = {"whole": (0.0, run.duration_s)}
    boundary = run.meta.get("phase_boundary_s")
    if boundary is not None and boundary < run.duration_s:
        out["first_day"] = (0.0, float(boundary))
        out["warmed"] = (float(boundary), run.duration_s)
    return out


def allocation_steps(run: Run) -> tuple[Floats, Floats]:
    """The memory allocation in force over time, as step-change times and bytes."""
    times, mem = [-np.inf], [run.initial.memory_bytes]
    for p in run.pods:
        current = p.status or p.spec
        if current is not None and current.memory_bytes != mem[-1]:
            times.append(p.t)
            mem.append(current.memory_bytes)
    return np.array(times), np.array(mem, dtype=np.float64)


def _step_at(times: Floats, values: Floats, t: Floats) -> Floats:
    return np.asarray(values[np.searchsorted(times, t, side="right") - 1], dtype=np.float64)


def _step_mean(times: Floats, values: Floats, a: float, b: float) -> float:
    edges = np.clip(np.append(times, np.inf), a, b)
    return float(np.sum(values * np.diff(edges)) / (b - a))


def memory_utilization(run: Run) -> Floats:
    """Memory usage as a fraction of the allocation in force, for every 1s sample."""
    s = run.samples
    times, mem = allocation_steps(run)
    return np.asarray(s.memory_bytes / _step_at(times, mem, s.t - 0.5), dtype=np.float64)  # a sample covers the second before it


def latencies(run: Run) -> tuple[Floats, Floats]:
    """(scheduled time, latency) of every request that is not an error; timeouts count at the timeout value."""
    c = run.client
    keep = c.status != "error"
    latency = np.where(c.status == "timeout", run.timeout_s, c.completed - c.scheduled)
    return c.scheduled[keep], latency[keep]


def container_restarts(run: Run) -> list[Restart]:
    """Every restart of the function container, from the restart count the pod reports.

    A restart is timed by the kubelet's finishedAt of the instance that ended. The restart
    count only goes up once the container is started again, which is later by the back-off.
    """
    out: list[Restart] = []
    seen = 0
    for p in run.pods:
        if p.restarts > seen:
            out += [Restart(p.last_exit_t or p.t, p.last_exit == "OOMKilled")] * (p.restarts - seen)
            seen = p.restarts
    return out


def oom_kill_times(run: Run) -> Floats:
    """When each OOM kill happened: kills inside the container, and OOM kills of the container itself.

    An OOM that kills the whole container takes its cgroup, and the cgroup's counter, with it,
    so those kills are taken from the restarts the pod reports. Every count, per-phase split
    and figure mark of OOM kills comes from here.
    """
    s = run.samples
    inside: list[float] = np.repeat(s.t, s.oom_kills.astype(int)).tolist()
    return np.array(sorted(inside + [r.exited_t for r in container_restarts(run) if r.oom]), dtype=np.float64)


def failed_rate(summary: Summary) -> float:
    """Failed requests (timeouts plus errors) as a share of requests sent (section 7)."""
    return summary["timeout_rate"] + summary["error_rate"]


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

    times, mem = allocation_steps(run)
    out["alloc_mem_bytes"] = _step_mean(times, mem, a, b)

    m = (s.t > a) & (s.t <= b)
    out["usage_mem_bytes"] = _mean(s.memory_bytes[m])
    out["util_mem"] = _mean(memory_utilization(run)[m])
    # Not a result: CPU is fixed and generous in every setting (section 6). Any throttling
    # means CPU was a constraint after all, and the run is invalid.
    out["cpu_throttled_s"] = float(np.sum(s.throttled_s[m]))

    # Like a sample, a kill or restart at the phase boundary belongs to the phase that ends there.
    kills = oom_kill_times(run)
    out["oom_kills"] = float(np.sum((kills > a) & (kills <= b)))
    out["container_restarts"] = float(sum(a < r.exited_t <= b for r in container_restarts(run)))

    patched = [r for r in run.resizes if a <= r.patched_t < b]
    applied = [r for r in patched if r.outcome == "applied"]
    out["resizes"] = len(applied)
    out["resize_mem_up"] = sum(r.after.memory_bytes > r.before.memory_bytes for r in applied)
    out["resize_mem_down"] = sum(r.after.memory_bytes < r.before.memory_bytes for r in applied)
    # Reported as deferred, infeasible or errored. A resize that is merely not applied yet when
    # the next one replaces it, or when the run ends, was not reported as any of these.
    out["resizes_failed_or_deferred"] = sum(bool(r.issues) for r in patched)
    return out


def _mean(x: Floats) -> float:
    return float(np.mean(x)) if len(x) else float("nan")


def measured_peaks(run: Run) -> Peaks:
    """What a generously sized replay showed, for comparison with the offline calculation."""
    c = run.client
    events = np.concatenate([c.sent, c.completed])
    delta = np.concatenate([np.ones(len(c.sent)), -np.ones(len(c.completed))])
    order = np.lexsort((delta, events))  # at equal times, completions first
    concurrency = int(np.max(np.cumsum(delta[order]))) if len(events) else 0
    s = run.samples
    memory = float(max(np.max(s.memory_bytes), np.max(s.memory_peak_bytes))) if len(s.t) else float("nan")
    return Peaks(concurrency, memory)


def payload_cost(run: Run, lead_in_s: float) -> PayloadCost:
    """Section 13: the payload's cost, from a run of spaced single requests after an idle lead-in."""
    c, s = run.client, run.samples
    baseline = float(np.median(s.memory_bytes[s.t < run.t0 + lead_in_s - 1]))
    return PayloadCost(
        service_s=float(np.median(c.completed - c.sent)),
        request_mem_bytes=float(np.max(s.memory_peak_bytes)) - baseline,
        baseline_mem_bytes=baseline,
    )


def static_sizing_too_low(run: Run) -> str | None:
    """Section 6.1 campaign check: a static run must show no OOM kill."""
    kills = len(oom_kill_times(run))
    return f"{kills} OOM kill{'s' if kills > 1 else ''}" if kills else None


def cpu_was_limiting(run: Run) -> str | None:
    """Whether the fixed CPU throttled the function at any point, and for how long."""
    throttled = float(np.sum(run.samples.throttled_s))
    if throttled <= 0:
        return None
    return f"CPU was throttled for {throttled:.1f}s ({100 * throttled / run.duration_s:.1f}% of the run)"


def invalid_reason(run: Run) -> str | None:
    """Section 6: any CPU throttling makes a run invalid. None means the run is valid.

    The prototype run is exempt (section 11): its testbed cannot give generous CPU, and it
    only verifies the pipeline.
    """
    return None if run.setting == PROTOTYPE else cpu_was_limiting(run)


def aggregate(summaries: list[Summary]) -> dict[str, dict[str, float]]:
    """Median with min-to-max range over repetitions, per metric."""
    out = {}
    for key in summaries[0]:
        values = [s[key] for s in summaries]
        out[key] = {"median": float(np.median(values)), "min": min(values), "max": max(values)}
    return out


def as_json(run: Run) -> dict[str, Any]:
    reason = invalid_reason(run)
    return {
        "setting": run.setting,
        "window": run.window,
        "rep": run.meta.get("rep"),
        "valid": reason is None,
        "invalid_reason": reason,
        "phases": summarize(run),
        "resizes": [
            {
                "patched_t": r.patched_t,
                "rec_updated_t": r.rec_updated_t,
                "applied_t": r.applied_t,
                "before": asdict(r.before),
                "after": asdict(r.after),
                "issues": list(r.issues),
                "outcome": r.outcome,
            }
            for r in run.resizes
        ],
    }
