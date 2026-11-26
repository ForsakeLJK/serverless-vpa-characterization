"""Window A capacity-contention experiment on the single-node ARM testbed."""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from vpaexp import config, metrics
from vpaexp.cluster import Kubectl, Profile, WatchRecorder, component_versions, extract_audit
from vpaexp.config import MI
from vpaexp.loadgen import replay_async
from vpaexp.records import load_run, parse_cpu, parse_memory
from vpaexp.run import EXPECTED_RESULT, PAYLOAD, TIMEOUT_FACTOR, RunPlan, clean_state, cluster_run_lock, pin_client, write_json

BUDGET = 6656 * MI
CPU_M = 30000
SAMPLER_MEMORY = 128 * MI  # two 64 MiB samplers, or one 128 MiB sampler for isolated controls
NEIGHBOR = "function-neighbor"


def capacity(kc: Kubectl, pods: set[str] | None = None) -> dict[str, Any]:
    """Use kubectl's effective pod request totals, including init containers and overhead."""
    node, = kc.json("get", "nodes", namespaced=False)["items"]
    name = node["metadata"]["name"]
    description = kc.run("describe", "node", name, namespaced=False)
    totals = description.partition("Allocated resources:")[2]
    cpu = re.search(r"^\s*cpu\s+(\S+)\s", totals, re.MULTILINE)
    memory = re.search(r"^\s*memory\s+(\S+)\s", totals, re.MULTILINE)
    if not cpu or not memory:
        raise ValueError("cannot read allocated resource totals from kubectl describe node")
    alloc = node["status"]["allocatable"]
    state = {"node": name, "machine_id": node["status"]["nodeInfo"]["machineID"], "allocatable_memory": parse_memory(alloc["memory"]),
            "allocated_memory": parse_memory(memory[1]), "allocatable_cpu_m": parse_cpu(alloc["cpu"]),
            "allocated_cpu_m": parse_cpu(cpu[1]),
            "memory_pressure": next(c["status"] for c in node["status"]["conditions"] if c["type"] == "MemoryPressure")}
    if pods:
        # kubectl renders effective requests by pod above the aggregate totals.
        rows = description.partition("Non-terminated Pods:")[2].partition("Allocated resources:")[0]
        seen = {}
        for row in rows.splitlines():
            fields = row.split()
            if len(fields) >= 8 and fields[0] == kc.namespace and fields[1] in pods:
                seen[fields[1]] = parse_memory(fields[6])
        if set(seen) != pods:
            raise ValueError("missing function in node resource accounting")
        state["function_requests"] = seen
        state["other_requests"] = state["allocated_memory"] - sum(seen.values())
    return state


def workload_budget(state: dict[str, Any]) -> int:
    """Available just after reset, before creating functions or their samplers."""
    return int(state["allocatable_memory"] - state["allocated_memory"] - SAMPLER_MEMORY)


def reservation_plan(kc: Kubectl) -> dict[str, Any]:
    if kc.json("get", "pods", "-l", "app in (function,function-neighbor,sampler)")["items"]:
        raise ValueError("finish the active experiment and remove its function and sampler pods first")
    state = capacity(kc)
    cfg = json.loads(
        kc.run("get", "--raw", f"/api/v1/nodes/{state['node']}/proxy/configz", namespaced=False))["kubeletconfig"]
    current = parse_memory(cfg.get("systemReserved", {}).get("memory", "0"))
    desired = current + workload_budget(state) - BUDGET
    if desired < 0:
        raise ValueError("existing reservations cannot yield the requested budget")
    return {**state, "sampler_memory": SAMPLER_MEMORY, "effective_budget": workload_budget(state),
            "target_budget": BUDGET, "system_reserved_bytes": current, "desired_system_reserved_bytes": desired}


def remove_workloads(kc: Kubectl, apps: list[str]) -> None:
    for app in apps:
        sampler = "sampler" if app == config.APP else "sampler-neighbor"
        kc.run("delete", "vpa", app, "--ignore-not-found")
        kc.run("delete", "deployment", app, "--ignore-not-found", "--cascade=foreground")
        kc.run("delete", "pod", sampler, "--ignore-not-found")
        if app == NEIGHBOR:
            kc.run("delete", "service", app, "--ignore-not-found")


