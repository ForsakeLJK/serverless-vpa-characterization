"""One run: clean state, deploy, replay, collect (spec section 9)."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from vpaexp import config
from vpaexp.cluster import (
    Kubectl,
    MetricsApiPoller,
    Profile,
    WatchRecorder,
    component_versions,
    extract_audit,
    max_allowed_memory,
)
from vpaexp.config import Allocation, VpaFlags
from vpaexp.loadgen import run_replay
from vpaexp.metrics import as_json, container_restarts
from vpaexp.records import load_run
from vpaexp.trace import Floats

PAYLOAD = {"size": 100_000, "seed": 42}  # SeBS `large`
EXPECTED_RESULT = 0.00033384438552589015  # SeBS expected pagerank[0] for that input
TIMEOUT_FACTOR = 10
VPA_SYSTEM_NS = "kube-system"

# static, default and tuned are the settings of section 6. prototype is the gate run (section 11);
# generous is a sizing or measuring replay with a generous memory limit and no VPA.
Setting = Literal["static", "default", "tuned", "prototype", "generous"]


@dataclass(frozen=True)
class RunPlan:
    out_dir: Path
    setting: Setting
    slice_name: str
    rep: int
    k: int
    schedule: Floats  # send times, seconds from replay start
    duration_s: float
    allocation: Allocation  # requests = limits. CPU is fixed; memory is initial (without VPA, permanent)
    concurrency_cap: int
    flags: VpaFlags | None  # None: no VPA object
    service_s: float
    phase_boundary_s: float | None = None
    poll_metrics_api: bool = False
    extra: dict[str, Any] | None = None


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def pin_client(numa_node: int | None) -> None:
    """Keep the load generator and collectors on their own NUMA node (full runs)."""
    if numa_node is None:
        return
    cpulist = Path(f"/sys/devices/system/node/node{numa_node}/cpulist").read_text().strip()
    cpus: set[int] = set()
    for part in cpulist.split(","):
        lo, _, hi = part.partition("-")
        cpus.update(range(int(lo), int(hi or lo) + 1))
    os.sched_setaffinity(0, cpus)  # type: ignore[attr-defined,unused-ignore]


def clean_state(kc: Kubectl, flags: VpaFlags | None) -> None:
    """New pod, no VPA object or checkpoint, restarted VPA components: no history carries over."""
    kc.run("create", "namespace", kc.namespace, namespaced=False, check=False)
    kc.run("delete", "verticalpodautoscalers.autoscaling.k8s.io", "--all", "--ignore-not-found")
    kc.run("delete", "verticalpodautoscalercheckpoints.autoscaling.k8s.io", "--all", "--ignore-not-found")
    kc.run("delete", "deployment", config.APP, "--ignore-not-found")
    kc.run("delete", "pod", "sampler", "--ignore-not-found", "--wait=true")
    kc.run("wait", "--for=delete", "pod", "-l", f"app={config.APP}", "--timeout=180s")
    if flags is None:
        return
    for deployment, args in (
        ("vpa-recommender", config.recommender_args(flags)),
        ("vpa-updater", config.updater_args(flags)),
    ):
        patch = [{"op": "add", "path": "/spec/template/spec/containers/0/args", "value": args}]
        kc.run("-n", VPA_SYSTEM_NS, "patch", "deployment", deployment, "--type=json", "-p", json.dumps(patch),
               namespaced=False)
        kc.run("-n", VPA_SYSTEM_NS, "rollout", "restart", f"deployment/{deployment}", namespaced=False)
    for deployment in ("vpa-recommender", "vpa-updater"):
        kc.run("-n", VPA_SYSTEM_NS, "rollout", "status", f"deployment/{deployment}", "--timeout=180s",
               namespaced=False)


def execute(profile: Profile, plan: RunPlan) -> Path:
    out = plan.out_dir
    out.mkdir(parents=True, exist_ok=False)
    pin_client(profile.client_numa_node)
    kc = Kubectl(profile.context, profile.namespace)
    timeout_s = TIMEOUT_FACTOR * plan.service_s

    log(f"{out.name}: resetting cluster state")
    clean_state(kc, plan.flags)

    deployment = config.deployment_manifest(profile.namespace, profile.image, plan.allocation, plan.concurrency_cap)
    vpa = config.vpa_manifest(profile.namespace, max_allowed_memory(kc, profile.system_reserve_memory_bytes)) if plan.flags else None
    kc.apply(deployment, config.service_manifest(profile.namespace))
    kc.run("rollout", "status", f"deployment/{config.APP}", "--timeout=300s")
    (pod,) = kc.json("get", "pods", "-l", f"app={config.APP}")["items"]
    pod_name = pod["metadata"]["name"]
    container_id = pod["status"]["containerStatuses"][0]["containerID"]

    kc.apply(config.sampler_manifest(profile.namespace, profile.image, container_id))
    kc.run("wait", "--for=condition=Ready", "pod/sampler", "--timeout=120s")

    meta: dict[str, Any] = {
        "setting": plan.setting,
        "window": plan.slice_name,
        "rep": plan.rep,
        "k": plan.k,
        "profile": profile.name,
        "replay_start": None,
        "replay_duration_s": plan.duration_s,
        "phase_boundary_s": plan.phase_boundary_s,
        "timeout_s": timeout_s,
        "requests_scheduled": len(plan.schedule),
        "payload": PAYLOAD,
        "allocation": asdict(plan.allocation),
        "concurrency_cap": plan.concurrency_cap,
        "vpa_flags": {"recommender": plan.flags.recommender, "updater": plan.flags.updater} if plan.flags else None,
        "manifests": {"deployment": deployment, "vpa": vpa},
        "pod": {"name": pod_name, "uid": pod["metadata"]["uid"], "container_id": container_id},
        "versions": component_versions(kc),
        **(plan.extra or {}),
    }
    write_json(out / "run.json", meta)

    recorder = WatchRecorder(kc, out / "watch.jsonl")
    recorder.start()
    poller = MetricsApiPoller(kc, pod_name, out / "metrics_api.jsonl") if plan.poll_metrics_api else None
    if poller:
        poller.start()
    time.sleep(2)  # let the watches deliver the initial state
    if vpa:
        kc.apply(vpa)

    log(f"replaying {len(plan.schedule)} requests over {plan.duration_s / 3600:.2f}h")
    started = time.time()
    try:
        meta["replay_start"] = run_replay(
            profile.function_url, plan.schedule, PAYLOAD, timeout_s, out / "client.csv", EXPECTED_RESULT
        )
        # The schedule ends with its last request; keep collecting to the end of the slice.
        time.sleep(max(0.0, meta["replay_start"] + plan.duration_s - time.time()))
    finally:
        meta["replay_start"] = meta["replay_start"] or started
        write_json(out / "run.json", meta)
        log("collecting")
        time.sleep(2)
        if poller:
            poller.stop()
        recorder.stop()
        samples = kc.run("exec", "sampler", "--", "cat", "/data/sampler.csv", check=False)
        (out / "sampler.csv").write_text(samples)
        if samples.count("\n") < 3:
            log("ERROR: the cgroup sampler returned no data; see sampler.log")
        (out / "sampler.log").write_text(kc.run("logs", "sampler", check=False))
        for name, args in (
            ("final-pod.json", ["get", "pods", "-l", f"app={config.APP}", "-o", "json"]),
            ("final-vpa.json", ["get", "verticalpodautoscalers.autoscaling.k8s.io", "-o", "json"]),
            ("final-events.json", ["get", "events", "-o", "json"]),
        ):
            (out / name).write_text(kc.run(*args, check=False))
        n = extract_audit(profile.audit_log_cmd, started - 5, out / "audit.jsonl")
        log(f"{n} audit events extracted")

    run = load_run(out)
    restarts = container_restarts(run)
    other = sum(not r.oom for r in restarts)
    if restarts:
        # OOM kills and the restarts that follow them are expected under VPA (section 14).
        log(f"the function container restarted {len(restarts)} time(s), {len(restarts) - other} after an OOM kill")
    if other:
        log(f"WARNING: {other} restart(s) had a cause other than an OOM kill; see final-pod.json and final-events.json")
    write_json(out / "summary.json", as_json(run))
    return out


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2))
    log(f"wrote {path}")
