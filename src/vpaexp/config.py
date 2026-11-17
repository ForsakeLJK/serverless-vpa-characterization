"""Settings: VPA component flags and the Kubernetes objects of a run (spec sections 3, 6, 7)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

MI = 1024 * 1024
APP = "function"
FUNCTION_PORT = 8080
HEALTH_PORT = 8081
NODE_PORT = 30080

# metrics-server --metric-resolution (scripts/install-components.sh). It cannot usefully go lower
# (section 6.2), so it is not scaled, and it is the floor for VPA's loop intervals at any k.
METRICS_RESOLUTION_S = 15
# Polling loops: running them faster than the metrics refresh only re-reads the same sample.
LOOP_INTERVALS = frozenset({"recommender-interval", "updater-interval"})

# Upstream defaults. Durations in seconds; these are divided by k (scaled_duration_s).
_RECOMMENDER_DURATIONS = {
    "cpu-histogram-decay-half-life": 24 * 3600,
    "memory-histogram-decay-half-life": 24 * 3600,
    "memory-aggregation-interval": 24 * 3600,
    "confidence-interval-cpu": 24 * 3600,
    "confidence-interval-memory": 24 * 3600,
    "recommender-interval": 60,
}
_RECOMMENDER_VALUES = {
    "memory-aggregation-interval-count": "8",
    "target-cpu-percentile": "0.9",
    "target-memory-percentile": "0.9",
    "recommendation-margin-fraction": "0.15",
    "pod-recommendation-min-cpu-millicores": "25",
    "pod-recommendation-min-memory-mb": "250",
}
_UPDATER_DURATIONS = {
    "in-recommendation-bounds-eviction-lifetime-threshold": 12 * 3600,
    "evict-after-oom-threshold": 600,
    "updater-interval": 60,
}
_UPDATER_VALUES = {"pod-update-threshold": "0.1"}

# Section 7: the only flags hand-tuning may change. VPA controls memory only, so no CPU flag is a knob.
DECLARED_KNOBS = frozenset(
    {
        "memory-histogram-decay-half-life",
        "memory-aggregation-interval",
        "confidence-interval-memory",
        "target-memory-percentile",
        "recommendation-margin-fraction",
        "pod-recommendation-min-memory-mb",
        "in-recommendation-bounds-eviction-lifetime-threshold",
        "pod-update-threshold",
    }
)

# Section 11: windows far below the k=4 values so that resizes occur within 30 minutes.
# Not subject to the declared-knob rule; the prototype only verifies the pipeline.
PROTOTYPE_FLAGS = {
    "memory-histogram-decay-half-life": "2m0s",
    "memory-aggregation-interval": "2m0s",
    "confidence-interval-memory": "2m0s",
    "in-recommendation-bounds-eviction-lifetime-threshold": "1m0s",
    "evict-after-oom-threshold": "30s",
}


class UndeclaredKnob(ValueError):
    pass


@dataclass(frozen=True)
class VpaFlags:
    recommender: dict[str, str]
    updater: dict[str, str]
    # Scaled flags whose value is not exactly upstream / k: {flag: {exact_s, used_s, reason}}.
    # Empty at k = 4. Written to run.json so a run says how far it departs from the k principle.
    scaling: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class Allocation:
    cpu_millicores: int
    memory_bytes: int

    def resources(self) -> dict[str, str]:
        return {"cpu": f"{self.cpu_millicores}m", "memory": f"{self.memory_bytes // MI}Mi"}


def go_duration(seconds: float) -> str:
    """Format like Go's time.Duration.String(), e.g. 6h0m0s, 2m30s, 15s."""
    if seconds != int(seconds):
        raise ValueError(f"{seconds}s is not a whole number of seconds")
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}h{m}m{s}s"
    return f"{m}m{s}s" if m else f"{s}s"


def scaled_duration_s(flag: str, upstream_s: int, k: int) -> int:
    """An upstream duration divided by k, as the whole seconds a flag is given.

    Two rules, applied in this order:
    - round to the nearest whole second (halves up), and never below 1s;
    - a loop interval is never below the metrics resolution.
    At k = 4 neither rule changes any value.
    """
    if k < 1:
        raise ValueError(f"k must be a positive integer, got {k}")
    seconds = max(1, math.floor(upstream_s / k + 0.5))
    if flag in LOOP_INTERVALS:
        seconds = max(seconds, METRICS_RESOLUTION_S)
    return seconds


def _scaling_note(flag: str, upstream_s: int, k: int) -> dict[str, Any] | None:
    used, exact = scaled_duration_s(flag, upstream_s, k), upstream_s / k
    if used == exact:
        return None
    floored = flag in LOOP_INTERVALS and used == METRICS_RESOLUTION_S and exact < METRICS_RESOLUTION_S
    return {"exact_s": exact, "used_s": used,
            "reason": "floored at the metrics resolution" if floored else "rounded to whole seconds"}


