# Kubernetes VPA (InPlace) under a serverless workload

Experiment harness for [docs/spec.md](docs/spec.md). Section numbers below refer to the spec.

VPA controls memory only. The function's CPU is fixed in every run at `generous_cpu_millicores` from the cluster profile (`cluster/*.toml`), and no CPU metric is a result. A run that records any CPU throttling is invalid (§6): `run` prints `RUN INVALID` and exits non-zero, `summary.json` records `"valid": false`, and `report` and `tune-select` leave the run out. Raise that value and repeat the run. The prototype run is exempt (§11).

## Layout

| Path | What it is |
|---|---|
| `src/vpaexp/trace.py` | Arrival times from the Azure trace, windows A and B, replay schedule at `k = 4` (§4) |
| `src/vpaexp/sizing.py` | Offline demand calculation and the static sizing rule (§6.1) |
| `src/vpaexp/loadgen.py` | Open-loop load generator (§5) |
| `src/vpaexp/config.py` | VPA flags scaled by `k`, declared tuning knobs, Deployment and VPA objects (§3, §6, §7) |
| `src/vpaexp/cluster.py` | Testbed profile (`cluster/*.toml`), kubectl access, watch recorder, metrics-API poller, audit log extract (§8.2) |
| `src/vpaexp/run.py` | One run: clean state, deploy, replay, collect (§9) |
| `src/vpaexp/records.py` | Reads a results directory; pairs each resize with its three lag timestamps and the problems the kubelet reported (§8.1) |
| `src/vpaexp/metrics.py` | Per-run metrics, whole run and per phase; run validity; measured payload cost and peaks (§6, §8.1, §13) |
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

### Arguments

Global options go before the command name (`vpaexp --profile full run ...`).

| Name | Required | Default | When to use | Meaning |
|---|---|---|---|---|
| `--profile NAME` | No | `prototype` | Pass `full` on every command on the ARM server. | Testbed profile, read from `cluster/NAME.toml`. Sets the kube context, namespace, image, function URL and sizing limits, and the results subdirectory. |
| `--results DIR` | No | `results` | To keep a campaign's output apart from earlier results. | Root of the results tree. Output goes to `DIR/<profile>/`. |

`measure` sends a few spaced requests to a generously sized pod and writes `payload-cost.json` (service time and memory per request), which `size` and `run` then use in place of the profile's estimates.

| Name | Required | Default | When to use | Meaning |
|---|---|---|---|---|
| `--requests N` | No | `20` | Raise it if the measured service time varies between attempts. | Number of calibration requests, sent a fixed 3s apart so they never overlap. This is a sample size, not a load level: `measure` does not replay the trace. |
| `--force` | No | off | When re-measuring. | Replace the existing `measure` run directory. Without it, the command refuses to overwrite. |

`size` computes the static allocation and concurrency cap for a window. With no mode flag it prints the offline calculation and saves nothing.

| Name | Required | Default | When to use | Meaning |
|---|---|---|---|---|
| `--window A\|B` | Yes | none | Always. Size each window once before its runs. | Which trace window to size: A is the 4 trace days from day offset 2, B the 4 from day offset 9. Each replays in 24h at `k = 4`. |
| `--validate` | No | off | The normal path for the campaign. | Run the 1h validation replay around the calculated memory peak, then save the sizing. Exits non-zero if measurement and calculation disagree by more than 10%. |
| `--full-profile` | No | off | When `--validate` fails, or a static run reports the sizing as too low. | Run a full 24h profiling replay and size from its measured peaks. |

A validation or profiling replay that records any CPU throttling is invalid: `size` exits non-zero and saves no sizing (§13).
| `--write-unvalidated` | No | off | Smoke tests only. | Save the calculation alone, with no replay. `run` warns when it uses such a sizing. |
| `--force` | No | off | When repeating a validation or profiling replay. | Replace the existing validation or profiling run directory. |

`--validate`, `--full-profile` and `--write-unvalidated` are mutually exclusive.

`run` executes one run. It needs a saved sizing for the window.

| Name | Required | Default | When to use | Meaning |
|---|---|---|---|---|
| `--setting static\|default\|tuned` | Yes | none | Always. | `static`: no VPA, fixed allocation from `size`. `default`: VPA with its default flags scaled by `k`. `tuned`: VPA with the flags from `--config`. |
| `--window A\|B` | Yes | none | Always. | The trace window to replay. Request timing comes from the trace's arrival times in that window. |
| `--rep N` | No | `1` | For the main comparison: 1, 2, 3 for each setting. | Repetition number. Names the run `r<N>`; `report` needs 3 repetitions each of `default` and `tuned` to evaluate H1. |
| `--config FILE` | Only with `--setting tuned` | none | Every tuned run: tuning iterations and the final tuned repetitions. | A TOML file with a `[flags]` table of declared knobs (see `tuning/example.toml`). Ignored for `static` and `default`. |
| `--label NAME` | No | `r<rep>` | Tuning iterations: `it1`, `it2`, ... | Run label in place of `r<rep>`. `tune-select` picks up `it<N>` labels, `report` only `r<N>`. |
| `--limit-s SECONDS` | No | whole window (24h) | Smoke tests, before committing to a 24h run. | Replay only the first `SECONDS` of the window. Appends `-smoke` to the label. |
| `--force` | No | off | When redoing a run that failed or was invalid. | Replace the run directory if it already exists. Without it, the command refuses to overwrite. |

The run directory is `<window>-<setting>-<label>`, so `--setting default --window A --rep 2` writes `A-default-r2`.

Each run writes a self-describing directory under `results/<profile>/runs/<window>-<setting>-<label>/`:

