"""Cluster access through kubectl, and the two collectors that run on the client (spec section 8.2)."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from vpaexp.config import APP, MI, Allocation
from vpaexp.records import audit_time, parse_memory
from vpaexp.sizing import PayloadCost


@dataclass(frozen=True)
class Profile:
    """A testbed: the prototype minikube cluster or the full-run machine (cluster/*.toml)."""

    name: str
    context: str
    function_url: str
    audit_log_cmd: list[str]
    generous: Allocation  # CPU: the fixed CPU of every run. Memory: the limit of sizing replays.
    system_reserve_memory_bytes: int
    payload_cost: PayloadCost  # the unverified estimate; `vpaexp measure` replaces it
    namespace: str
    image: str
    client_numa_node: int | None

    @staticmethod
    def load(path: Path) -> Profile:
        d = tomllib.loads(path.read_text())
        return Profile(
            name=path.stem,
            context=d["context"],
            function_url=d["function_url"],
            audit_log_cmd=d["audit_log_cmd"],
            generous=Allocation(d["generous_cpu_millicores"], d["generous_memory_mib"] * MI),
            system_reserve_memory_bytes=d["system_reserve_memory_mib"] * MI,
            payload_cost=PayloadCost(d["service_s"], d["request_mem_mib"] * MI, d["baseline_mem_mib"] * MI),
            namespace=d.get("namespace", "vpaexp"),
            image=d.get("image", "vpaexp-function:1"),
            client_numa_node=d.get("client_numa_node"),
        )


@dataclass
class Kubectl:
    context: str
    namespace: str

    def base(self, namespaced: bool = True) -> list[str]:
        return ["kubectl", "--context", self.context] + (["-n", self.namespace] if namespaced else [])

    def run(self, *args: str, stdin: str | None = None, namespaced: bool = True, check: bool = True) -> str:
        p = subprocess.run(self.base(namespaced) + list(args), input=stdin, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args)} failed: {p.stderr.strip()}")
        return p.stdout

    def json(self, *args: str, namespaced: bool = True) -> Any:
        return json.loads(self.run(*args, "-o", "json", namespaced=namespaced))

    def apply(self, *manifests: dict[str, Any]) -> None:
        self.run("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": list(manifests)}))


def max_allowed_memory(kc: Kubectl, reserve_bytes: int) -> int:
    """Node allocatable memory minus the fixed system reserve (section 6.4)."""
    (node,) = kc.json("get", "nodes", namespaced=False)["items"]
    return (parse_memory(node["status"]["allocatable"]["memory"]) - reserve_bytes) // MI * MI


def component_versions(kc: Kubectl) -> dict[str, Any]:
    """Versions, images and arguments of everything a run depends on."""
    out: dict[str, Any] = {"kubernetes": json.loads(kc.run("version", "-o", "json", namespaced=False))}
    (node,) = kc.json("get", "nodes", namespaced=False)["items"]
    out["node"] = {"nodeInfo": node["status"]["nodeInfo"], "allocatable": node["status"]["allocatable"]}
    deployments = kc.json("get", "deployments", "-n", "kube-system", namespaced=False)["items"]
    for d in deployments:
        name = d["metadata"]["name"]
        if name.startswith("vpa-") or name == "metrics-server":
            c = d["spec"]["template"]["spec"]["containers"][0]
            out[name] = {"image": c["image"], "args": c.get("args", []), "command": c.get("command", [])}
    return out


@dataclass
class WatchRecorder:
    """Watches the VPA object, the function pod and events; one JSON line per change, stamped on receipt."""

    kc: Kubectl
    out_path: Path
    _stop: threading.Event = field(default_factory=threading.Event)
    _threads: list[threading.Thread] = field(default_factory=list)
    _procs: list[subprocess.Popen[str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _out: IO[str] | None = None

    app: str = APP
    pod_name: str | None = None

    WATCHES = {
        "pod": ["pods", "-l", f"app={APP}"],
        "vpa": ["verticalpodautoscalers.autoscaling.k8s.io"],
        "event": ["events"],
    }

    def start(self) -> None:
        self._out = open(self.out_path, "a")
        watches = self.WATCHES
        if self.pod_name:
            watches = {
                "pod": ["pods", "-l", f"app={self.app}"],
                "vpa": ["verticalpodautoscalers.autoscaling.k8s.io", "--field-selector", f"metadata.name={self.app}"],
                "event": ["events", "--field-selector", f"involvedObject.name={self.pod_name}"],
            }
        for kind, what in watches.items():
            t = threading.Thread(target=self._watch, args=(kind, what), daemon=True)
            t.start()
            self._threads.append(t)

    def _watch(self, kind: str, what: list[str]) -> None:
        cmd = self.kc.base() + ["get", *what, "--watch", "--output-watch-events", "-o", "json"]
        while not self._stop.is_set():  # kubectl ends a watch when the server closes it; start again
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            with self._lock:
                self._procs.append(proc)
            assert proc.stdout is not None
            pending = ""
            for line in proc.stdout:
                try:
                    pending += line
                    event = json.loads(pending)
                except json.JSONDecodeError:
                    continue
                pending = ""
                self._write({"t": time.time(), "kind": kind, "type": event.get("type"), "object": event.get("object")})
            proc.wait()
            self._stop.wait(1.0)

    def _write(self, record: dict[str, Any]) -> None:
        with self._lock:
            if self._out is None or self._out.closed:
                return
            self._out.write(json.dumps(record, separators=(",", ":")) + "\n")
            self._out.flush()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            p.terminate()
        for t in self._threads:
            t.join(timeout=5)
        with self._lock:
            if self._out:
                self._out.close()


@dataclass
class MetricsApiPoller:
    """Polls the pod's PodMetrics timestamp and window (prototype gate check 4)."""

    kc: Kubectl
    pod: str
    out_path: Path
    interval_s: float = 2.0
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def _poll(self) -> None:
        path = f"/apis/metrics.k8s.io/v1beta1/namespaces/{self.kc.namespace}/pods/{self.pod}"
        with open(self.out_path, "a") as out:
            while not self._stop.wait(self.interval_s):
                raw = self.kc.run("get", "--raw", path, namespaced=False, check=False)
                if not raw:
                    continue
                m = json.loads(raw)
                rec = {"t": time.time(), "timestamp": m.get("timestamp"), "window": m.get("window"),
                       "containers": m.get("containers")}
                out.write(json.dumps(rec) + "\n")
                out.flush()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)


def extract_audit(cmd: list[str], since_epoch: float, out_path: Path) -> int:
    """Copy this run's events from the API server audit log (the policy logs only what the analysis needs)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    n = 0
    with open(out_path, "w") as out:
        for line in proc.stdout:
            if "audit.k8s.io" not in line:
                continue
            try:
                event = json.loads(line)
                if audit_time(event) < since_epoch:
                    continue
            except (ValueError, KeyError):
                continue
            out.write(json.dumps(event, separators=(",", ":")) + "\n")
            n += 1
    if proc.wait() != 0:
        raise RuntimeError(f"audit log command failed: {' '.join(cmd)}")
    return n