def vpa_flags(k: int, overrides: dict[str, str] | None = None, *, restrict_to_declared: bool = True) -> VpaFlags:
    """Upstream defaults with every time-based setting divided by k (scaled_duration_s), then the overrides."""
    recommender = {f: go_duration(scaled_duration_s(f, v, k)) for f, v in _RECOMMENDER_DURATIONS.items()} | _RECOMMENDER_VALUES
    updater = {f: go_duration(scaled_duration_s(f, v, k)) for f, v in _UPDATER_DURATIONS.items()} | _UPDATER_VALUES
    for knob, value in (overrides or {}).items():
        if restrict_to_declared and knob not in DECLARED_KNOBS:
            raise UndeclaredKnob(f"{knob} is not in the declared knob set (spec section 7)")
        target = recommender if knob in recommender else updater
        if knob not in target:
            raise UndeclaredKnob(f"{knob} is not a known flag")
        target[knob] = value
    scaling = {f: note for f, v in (_RECOMMENDER_DURATIONS | _UPDATER_DURATIONS).items()
               if f not in (overrides or {}) and (note := _scaling_note(f, v, k))}
    return VpaFlags(recommender, updater, scaling)


_BASE_ARGS = ["--v=4", "--stderrthreshold=info"]


def recommender_args(flags: VpaFlags) -> list[str]:
    return _BASE_ARGS + [f"--{f}={v}" for f, v in sorted(flags.recommender.items())]


def updater_args(flags: VpaFlags) -> list[str]:
    return _BASE_ARGS + ["--feature-gates=InPlace=true"] + [f"--{f}={v}" for f, v in sorted(flags.updater.items())]


def deployment_manifest(namespace: str, image: str, allocation: Allocation, concurrency_cap: int) -> dict[str, Any]:
    labels = {"app": APP}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": APP, "namespace": namespace, "labels": labels},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "terminationGracePeriodSeconds": 5,
                    "containers": [
                        {
                            "name": APP,
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [{"containerPort": FUNCTION_PORT}, {"containerPort": HEALTH_PORT}],
                            "env": [{"name": "CONCURRENCY_CAP", "value": str(concurrency_cap)}],
                            "resources": {"requests": allocation.resources(), "limits": allocation.resources()},
                            "resizePolicy": [
                                {"resourceName": "cpu", "restartPolicy": "NotRequired"},
                                {"resourceName": "memory", "restartPolicy": "NotRequired"},
                            ],
                            "readinessProbe": {"httpGet": {"path": "/", "port": HEALTH_PORT}, "periodSeconds": 2},
                        }
                    ],
                },
            },
        },
    }


def service_manifest(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": APP, "namespace": namespace},
        "spec": {
            "type": "NodePort",
            "selector": {"app": APP},
            "ports": [{"port": FUNCTION_PORT, "targetPort": FUNCTION_PORT, "nodePort": NODE_PORT}],
        },
    }


def vpa_manifest(namespace: str, max_memory_bytes: int) -> dict[str, Any]:
    """VPA controls memory only (section 6.4); the pod's CPU stays at its fixed, generous value."""
    return {
        "apiVersion": "autoscaling.k8s.io/v1",
        "kind": "VerticalPodAutoscaler",
        "metadata": {"name": APP, "namespace": namespace},
        "spec": {
            "targetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": APP},
            # minReplicas: the updater skips workloads below its --min-replicas (default 2);
            # the per-object override lets it act on the single function pod.
            "updatePolicy": {"updateMode": "InPlace", "minReplicas": 1},
            "resourcePolicy": {
                "containerPolicies": [
                    {
                        "containerName": APP,
                        "controlledResources": ["memory"],
                        "controlledValues": "RequestsAndLimits",
                        "maxAllowed": {"memory": f"{max_memory_bytes // MI}Mi"},
                    }
                ]
            },
        },
    }


def sampler_manifest(namespace: str, image: str, container_id: str) -> dict[str, Any]:
    """The cgroup sampler: a pod on the node that reads the function container's cgroup files."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "sampler", "namespace": namespace, "labels": {"app": "sampler"}},
        "spec": {
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 2,
            "containers": [
                {
                    "name": "sampler",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["python", "/app/sampler.py", "--cgroup-root", "/host-cgroup",
                                "--container-id", container_id, "--out", "/data/sampler.csv"],
                    "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}},
                    "volumeMounts": [
                        {"name": "cgroup", "mountPath": "/host-cgroup", "readOnly": True},
                        {"name": "data", "mountPath": "/data"},
                    ],
                }
            ],
            "volumes": [
                {"name": "cgroup", "hostPath": {"path": "/sys/fs/cgroup"}},
                {"name": "data", "emptyDir": {}},
            ],
        },
    }