| File | Content |
|---|---|
| `run.json` | Setting, window, flags, manifests, initial allocation, component versions and arguments |
| `client.csv` | Per request: scheduled, sent, completed (epoch seconds), status |
| `sampler.csv` | 1s cgroup samples: `memory.current`, `memory.peak`, `memory.events`, and `cpu.stat` (used only to check that CPU was never throttled) |
| `watch.jsonl` | Every change to the pod, the VPA object and events, stamped on receipt |
| `audit.jsonl` | API server audit events for pod resizes and VPA status writes |
| `summary.json` | Whether the run is valid, metrics for the whole run and per phase, and every resize with its timestamps |

Bring `results/full/` back from the server; analysis runs anywhere:

```bash
uv run vpaexp figures results/full/runs/A-default-r1       # re-render figures 1-6 and overview.png (every run renders them itself)
uv run vpaexp --profile full tune-select                   # selection rule over it1, it2, ...
uv run vpaexp --profile full report --window A             # figure 7, H1
uv run vpaexp --profile full report --window B             # figure 8 (H1 is tested on window A only)
uv run vpaexp tuning-log                                   # figure 9 from tuning/log.toml
```

## Tuning (§7)

Each iteration is a TOML file with a `[flags]` table (see `tuning/example.toml`). Flags outside the declared knob set are rejected. Record each iteration in `tuning/log.toml`. Once `tune-select` has chosen, run the main comparison with that file: `run --setting tuned --window A --rep N --config tuning/<chosen>.toml`.

## Interpretations of the spec

Places where the spec left a choice open; each is one function, easy to change.

- **Fixed CPU (§6).** The spec asks for a generous value per testbed. It is `generous_cpu_millicores` in the profile; `size` warns if it is below one core per request at peak concurrency. A run with any throttled time in `cpu.stat` is invalid, the prototype run excepted (`metrics.invalid_reason`).
- **Validation replay and peaks (§6.1).** The 1h replay is centred on the calculated memory peak and compared with the calculation for that same hour. Where it measures more than calculated, the window's calculated peak is raised by the same ratio (`sizing.static_sizing`).
- **Selection rule (§7).** The static p99 and failed-request rate are the worst values over the static repetitions on window A that exist when `tune-select` runs. The failed-request rate is the timeout rate plus the error rate. An iteration's OOM kills include OOM kills of the whole container (`decisions.select_tuned`).
- **H1 (§1).** A metric is beaten when the median improves by more than the larger of the two min-to-max spreads. H1 is supported if mean memory allocation or p99 is beaten (`decisions.h1`).
- **Recommendation timestamp (§8.1).** For a resize, "recommendation updated" is the first VPA status write in the latest unbroken run of writes carrying the value that was patched into the pod (`records._recommended_at`).
- **Resize applied (§8.1).** A resize counts as applied at the kubelet's first status write that shows the new limits with no resize in progress. The time is taken from the audit log, so all three lag timestamps share the API server's clock; the watch recorder is the fallback.
- **OOM kills and restarts.** An OOM that kills the whole container shows as a container restart with reason OOMKilled, not in the container's `memory.events`. A run's OOM kills are the cgroup counter's kills plus those restarts, each timed by the kubelet's `finishedAt` (`metrics.oom_kill_times`); every count, phase split and figure mark comes from that function. Any OOM kill in a static run fails the sizing check. Restarts are also counted per run (`container_restarts`); one with another cause is logged as a warning.
- **Failed or deferred resizes (§8.1).** A resize counts when the pod's conditions or a kubelet event (`ResizeDeferred`, `ResizeInfeasible`, `ResizeError`) report a problem with it, or the API server rejected the patch. A resize that is only still pending when the next one replaces it or the run ends does not count (`records.derive_resizes`).
- **`minReplicas: 1` on the VPA object.** The updater ignores workloads with fewer replicas than its `--min-replicas` (default 2). The per-object override is set so that it acts on the single pod; no component flag changes.
- **Latency of failed requests.** Timeouts count at the timeout value (§8.1). Requests with status `error` are excluded from latency and reported in the error rate.

## Not verified, and known gaps

A prototype run on the minikube cluster passed the gate (§11) when VPA still controlled CPU and memory; it exercised `cluster.py`, `run.py`, the image build, the collectors and the figures end to end. The gate has not been re-run since the change to memory-only VPA, so the `controlledResources: ["memory"]` object and gate check 2 are untested against a cluster (the recorded run fails check 2, as it should: its VPA still resized CPU). `scripts/setup-fullrun.sh` and the `full` profile have not been run.

- **The prototype VM cannot give generous CPU.** It has 4 vCPUs and the function gets 2.5, against a peak concurrency well above that, so the prototype run is throttled. The gate reports this as a note; the validity check does not apply to the prototype run (§11).

- **VPA 1.7.1 flag and field names** were accepted by the 1.7.1 recommender and updater in the prototype run.
- **NUMA placement** on the full-run machine relies on systemd `CPUAffinity` for containerd and the kubelet. `setup-fullrun.sh` prints how to check it.
- **metrics-server and flannel** install from their latest release. The versions used are recorded in each `run.json` but not pinned.
- **Resize events.** Only `ResizeError` has been seen on a cluster (twice in the prototype run). `ResizeDeferred` and `ResizeInfeasible` are parsed by name and have not been observed.
- **OOM kill double counting.** If the sampler read the cgroup's `oom_kill` counter after a kill of the whole container but before the cgroup was removed, that kill would be counted twice. The prototype run had 12 container OOM kills and the counter never moved, so this has not been observed; it is not ruled out.
- **Existing VPA on the minikube cluster.** If it is not 1.7.1, `install-components.sh` stops. Replacing it (`scripts/setup-prototype.sh --replace-vpa`) deletes every VPA object on the cluster, including `primes-vpa` and `base64-vpa`.
