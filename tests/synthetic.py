"""Builds a run directory in the formats the collectors write, with hand-chosen contents."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

T0 = 1_000_000.0
MI = 1024 * 1024


def res(cpu_m: int, mem_mi: int) -> dict[str, str]:
    return {"cpu": f"{cpu_m}m", "memory": f"{mem_mi}Mi"}


def pod(t: float, spec: dict[str, str], status: dict[str, str], conditions: list[dict[str, str]] | None = None,
        restarts: int = 0) -> dict[str, Any]:
    return {
        "t": T0 + t,
        "kind": "pod",
        "type": "MODIFIED",
        "object": {
            "metadata": {"name": "function-abc"},
            "spec": {"containers": [{"name": "function", "resources": {"requests": spec, "limits": spec}}]},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}] + (conditions or []),
                "containerStatuses": [
                    {"name": "function", "restartCount": restarts, "resources": {"requests": status, "limits": status}}
                ],
            },
        },
    }


def rec_obj(target: dict[str, str], lower: dict[str, str], upper: dict[str, str]) -> dict[str, Any]:
    return {
        "status": {
            "recommendation": {
                "containerRecommendations": [
                    {"containerName": "function", "target": target, "lowerBound": lower, "upperBound": upper}
                ]
            }
        }
    }


def vpa(t: float, target: dict[str, str], lower: dict[str, str], upper: dict[str, str]) -> dict[str, Any]:
    return {"t": T0 + t, "kind": "vpa", "type": "MODIFIED", "object": rec_obj(target, lower, upper)}


def _stamp(t: float) -> str:
    return datetime.fromtimestamp(T0 + t, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def audit_rec(t: float, target: dict[str, str]) -> dict[str, Any]:
    return {
        "stage": "ResponseComplete",
        "verb": "patch",
        "stageTimestamp": _stamp(t),
        "objectRef": {"resource": "verticalpodautoscalers", "subresource": "status", "name": "function"},
        "responseStatus": {"code": 200},
        "responseObject": rec_obj(target, target, target),
    }


def audit_resize(t: float, requests: dict[str, str], code: int = 200) -> dict[str, Any]:
    return {
        "stage": "ResponseComplete",
        "verb": "patch",
        "stageTimestamp": _stamp(t),
        "objectRef": {"resource": "pods", "subresource": "resize", "name": "function-abc"},
        "responseStatus": {"code": code},
        "responseObject": {"spec": {"containers": [{"name": "function", "resources": {"requests": requests}}]}}
        if code == 200
        else {"kind": "Status"},
    }


def write_run(
    d: Path,
    *,
    duration_s: float = 100.0,
    phase_boundary_s: float | None = 40.0,
    client: list[tuple[float, float, str]],  # (scheduled offset, latency, status)
    sampler: list[tuple[float, float, float, int, int, float]] | None = None,
    # (offset, cpu cores since previous sample, memory Mi, oom_kill total, nr_throttled total, throttled seconds total)
    pods: list[dict[str, Any]] | None = None,
    vpas: list[dict[str, Any]] | None = None,
    audit: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    base = {
        "setting": "default",
        "window": "A",
        "rep": 1,
        "k": 4,
        "replay_start": T0,
        "replay_duration_s": duration_s,
        "phase_boundary_s": phase_boundary_s,
        "timeout_s": 3.3,
        "allocation": {"cpu_millicores": 1000, "memory_bytes": 1000 * MI},
    }
    (d / "run.json").write_text(json.dumps(base | (meta or {})))
    with open(d / "client.csv", "w") as f:
        f.write("scheduled,sent,completed,status\n")
        for off, latency, status in client:
            f.write(f"{T0 + off},{T0 + off},{T0 + off + latency},{status}\n")
    with open(d / "sampler.csv", "w") as f:
        f.write("t,usage_usec,nr_periods,nr_throttled,throttled_usec,memory_current,memory_peak,oom_kill\n")
        usage, prev = 0.0, None
        for off, cores, mem_mi, oom, nthr, thr_s in sampler or []:
            if prev is not None:
                usage += cores * (off - prev) * 1e6
            prev = off
            f.write(f"{T0 + off},{usage:.0f},0,{nthr},{thr_s * 1e6:.0f},{mem_mi * MI:.0f},{mem_mi * MI:.0f},{oom}\n")
    with open(d / "watch.jsonl", "w") as f:
        for r in sorted((pods or []) + (vpas or []), key=lambda r: r["t"]):
            f.write(json.dumps(r) + "\n")
    with open(d / "audit.jsonl", "w") as f:
        for e in audit or []:
            f.write(json.dumps(e) + "\n")
    return d


def scenario(d: Path, **meta: Any) -> Path:
    """One CPU resize up (t=20, applied 22) and one CPU+memory resize down (t=60, deferred, applied 65)."""
    big, small, start = res(2000, 1000), res(500, 500), res(1000, 1000)
    client = [(float(i), 0.2, "ok") for i in range(10)]
    client += [(40.0 + i * 0.5, 0.1, "ok") for i in range(97)]
    client += [(90.0, 3.3, "timeout"), (91.0, 3.3, "timeout"), (92.0, 0.05, "error")]
    sampler = [
        (float(i), 0.5, 500.0, 1 if i >= 50 else 0, 3 if i >= 70 else 0, 0.25 if i >= 70 else 0.0)
        for i in range(101)
    ]
    return write_run(
        d,
        client=client,
        sampler=sampler,
        pods=[
            pod(0.0, start, start),
            pod(20.1, big, start, [{"type": "PodResizeInProgress", "status": "True"}]),
            pod(22.0, big, big),
            pod(60.1, small, big, [{"type": "PodResizePending", "status": "True", "reason": "Deferred"}]),
            pod(65.0, small, small),
        ],
        vpas=[
            vpa(10.0, big, res(900, 900), res(4000, 2000)),
            vpa(58.0, small, res(400, 400), res(700, 700)),
        ],
        audit=[
            audit_rec(5.0, start),
            audit_rec(10.0, big),
            audit_rec(15.0, big),
            audit_resize(20.0, big),
            audit_rec(58.0, small),
            audit_resize(60.0, small),
        ],
        meta=meta,
    )
