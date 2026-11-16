# Kubernetes VPA (InPlace) under a serverless workload

Experiment harness for [docs/spec.md](docs/spec.md). Section numbers below refer to the spec.

## Layout

| Path | What it is |
|---|---|
| `src/vpaexp/trace.py` | Arrival times from the Azure trace, windows A and B, replay schedule at `k = 4` (§4) |
| `src/vpaexp/sizing.py` | Offline demand calculation and the static sizing rule (§6.1) |
| `src/vpaexp/loadgen.py` | Open-loop load generator (§5) |
| `src/vpaexp/config.py` | VPA flags scaled by `k`, declared tuning knobs, Deployment and VPA objects (§3, §6, §7) |
| `src/vpaexp/cluster.py` | kubectl access, watch recorder, metrics-API poller, audit log extract (§8.2) |
| `src/vpaexp/run.py` | One run: clean state, deploy, replay, collect (§9) |
| `src/vpaexp/records.py` | Reads a results directory; pairs each resize with its three lag timestamps (§8.1) |
| `src/vpaexp/metrics.py` | Per-run metrics, whole run and per phase (§8.1) |
| `src/vpaexp/decisions.py` | Tuned-configuration selection rule, H1 test, prototype gate checks (§1, §7, §11) |
| `src/vpaexp/figures.py` | Figures 1–9 (§12) |
| `function/` | Container image: SeBS `501.graph-pagerank` behind the HTTP wrapper, plus the cgroup sampler (§3, §8.2) |
| `scripts/` | Testbed setup for the prototype and the full-run machine, audit policy |
| `cluster/*.toml` | Testbed profiles: `prototype` (Mac minikube) and `full` (ARM server) |
| `tuning/` | Tuned configurations and the tuning log |
| `data/arrivals.txt.gz` | The trace function's 142,856 arrival times, extracted from the Azure trace |

## Development

```bash
uv sync
uv run pytest
uv run mypy
```

`data/arrivals.txt.gz` is committed, so the 305 MB trace is only needed to regenerate it (`uv run vpaexp extract`).

## Prototype gate (§11), on the Mac

```bash
scripts/setup-prototype.sh
uv run vpaexp --profile prototype measure
uv run vpaexp --profile prototype prototype
```

`setup-prototype.sh` prints the function URL; put it in `cluster/prototype.toml` if it differs. `prototype` runs for 30 minutes and then prints the five gate checks. `uv run vpaexp gate <run-dir>` re-evaluates them on an existing run.

## Full runs, on the ARM server

`scripts/setup-fullrun.sh` has not been run on the target machine. Read it first, then follow the checks it prints. All commands take `--profile full`.

```bash
scripts/setup-fullrun.sh
uv run vpaexp --profile full measure                       # payload cost in the container (§13)
uv run vpaexp --profile full size --window A --validate    # offline calculation + 1h validation replay (§6.1)
uv run vpaexp --profile full size --window B --validate

# smoke test: one short run per setting (§9)
uv run vpaexp --profile full run --setting static  --window A --limit-s 600
uv run vpaexp --profile full run --setting default --window A --limit-s 600
uv run vpaexp --profile full run --setting tuned   --window A --limit-s 600 --config tuning/example.toml

# the campaign: one command per run, sequential only (§9, §10)
uv run vpaexp --profile full run --setting default --window A --rep 1
uv run vpaexp --profile full run --setting tuned   --window A --label it1 --config tuning/it1.toml
uv run vpaexp --profile full run --setting static  --window A --rep 1
```

If `size --validate` reports a disagreement above 10%, it exits non-zero and that window needs `size --window X --full-profile` (a 24h profiling replay).

Each run writes a self-describing directory under `results/<profile>/runs/<window>-<setting>-<label>/`:

