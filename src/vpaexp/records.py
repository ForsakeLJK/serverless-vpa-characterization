"""Reads a run's results directory into typed records (spec section 8).

Files: run.json (configuration), client.csv (load generator), sampler.csv (cgroup sampler),
watch.jsonl (watch recorder), audit.jsonl (API server audit log extract).
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple

import numpy as np
from numpy.typing import NDArray

from vpaexp.config import APP, Allocation
from vpaexp.trace import Floats

Outcome = Literal["applied", "pending", "rejected"]

# Kubelet event reasons that report a resize problem, and the issue each stands for.
_RESIZE_EVENT_ISSUES = {"ResizeDeferred": "Deferred", "ResizeInfeasible": "Infeasible", "ResizeError": "Error"}

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


def _memory(quantities: dict[str, str] | None) -> int | None:
    return parse_memory(quantities["memory"]) if quantities and "memory" in quantities else None


def _container(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    return next((c for c in items or [] if c.get("name", c.get("containerName")) == APP), {})


def _spec_resources(pod: dict[str, Any]) -> dict[str, Any]:
    """The function container's `resources` in a pod object's spec."""
    return dict(_container(pod.get("spec", {}).get("containers")).get("resources", {}))


@dataclass(frozen=True)
class ClientLog:
    """One row per request (loadgen.HEADER). Times are epoch seconds; status is ok, error or timeout."""

    scheduled: Floats
    sent: Floats
    completed: Floats
    status: NDArray[np.str_]


@dataclass(frozen=True)
class PodState:
    t: float  # recorder clock
    spec: Allocation | None  # requested
    status: Allocation | None  # applied by the kubelet
    conditions: tuple[tuple[str, str], ...]  # (type, reason) of conditions that are True
    restarts: int
    cpu_millicores: frozenset[int] = frozenset()  # every CPU request and limit shown, in spec and in status
    last_exit: str = ""  # why the previous container instance ended, e.g. OOMKilled
    last_exit_t: float | None = None  # when it ended (the kubelet's finishedAt, whole seconds)


@dataclass(frozen=True)
class Recommendation:
    """A VPA memory recommendation, in bytes. VPA controls memory only (section 6.4)."""

    t: float
    target: int
    lower: int
    upper: int


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
    issues: tuple[str, ...]  # Deferred, Infeasible, Error: from pod conditions and kubelet events
    outcome: Outcome

    @property
    def complete(self) -> bool:
        return self.rec_updated_t is not None and self.applied_t is not None


@dataclass(frozen=True)
class Samples:
    """cgroup samples. Entry i describes the interval ending at t[i]."""

    t: Floats
    memory_bytes: Floats
    memory_peak_bytes: Floats
    oom_kills: Floats
    throttled_s: Floats
    cpu_periods: Floats
    cpu_throttled_periods: Floats


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
    def setting(self) -> str:
        return str(self.meta.get("setting"))

    @property
    def window(self) -> str:
        return str(self.meta.get("window"))

    @property
    def label(self) -> str:
        return str(self.meta.get("label", ""))

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
        resizes=derive_resizes(
            Allocation(**meta["allocation"]), _read_jsonl(path / "audit.jsonl"), pods, _resize_events(watch)
        ),
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


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path) as f:
        return list(csv.DictReader(f))


def _column(rows: list[dict[str, str]], name: str) -> Floats:
    return np.array([float(r[name]) for r in rows], dtype=np.float64)


def read_client_log(path: Path) -> ClientLog:
    rows = sorted(_read_csv(path), key=lambda r: float(r["scheduled"]))
    return ClientLog(
        _column(rows, "scheduled"),
        _column(rows, "sent"),
        _column(rows, "completed"),
        np.array([r["status"] for r in rows], dtype=str),
    )


def read_samples(path: Path) -> Samples:
    rows = _read_csv(path)
    t = _column(rows, "t")
    if len(t) < 2:
        e = np.array([], dtype=np.float64)
        return Samples(e, e, e, e, e, e, e)
    return Samples(
        t=t[1:],
        memory_bytes=_column(rows, "memory_current")[1:],
        memory_peak_bytes=_column(rows, "memory_peak")[1:],
        oom_kills=_increments(_column(rows, "oom_kill")),
        throttled_s=_increments(_column(rows, "throttled_usec")) / 1e6,
        cpu_periods=_increments(_column(rows, "nr_periods")),
        cpu_throttled_periods=_increments(_column(rows, "nr_throttled")),
    )


def _pod_state(record: dict[str, Any]) -> PodState:
    obj = record["object"]
    status = obj.get("status", {})
    cs = _container(status.get("containerStatuses"))
    last = cs.get("lastState", {}).get("terminated", {})
    spec, actual = _spec_resources(obj), cs.get("resources", {})
    shown = [r.get(kind) or {} for r in (spec, actual) for kind in ("requests", "limits")]
    return PodState(
        t=record["t"],
        spec=_allocation(spec.get("requests")),
        # Limits are what the kubelet has actuated; requests can show the new value before it has.
        status=_allocation(actual.get("limits") or actual.get("requests")),
        cpu_millicores=frozenset(parse_cpu(q["cpu"]) for q in shown if "cpu" in q),
        conditions=tuple(
            (c["type"], c.get("reason", "")) for c in status.get("conditions", []) if c.get("status") == "True"
        ),
        restarts=int(cs.get("restartCount", 0)),
        last_exit=str(last.get("reason", "")),
        last_exit_t=datetime.fromisoformat(last["finishedAt"]).timestamp() if last.get("finishedAt") else None,
    )


def _recommended(obj: dict[str, Any]) -> dict[str, Any]:
    return _container(obj.get("status", {}).get("recommendation", {}).get("containerRecommendations"))


def _recommendations(watch: list[dict[str, Any]]) -> list[Recommendation]:
    out: list[Recommendation] = []
    for r in watch:
        if r["kind"] != "vpa":
            continue
        c = _recommended(r["object"])
        target, lower, upper = (_memory(c.get(k)) for k in ("target", "lowerBound", "upperBound"))
        if target is None or lower is None or upper is None:
            continue
        if out and (out[-1].target, out[-1].lower, out[-1].upper) == (target, lower, upper):
            continue
        out.append(Recommendation(r["t"], target, lower, upper))
    return out


def audit_time(event: dict[str, Any]) -> float:
    """When the API server finished handling an audit event: the clock of all three lag timestamps."""
    return datetime.fromisoformat(event["stageTimestamp"]).timestamp()


class RecWrite(NamedTuple):
    """A recommender write of the VPA status: when, and the memory target it carried."""

    t: float
    target: int


class ResizeEvent(NamedTuple):
    """A kubelet event reporting a resize problem."""

    t: float  # the event's own timestamp (whole seconds)
    issue: str  # Deferred, Infeasible or Error
    memory_bytes: int | None  # the memory limit of the resize it is about, if the message carries it


def _resize_events(watch: list[dict[str, Any]]) -> list[ResizeEvent]:
    """Kubelet events about a failed or deferred resize of the function pod (section 8.1)."""
    out: list[ResizeEvent] = []
    for r in watch:
        obj = r.get("object") or {}
        issue = _RESIZE_EVENT_ISSUES.get(str(obj.get("reason")))
        involved = obj.get("involvedObject", {})
        if r["kind"] != "event" or issue is None or involved.get("kind") != "Pod":
            continue
        if not str(involved.get("name", "")).startswith(APP + "-"):
            continue
        # The event's own time, so that events left over from an earlier run fall before every patch.
        stamp = obj.get("firstTimestamp") or obj.get("eventTime")
        t = datetime.fromisoformat(stamp).timestamp() if stamp else float(r["t"])
        out.append(ResizeEvent(t, issue, _event_memory(str(obj.get("message", "")))))
    return out


def _event_memory(message: str) -> int | None:
    """The memory limit in a kubelet resize event message, which ends in the resize as JSON."""
    try:
        resize = json.loads(message[message.index("{") :])
        return _memory(_container(resize.get("containers")).get("resources", {}).get("limits"))
    except (ValueError, AttributeError):
        return None


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


def derive_resizes(
    initial: Allocation,
    audit: list[dict[str, Any]],
    pods: list[PodState],
    events: list[ResizeEvent] | None = None,
) -> list[Resize]:
    """Pair every pod resize patch with the recommendation behind it and the kubelet applying it.

    Problems the kubelet reported come from the pod's conditions and from its events.
    """
    rec_updates: list[RecWrite] = []
    for e in audit:
        if _audit_matches(e, "verticalpodautoscalers", "status") and _succeeded(e):
            target = _memory(_recommended(e.get("responseObject", {})).get("target"))
            if target is not None:
                rec_updates.append(RecWrite(audit_time(e), target))
    rec_updates.sort(key=lambda u: u.t)
    patches = sorted((e for e in audit if _audit_matches(e, "pods", "resize")), key=audit_time)
    # The kubelet's own status writes: same clock as the patches, and no watch gaps.
    kubelet = sorted(
        (_pod_state({"t": audit_time(e), "object": e.get("responseObject", {})})
         for e in audit if _audit_matches(e, "pods", "status") and _succeeded(e)),
        key=lambda p: p.t,
    )

    out: list[Resize] = []
    current, cursor = initial, 0
    for e in patches:
        t = audit_time(e)
        if not _succeeded(e):
            out.append(Resize(current, current, t, _recommended_at(rec_updates, t, None), None, ("Error",), "rejected"))
            continue
        after = _allocation(_spec_resources(e.get("responseObject", {})).get("requests"))
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
                rec_updated_t=_recommended_at(rec_updates, t, after.memory_bytes),
                applied_t=applied_t,
                issues=tuple(issues),
                outcome="applied" if applied_t is not None else "pending",
            )
        )
        current = after
    return _with_event_issues(out, events or [])


def _with_event_issues(resizes: list[Resize], events: list[ResizeEvent]) -> list[Resize]:
    """Add each kubelet resize event's issue to the resize it is about.

    That is the latest resize patched by the event's time whose new memory is the one in the
    event's message; if the message names no such resize, the latest resize patched by then. Event
    times are whole seconds, so a patch up to a second after the event still precedes it.
    """
    out = list(resizes)
    for event in events:
        patched = [i for i, r in enumerate(out) if r.outcome != "rejected" and r.patched_t <= event.t + 1]
        about = [i for i in patched if out[i].after.memory_bytes == event.memory_bytes] or patched
        if about and event.issue not in out[about[-1]].issues:
            out[about[-1]] = replace(out[about[-1]], issues=(*out[about[-1]].issues, event.issue))
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


def _succeeded(event: dict[str, Any]) -> bool:
    return 200 <= int(event.get("responseStatus", {}).get("code", 0)) < 300


def _recommended_at(updates: list[RecWrite], patched_t: float, value: int | None) -> float | None:
    """When the recommendation behind a patch was written.

    That is the first write in the latest unbroken run of writes carrying the patched value.
    If the last write before the patch carries a different value, its time is used.
    """
    before = [u for u in updates if u.t <= patched_t]
    if not before:
        return None
    t = before[-1].t
    if value is not None and before[-1].target == value:
        for write in reversed(before):
            if write.target != value:
                break
            t = write.t
    return t