async def replay_pair(plans: dict[str, RunPlan], urls: dict[str, str], kc: Kubectl, root: Path,
                      pods: set[str] | None = None) -> None:
    loop = asyncio.get_running_loop()
    origin = time.time() + 1, loop.time() + 1
    duration = next(iter(plans.values())).duration_s
    for plan in plans.values():
        path = plan.out_dir / "run.json"
        meta = json.loads(path.read_text())
        write_json(path, meta | {"replay_start": origin[0]})

    async def observe() -> None:
        with (root / "node.jsonl").open("w") as stream:
            while True:
                try:
                    state = await asyncio.to_thread(capacity, kc, pods)
                except (ValueError, RuntimeError) as exc:
                    state = {"collection_error": str(exc)}
                state["t"] = time.time()
                state["mem_available"] = next(line for line in Path("/proc/meminfo").read_text().splitlines()
                                              if line.startswith("MemAvailable:"))
                stream.write(json.dumps(state) + "\n")
                stream.flush()
                left = origin[1] + duration - loop.time()
                if left <= 0:
                    return
                await asyncio.sleep(min(60, left))

    async with asyncio.TaskGroup() as group:
        group.create_task(observe())
        for app, plan in plans.items():
            group.create_task(replay_async(urls[app], plan.schedule, PAYLOAD, TIMEOUT_FACTOR * plan.service_s,
                                          plan.out_dir / "client.csv", EXPECTED_RESULT, origin))