| File | Content |
|---|---|
| `run.json` | Setting, window, flags, manifests, initial allocation, component versions and arguments |
| `client.csv` | Per request: scheduled, sent, completed (epoch seconds), status |
| `sampler.csv` | 1s cgroup samples: `cpu.stat`, `memory.current`, `memory.peak`, `memory.events` |
| `watch.jsonl` | Every change to the pod, the VPA object and events, stamped on receipt |
| `audit.jsonl` | API server audit events for pod resizes and VPA status writes |
| `summary.json` | Metrics for the whole run and per phase, and every resize with its timestamps |

Bring `results/full/` back from the server; analysis runs anywhere:

```bash
uv run vpaexp figures results/full/runs/A-default-r1       # figures 1-6
uv run vpaexp --profile full tune-select                   # selection rule over it1, it2, ...
uv run vpaexp --profile full report --window A             # figure 7, latency CDF, H1
uv run vpaexp --profile full report --window B             # figure 8
uv run vpaexp tuning-log                                   # figure 9 from tuning/log.toml
```

## Tuning (§7)

Each iteration is a TOML file with a `[flags]` table (see `tuning/example.toml`). Flags outside the declared knob set are rejected. Record each iteration in `tuning/log.toml`. Once `tune-select` has chosen, run the main comparison with that file: `run --setting tuned --window A --rep N --config tuning/<chosen>.toml`.

## Interpretations of the spec

Places where the spec left a choice open; each is one function, easy to change.

- **Validation replay and peaks (§6.1).** A window's peak concurrency and peak 15s CPU fall hours apart, so one 1h replay cannot contain both. The replay is centred on the calculated CPU peak and compared with the calculation for that same hour. Where it measures more than calculated, the window's calculated peak is raised by the same ratio (`sizing.static_sizing`).
- **Selection rule (§7).** "Lowest mean allocated CPU and memory" is scored as CPU and memory each taken as a fraction of the static allocation, summed (`decisions.select_tuned`).
- **H1 (§1).** A metric is beaten when the median improves by more than the larger of the two min-to-max spreads. H1 is supported if mean CPU allocation, mean memory allocation or p99 is beaten (`decisions.h1`).
- **Recommendation timestamp (§8.1).** For a resize, "recommendation updated" is the first VPA status write in the latest unbroken run of writes carrying the value that was patched into the pod (`records._recommended_at`).
- **Resize applied (§8.1).** A resize counts as applied at the kubelet's first status write that shows the new limits with no resize in progress. The time is taken from the audit log, so all three lag timestamps share the API server's clock; the watch recorder is the fallback.
- **OOM kills and restarts.** An OOM that kills the whole container shows as a container restart, not in the container's `memory.events`. Restarts are counted per run (`container_restarts`), fail the static sizing check, and disqualify a tuning iteration like an OOM kill.
- **`minReplicas: 1` on the VPA object.** The updater ignores workloads with fewer replicas than its `--min-replicas` (default 2). The per-object override is set so that it acts on the single pod; no component flag changes.
- **Latency of failed requests.** Timeouts count at the timeout value (§8.1). Requests with status `error` are excluded from latency and reported in the error rate.

## Not verified, and known gaps

Nothing here has run against a cluster: the unit tests cover the trace, sizing, load generator, configuration and analysis, and the function wrapper was exercised locally with a stub handler. The first prototype run is the test of `cluster.py`, `run.py`, the image build and the setup scripts.

- **VPA 1.7.1 flag and field names** (`confidence-interval-cpu`, `updateMode: InPlace`, the `InPlace` feature gate) are taken from the spec, not checked against the release.
- **NUMA placement** on the full-run machine relies on systemd `CPUAffinity` for containerd and the kubelet. `setup-fullrun.sh` prints how to check it.
- **metrics-server and flannel** install from their latest release. The versions used are recorded in each `run.json` but not pinned.
- **Events** are recorded in `watch.jsonl` but not parsed; failed and deferred resizes are counted from pod conditions.
- **Existing VPA on the minikube cluster.** If it is not 1.7.1, `install-components.sh` stops. Replacing it (`scripts/setup-prototype.sh --replace-vpa`) deletes every VPA object on the cluster, including `primes-vpa` and `base64-vpa`.
