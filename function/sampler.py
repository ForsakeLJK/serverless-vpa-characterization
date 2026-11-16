"""cgroup sampler (spec section 8.2). Standard library only; runs on the node.

Every second, reads the function container's cpu.stat, memory.current, memory.peak and
memory.events (cgroup v2) and appends one CSV row. Counters are written as read (cumulative).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

HEADER = "t,usage_usec,nr_periods,nr_throttled,throttled_usec,memory_current,memory_peak,oom_kill"


def find_cgroup(root: str, container_id: str) -> str:
    cid = container_id.split("//")[-1]
    for dirpath, dirnames, _ in os.walk(root):
        for d in dirnames:
            if cid in d:
                return os.path.join(dirpath, d)
    raise FileNotFoundError(f"no cgroup directory for container {cid} under {root}")


def newest_container(pod_cgroup: str, not_this: str) -> str | None:
    """After a restart: the pod's container cgroup that is not the old one and not the pause sandbox."""
    best, best_mem = None, 0
    for d in os.listdir(pod_cgroup):
        path = os.path.join(pod_cgroup, d)
        if path == not_this or not os.path.isfile(os.path.join(path, "memory.current")):
            continue
        mem = _value(os.path.join(path, "memory.current"))
        if mem > best_mem:  # the pause container holds well under 1 MB
            best, best_mem = path, mem
    return best if best_mem > 4 * 1024 * 1024 else None


def _fields(path: str) -> dict[str, int]:
    with open(path) as f:
        return {k: int(v) for k, v in (line.split() for line in f)}


def _value(path: str) -> int:
    try:
        with open(path) as f:
            return int(f.read())
    except FileNotFoundError:  # memory.peak needs Linux 5.19+
        return 0


def sample(cgroup: str) -> str:
    cpu = _fields(os.path.join(cgroup, "cpu.stat"))
    events = _fields(os.path.join(cgroup, "memory.events"))
    current = _value(os.path.join(cgroup, "memory.current"))
    peak = _value(os.path.join(cgroup, "memory.peak"))
    return (
        f"{time.time():.3f},{cpu['usage_usec']},{cpu.get('nr_periods', 0)},{cpu.get('nr_throttled', 0)},"
        f"{cpu.get('throttled_usec', 0)},{current},{peak},{events.get('oom_kill', 0)}"
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cgroup-root", default="/sys/fs/cgroup")
    p.add_argument("--container-id", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--interval", type=float, default=1.0)
    args = p.parse_args()

    cgroup = find_cgroup(args.cgroup_root, args.container_id)
    pod_cgroup = os.path.dirname(cgroup)
    print(f"sampling {cgroup}", flush=True)
    start = time.monotonic()
    n = 0
    with open(args.out, "w") as out:
        out.write(HEADER + "\n")
        while True:
            try:
                out.write(sample(cgroup) + "\n")
            except FileNotFoundError:
                # The container was restarted (for example OOM-killed) and has a new cgroup.
                # Counters restart from zero there; the analysis treats a drop as a restart.
                replacement = None
                try:
                    replacement = newest_container(pod_cgroup, cgroup)
                except FileNotFoundError:
                    pass
                if replacement:
                    print(f"container restarted; now sampling {replacement}", flush=True)
                    cgroup = replacement
            out.flush()
            n += 1
            time.sleep(max(0.0, start + n * args.interval - time.monotonic()))


if __name__ == "__main__":
    sys.exit(main())