@cluster_run_lock()
def execute_rq4(profile: Profile, plans: dict[str, RunPlan], root: Path, ample: bool) -> None:
    root.mkdir(parents=True, exist_ok=False)
    pin_client(profile.client_numa_node)
    kc = Kubectl(profile.context, profile.namespace)
    # A previous interrupted paired run can have left a second Deployment on the node.
    remove_workloads(kc, [NEIGHBOR])
    clean_state(kc, config.vpa_flags(4))
    before = capacity(kc)
    budget = workload_budget(before)
    if before["allocatable_cpu_m"] - before["allocated_cpu_m"] < len(plans) * (CPU_M + 50):
        raise ValueError("insufficient allocatable CPU for 30 CPU per function plus samplers")
    if (not ample and budget != BUDGET) or (ample and budget <= 7448 * MI):
        raise ValueError(f"effective workload budget is {budget / MI:g} MiB; check rq4-budget and the selected mode")
    startup = sum(p.allocation.memory_bytes for p in plans.values())
    if startup > budget:
        raise ValueError(f"startup requests of {startup / MI:g} MiB exceed available workload memory")
    versions = component_versions(kc)
    urls: dict[str, str] = {}
    pods: dict[str, str] = {}
    samplers: dict[str, str] = {}
    started = time.time()
    base = urlsplit(profile.function_url)
    if base.port != config.NODE_PORT or not base.hostname:
        raise ValueError("RQ4 expects the profile function URL to use the target NodePort 30080")
    write_json(root / "capacity.json", {**before, "effective_budget": budget, "ample_memory": ample})
    with ExitStack() as cleanup:
        cleanup.callback(remove_workloads, kc, list(plans))
        vpas = []
        for index, (app, plan) in enumerate(plans.items()):
            plan.out_dir.mkdir()
            port = config.NODE_PORT + index
            urls[app] = urlunsplit(base._replace(netloc=base.netloc.rsplit(":", 1)[0] + f":{port}"))
            dep = config.deployment_manifest(profile.namespace, profile.image, plan.allocation, plan.concurrency_cap, app)
            kc.apply(dep, config.service_manifest(profile.namespace, app, port))
            kc.run("rollout", "status", f"deployment/{app}", "--timeout=180s")
            pod, = kc.json("get", "pods", "-l", f"app={app}")["items"]
            pods[app] = pod["metadata"]["name"]
            cid = pod["status"]["containerStatuses"][0]["containerID"]
            sampler = samplers[app] = "sampler" if index == 0 else "sampler-neighbor"
            kc.apply(config.sampler_manifest(profile.namespace, profile.image, cid, sampler, 128 // len(plans)))
            kc.run("wait", "--for=condition=Ready", f"pod/{sampler}", "--timeout=120s")
            # Record PID 1's actual CPU affinity and memory placement for the ARM pilot.
            (plan.out_dir / "process-status.txt").write_text(kc.run("exec", pods[app], "--", "cat", "/proc/1/status"))
            (plan.out_dir / "numa-maps.txt").write_text(kc.run("exec", pods[app], "--", "cat", "/proc/1/numa_maps"))
            vpa = config.vpa_manifest(profile.namespace, budget, app) if plan.flags else None
            if vpa:
                vpas.append(vpa)
            write_json(plan.out_dir / "run.json", {
                "setting": plan.setting, "window": "A", "rep": plan.rep, "profile": profile.name, "k": plan.k,
                "replay_start": None, "replay_duration_s": plan.duration_s, "phase_boundary_s": plan.phase_boundary_s,
                "requests_scheduled": len(plan.schedule), "timeout_s": TIMEOUT_FACTOR * plan.service_s,
                "payload": PAYLOAD, "allocation": vars(plan.allocation), "concurrency_cap": plan.concurrency_cap,
                "vpa_flags": vars(plan.flags) if plan.flags else None, "versions": versions,
                "manifests": {"deployment": dep, "vpa": vpa}, "pod": {"name": pods[app], "uid": pod["metadata"]["uid"],
                "container_id": cid}, **(plan.extra or {})})
            watch = WatchRecorder(kc, plan.out_dir / "watch.jsonl", app=app, pod_name=pods[app])
            watch.start()
            cleanup.callback(watch.stop)
        installed = capacity(kc, set(pods.values()))
        if sum(installed["function_requests"].values()) != startup:
            raise ValueError("startup allocations differ from sizing before replay")
        if installed["allocatable_memory"] - installed["other_requests"] != budget:
            raise ValueError("node requests changed during setup; verify capacity before replay")
        if vpas:
            kc.apply(*vpas)
        try:
            asyncio.run(replay_pair(plans, urls, kc, root, set(pods.values())))
        finally:
            for app, plan in plans.items():
                sampler = samplers[app]
                for file, command in [
                    ("sampler.csv", ["exec", sampler, "--", "cat", "/data/sampler.csv"]),
                    ("sampler.log", ["logs", sampler]),
                    ("final-pod.json", ["get", "pod", pods[app], "-o", "json"]),
                    ("final-events.json", ["get", "events", "--field-selector", f"involvedObject.name={pods[app]}", "-o", "json"]),
                    ("final-vpa.json", ["get", "vpa", "--field-selector", f"metadata.name={app}", "-o", "json"]),
                ]:
                    (plan.out_dir / file).write_text(kc.run(*command, check=False))
            audit = root / "audit.jsonl"
            extract_audit(profile.audit_log_cmd, started, audit)
            split_audit(audit, profile.namespace, pods, plans)
    with (root / "node.jsonl").open() as stream:
        snapshots = [json.loads(line) for line in stream]
    measured = [s["allocatable_memory"] - s["other_requests"] for s in snapshots if "other_requests" in s]
    failures = []
    budget_error = not measured or (not ample and any(b != budget for b in measured))
    for plan in plans.values():
        run = load_run(plan.out_dir)
        summary = metrics.as_json(run)
        if not len(run.samples.t) or not run.pods or (plan.flags and not run.recommendations):
            summary.update(valid=False, invalid_reason="incomplete RQ4 collector data")
        if budget_error:
            summary.update(valid=False, invalid_reason="shared workload budget missing or changed during replay")
        write_json(plan.out_dir / "summary.json", summary)
        if not summary["valid"]:
            failures.append(f"{plan.out_dir}: {summary['invalid_reason']}")
    if failures:
        raise ValueError("\n".join(failures))


def split_audit(audit: Path, namespace: str, pods: dict[str, str], plans: dict[str, RunPlan]) -> None:
    """Route exact objects only; the target and neighbor pod names share a prefix."""
    with audit.open() as src, ExitStack() as stack:
        outputs = {}
        for app, plan in plans.items():
            output = stack.enter_context((plan.out_dir / "audit.jsonl").open("w"))
            outputs[("pods", pods[app])] = output
            outputs[("verticalpodautoscalers", app)] = output
        for line in src:
            ref = json.loads(line).get("objectRef", {})
            dest = outputs.get((ref.get("resource"), ref.get("name")))
            if dest is not None and ref.get("namespace") == namespace:
                dest.write(line)
