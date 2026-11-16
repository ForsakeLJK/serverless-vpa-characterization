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
    print(f"sampling {cgroup}", flush=True)
    start = time.monotonic()
    n = 0
    with open(args.out, "w") as out:
        out.write(HEADER + "\n")
        while True:
            try:
                out.write(sample(cgroup) + "\n")
            except FileNotFoundError:
                print("cgroup disappeared: the container was restarted or removed", file=sys.stderr, flush=True)
                return 1
            out.flush()
            n += 1
            time.sleep(max(0.0, start + n * args.interval - time.monotonic()))


if __name__ == "__main__":
    sys.exit(main())
