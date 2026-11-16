"""Reads a run's results directory into typed records (spec section 8).

Files: run.json (configuration), client.csv (load generator), sampler.csv (cgroup sampler),
watch.jsonl (watch recorder), audit.jsonl (API server audit log extract).
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from vpaexp.config import APP, Allocation
from vpaexp.loadgen import ClientLog, read_client_log
from vpaexp.trace import Floats

_MEM_UNITS = {
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40,
    "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12,
}  # fmt: skip


def parse_cpu(q: str) -> int:
    """Kubernetes CPU quantity to millicores."""
    return round(float(q[:-1])) if q.endswith("m") else round(float(q) * 1000)


def parse_memory(q: str) -> int:
    """Kubernetes memory quantity to bytes."""
    for suffix, factor in _MEM_UNITS.items():
        if q.endswith(suffix):
            return round(float(q[: -len(suffix)]) * factor)
    return round(float(q))


def _allocation(quantities: dict[str, str] | None) -> Allocation | None:
    if not quantities or "cpu" not in quantities or "memory" not in quantities:
        return None
    return Allocation(parse_cpu(quantities["cpu"]), parse_memory(quantities["memory"]))


def _container(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    return next((c for c in items or [] if c.get("name", c.get("containerName")) == APP), {})


@dataclass(frozen=True)
class PodState:
    t: float  # recorder clock
    spec: Allocation | None  # requested
    status: Allocation | None  # applied by the kubelet
    conditions: tuple[tuple[str, str], ...]  # (type, reason) of conditions that are True
    restarts: int
    last_exit: str = ""  # why the previous container instance ended, e.g. OOMKilled


@dataclass(frozen=True)
class Recommendation:
    t: float
    target: Allocation
    lower: Allocation
    upper: Allocation


@dataclass(frozen=True)
class Resize:
    """One pod resize patch and what became of it. Times are epoch seconds.

    All three are on the API server's clock (audit log): the recommender's status write, the
    updater's resize patch, and the kubelet's status write showing the new limits. applied_t
    falls back to the watch recorder's clock if the audit log lacks the kubelet's write.
    """

    before: Allocation
    after: Allocation
    patched_t: float
    rec_updated_t: float | None
    applied_t: float | None
    issues: tuple[str, ...]  # Deferred, Infeasible, Error
    outcome: str  # applied, pending, rejected

    @property
    def complete(self) -> bool:
        return self.rec_updated_t is not None and self.applied_t is not None


@dataclass(frozen=True)
class Samples:
    """cgroup samples. Entry i describes the interval ending at t[i]."""

    t: Floats
    cpu_cores: Floats
    memory_bytes: Floats
    memory_peak_bytes: Floats
    oom_kills: Floats
    throttled_periods: Floats
    throttled_s: Floats


@dataclass(frozen=True)
class Run:
    path: Path
    meta: dict[str, Any]
    client: ClientLog
    samples: Samples
    pods: list[PodState]
    recommendations: list[Recommendation]
    resizes: list[Resize]

    @property
    def t0(self) -> float:
        return float(self.meta["replay_start"])

    @property
    def duration_s(self) -> float:
        return float(self.meta["replay_duration_s"])

    @property
    def timeout_s(self) -> float:
        return float(self.meta["timeout_s"])

    @property
    def initial(self) -> Allocation:
        return Allocation(**self.meta["allocation"])


def load_run(path: Path) -> Run:
    meta = json.loads((path / "run.json").read_text())
    watch = _read_jsonl(path / "watch.jsonl")
    pods = [_pod_state(r) for r in watch if r["kind"] == "pod"]
    return Run(
        path=path,
        meta=meta,
        client=read_client_log(path / "client.csv"),
        samples=read_samples(path / "sampler.csv"),
        pods=pods,
        recommendations=_recommendations(watch),
        resizes=derive_resizes(Allocation(**meta["allocation"]), _read_jsonl(path / "audit.jsonl"), pods),
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _increments(counter: Floats) -> Floats:
    """Per-interval growth of a cumulative counter; a drop means the counter restarted from zero."""
    d = np.diff(counter)
    return np.where(d < 0, counter[1:], d)


def read_samples(path: Path) -> Samples:
    with open(path) as f:
        rows = list(csv.DictReader(f))

    def col(name: str) -> Floats:
        return np.array([float(r[name]) for r in rows], dtype=np.float64)

    t = col("t")
    if len(t) < 2:
        e = np.array([], dtype=np.float64)
        return Samples(e, e, e, e, e, e, e)
    return Samples(
        t=t[1:],
        cpu_cores=_increments(col("usage_usec")) / 1e6 / np.diff(t),
        memory_bytes=col("memory_current")[1:],
        memory_peak_bytes=col("memory_peak")[1:],
        oom_kills=_increments(col("oom_kill")),
        throttled_periods=_increments(col("nr_throttled")),
        throttled_s=_increments(col("throttled_usec")) / 1e6,
    )


def _pod_state(record: dict[str, Any]) -> PodState:
    obj = record["object"]
    status = obj.get("status", {})
    cs = _container(status.get("containerStatuses"))
    return PodState(
        t=record["t"],
        spec=_allocation(_container(obj.get("spec", {}).get("containers")).get("resources", {}).get("requests")),
        # Limits are what the kubelet has actuated; requests can show the new value before it has.
        status=_allocation(cs.get("resources", {}).get("limits") or cs.get("resources", {}).get("requests")),
        conditions=tuple(
            (c["type"], c.get("reason", "")) for c in status.get("conditions", []) if c.get("status") == "True"
        ),
        restarts=int(cs.get("restartCount", 0)),
        last_exit=str(cs.get("lastState", {}).get("terminated", {}).get("reason", "")),
    )


def _recommended(obj: dict[str, Any]) -> dict[str, Any]:
    return _container(obj.get("status", {}).get("recommendation", {}).get("containerRecommendations"))


def _recommendations(watch: list[dict[str, Any]]) -> list[Recommendation]:
    out: list[Recommendation] = []
    for r in watch:
        if r["kind"] != "vpa":
            continue
        c = _recommended(r["object"])
        target, lower, upper = (_allocation(c.get(k)) for k in ("target", "lowerBound", "upperBound"))
        if target is None or lower is None or upper is None:
            continue
        if out and (out[-1].target, out[-1].lower, out[-1].upper) == (target, lower, upper):
            continue
        out.append(Recommendation(r["t"], target, lower, upper))
    return out


def _audit_time(event: dict[str, Any]) -> float:
    return datetime.fromisoformat(event["stageTimestamp"]).timestamp()


def _audit_matches(event: dict[str, Any], resource: str, subresource: str) -> bool:
    ref = event.get("objectRef", {})
    # Only the experiment's own objects: the function pod (function-<hash>) and the VPA named function.
    name = str(ref.get("name", ""))
    if not (name == APP or name.startswith(APP + "-")):
        return False
    return (
        event.get("stage") == "ResponseComplete"
        and event.get("verb") in ("patch", "update")
        and ref.get("resource") == resource
        and ref.get("subresource") == subresource
    )


def derive_resizes(initial: Allocation, audit: list[dict[str, Any]], pods: list[PodState]) -> list[Resize]:
    """Pair every pod resize patch with the recommendation behind it and the kubelet applying it."""
    rec_updates: list[tuple[float, Allocation]] = []
    for e in audit:
        if _audit_matches(e, "verticalpodautoscalers", "status") and _ok(e):
            target = _allocation(_recommended(e.get("responseObject", {})).get("target"))
            if target is not None:
                rec_updates.append((_audit_time(e), target))
    rec_updates.sort(key=lambda u: u[0])
    patches = sorted((e for e in audit if _audit_matches(e, "pods", "resize")), key=_audit_time)
    # The kubelet's own status writes: same clock as the patches, and no watch gaps.
    kubelet = sorted(
        (_pod_state({"t": _audit_time(e), "object": e.get("responseObject", {})})
         for e in audit if _audit_matches(e, "pods", "status") and _ok(e)),
        key=lambda p: p.t,
    )

    out: list[Resize] = []
    current, cursor = initial, 0
    for e in patches:
        t = _audit_time(e)
        if not _ok(e):
            out.append(Resize(current, current, t, _recommended_at(rec_updates, t, None), None, ("Error",), "rejected"))
            continue
        spec = e.get("responseObject", {}).get("spec", {})
        after = _allocation(_container(spec.get("containers")).get("resources", {}).get("requests"))
        if after is None or after == current:
            continue
        seen = next((i for i in range(cursor, len(pods)) if pods[i].spec == after), None)
        if seen is not None:
            cursor = seen
        watched = pods[seen:] if seen is not None else []
        applied_t, issues = _outcome([p for p in kubelet if p.t >= t], after)
        watch_applied_t, watch_issues = _outcome(watched, after)
        if applied_t is None:
            applied_t = watch_applied_t
        issues += [i for i in watch_issues if i not in issues]
        out.append(
            Resize(
                before=current,
                after=after,
                patched_t=t,
                rec_updated_t=_recommended_at(rec_updates, t, after),
                applied_t=applied_t,
                issues=tuple(issues),
                outcome="applied" if applied_t is not None else "pending",
            )
        )
        current = after
    return out


def _outcome(states: list[PodState], after: Allocation) -> tuple[float | None, list[str]]:
    """When the pod first shows `after` as applied, and the resize problems reported before then."""
    issues: list[str] = []
    for p in states:
        if p.spec != after:  # the next resize has begun
            break
        resizing = any(kind == "PodResizeInProgress" for kind, _ in p.conditions)
        if p.status == after and not resizing:
            return p.t, issues
        for kind, reason in p.conditions:
            issue = reason if kind == "PodResizePending" else "Error" if reason == "Error" else None
            if kind in ("PodResizePending", "PodResizeInProgress") and issue and issue not in issues:
                issues.append(issue)
    return None, issues


def _ok(event: dict[str, Any]) -> bool:
    return 200 <= int(event.get("responseStatus", {}).get("code", 0)) < 300


def _recommended_at(updates: list[tuple[float, Allocation]], patched_t: float, value: Allocation | None) -> float | None:
    """When the recommendation behind a patch was written.

    That is the first write in the latest unbroken run of writes carrying the patched value.
    If the last write before the patch carries a different value, its time is used.
    """
    before = [u for u in updates if u[0] <= patched_t]
    if not before:
        return None
    t = before[-1][0]
    if value is not None and before[-1][1] == value:
        for when, target in reversed(before):
            if target != value:
                break
            t = when
    return t
