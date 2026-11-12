# Experiment specification: Kubernetes VPA (InPlace) under a serverless workload

Status: agreed design, not yet implemented. Written 2026-11-12. Target venue: ISPASS 2027.

Items marked **[verify]** are assumptions or single measurements that must be confirmed before the full runs. They are collected in section 13.

## 1. Goal, hypothesis and research questions

Characterize the Kubernetes Vertical Pod Autoscaler, in InPlace update mode, on a single serverless function driven by real arrival times. Findings come from the results.

**Research questions.** Every metric and figure must serve one of these.

- **RQ1**: How well does the recommender track serverless demand (slack and under-provisioning over time)?
- **RQ2**: How long does the InPlace pipeline take from recommendation to pod patch to kubelet-applied, and how often does it fail or defer?
- **RQ3**: What is the net effect on tail latency and resource efficiency versus static allocation, and how sensitive is it to configuration?

**Hypothesis H1: best VPA performance requires hand-tuning.**

- Test: on window A, compare the default configuration with the best configuration found by hand-tuning on window A.
- Supported if the tuned configuration beats default on mean allocation or on p99 latency by more than the spread (min to max) across the 3 repetitions of each.
- Rejected otherwise.
- The tuned configuration is labelled "best-found on window A". It is not claimed to be a general-purpose setting.

**Transfer question.** Does a configuration hand-tuned on window A still hold up on window B? The A-tuned configuration is run unchanged on window B and compared with static and default there.

## 2. Testbed

| | Prototype | Full runs |
|---|---|---|
| Machine | Mac, minikube profile `vpa-serverless` (vfkit VM) | ARM Linux server |
| CPU / memory | 4 vCPU, 8 GB | 128 cores (2 NUMA nodes × 64), about 510 GB |
| Architecture | arm64 | arm64 |
| Kubernetes | v1.37.0, containerd | v1.37.0, installed with kubeadm, single node, stock kubelet |
| VPA | 1.7.1 | 1.7.1 |
| Client placement | Mac host | Same machine, on NUMA node 1 |
| Function pod and Kubernetes | The VM | NUMA node 0 |

- Single tenant: nothing else runs on the node during a run.
- The full-run machine is not reachable from the development session. The experimenter runs the campaign and brings the results back (section 9).
- The VPA updater and admission controller run with `--feature-gates=InPlace=true`.
- Objects currently on the minikube cluster (`primes-vpa`, `base64-vpa`, the `vpaexp-sampler` pod, shortened recommender flags) are not part of this project. Everything is defined from scratch in this repo.

## 3. Workload function

- **Source**: the primes function from AWARE (`aware-main/benchmarks/primes/web-server-primes.py`), a pure-Python sieve.
- **Payload**: `n`, the sieve's upper bound. It is one fixed value for every request in every run.
  - Full runs: `n` = 3,000,000, provisional **[verify]**.
  - Prototype only: `n` = 1,000,000, to fit 4 vCPUs.
- **Deviations from AWARE**, both documented in the paper:
  - `n` is read from the request instead of being hardcoded at 10,000,000.
  - Requests are processed concurrently, one process per request. As shipped, the server handles one request at a time, which caps CPU at one core and makes memory independent of load.
- **Concurrency cap**: 4× the peak concurrency observed in the profiling run for that window, fixed for all settings on that window. Requests beyond the cap queue in the server.
- **Deployment**: one Deployment, one replica, no horizontal scaling.
- **Resources**: requests equal limits for CPU and memory (Guaranteed QoS), so every allocation change alters what the function can use.
- **Resize policy**: `NotRequired` for CPU and memory. No resize may restart the container.

Indicative cost per invocation, measured once on the Mac with Python 3.12 **[verify]**:

| `n` | Service time | Memory above a 13 MB baseline |
|---|---|---|
| 1,000,000 | 76 ms | 21 MB |
| 3,000,000 | 252 ms | 64 MB |
| 10,000,000 | 1,012 ms | 211 MB |

Both scale linearly with `n`, about 280 MB per busy core.

## 4. Arrival process

- **Trace**: Azure Functions Invocation Trace 2021 (`azure-trace/AzureFunctionsInvocationTraceForTwoWeeksJan2021.txt`), 14 days.
- **Arrival time**: `end_timestamp − duration`, since the trace records only end times.
- **Function**: the one whose `func` id starts with `155e47f8e7f7`. It is the busiest (535,667 invocations) and the only high-volume function with load on all 14 days. It is active in 40.9% of minutes, with per-minute counts of 110 (p95), 125 (p99) and 252 (max).
- **No rate scaling**: arrival times are used as recorded, apart from time compression.
- **Time compression**: `k = 12`. A 24h window replays in 2h. Mean rate becomes about 5 rps, the p95 minute 22 rps, the peak minute 50 rps.
- **Windows** (days are 0-indexed from the trace start, each 86,400 s):
  - Window A: day 3 (43.1k invocations). Used for tuning and the main comparison.
  - Window B: day 12 (42.6k invocations). Used for the transfer evaluation.
  - Prototype: the first 6h of day 3, which is 30 minutes at `k = 12`.

## 5. Load generator

- Open loop: each request is sent at its scheduled time whether or not earlier ones have finished.
- Written in Python, environment managed with uv.
- Timeout: fixed at 10× the unconstrained service time of the chosen `n`.
- Records for every request: scheduled send time, actual send time, completion time, status (ok, error, timeout).

## 6. Settings

All three settings use the same window, the same `n`, and the same initial allocation.

### 6.1 Static (no VPA)

- A profiling run replays the window for 2h with generous fixed limits and no VPA.
- Static CPU = the highest CPU usage in any 10s sample, plus 15%.
- Static memory = the highest memory usage observed, plus 15%.
- Computed separately for windows A and B.
- The profiling run also yields the peak concurrency used for the concurrency cap.

### 6.2 VPA default

Upstream defaults, with time-based statistical windows divided by `k = 12`. Everything not listed is stock.

| Flag | Component | Upstream | Used |
|---|---|---|---|
| `cpu-histogram-decay-half-life` | recommender | 24h | 2h |
| `memory-histogram-decay-half-life` | recommender | 24h | 2h |
| `memory-aggregation-interval` (count stays 8) | recommender | 24h | 2h |
| `confidence-interval-cpu`, `confidence-interval-memory` | recommender | 24h | 2h |
| `in-recommendation-bounds-eviction-lifetime-threshold` | updater | 12h | 1h |
| `evict-after-oom-threshold` | updater | 10 min | 50s |
| `recommender-interval` | recommender | 1 min | 10s **[verify]** |
| `updater-interval` | updater | 1 min | 10s **[verify]** |
| metrics-server `--metric-resolution` | metrics-server | 60s | 10s **[verify]** |
| `target-cpu-percentile`, `target-memory-percentile` | recommender | 0.9 | 0.9 |
| `recommendation-margin-fraction` | recommender | 0.15 | 0.15 |
| `pod-recommendation-min-cpu-millicores` | recommender | 25 | 25 |
| `pod-recommendation-min-memory-mb` | recommender | 250 | 250 |
| `pod-update-threshold` | updater | 0.1 | 0.1 |

The loop intervals and metrics resolution are set to the lowest value stock metrics-server accepts: 10s if the prototype confirms it, otherwise 15s. No component is patched or replaced.

### 6.3 VPA tuned

The configuration chosen by the procedure in section 7.

### 6.4 VPA object, both VPA settings

- `updateMode: InPlace`. No evict-and-recreate.
- `controlledValues: RequestsAndLimits`, so the 1:1 request-to-limit ratio is preserved.
- `maxAllowed`: node allocatable minus a fixed system reserve **[verify value]**, the same for both settings.
- Initial pod allocation: the static allocation for that window.

## 7. Tuning procedure

Hand-tuning by diagnose-and-adjust, on window A only.

1. Start from the first default run on window A.
2. Record what went wrong (for example, memory pinned at the floor, or CPU target lagging bursts).
3. Change a knob from the declared set to address it, and run once.
4. Repeat, up to the budget.

Guardrails:

- **Declared knobs**: CPU and memory half-life, memory aggregation interval, confidence intervals, target percentiles, safety margin, the 25m / 250Mi floor, in-bounds lifetime threshold, update threshold. Nothing else may change. Loop intervals are excluded, since default is already at the stock floor.
- **Tuning log**: each iteration records the observation, the knob changed and why, and the result. The log is a paper artifact (figure 9).
- **Budget**: 3 iterations to start. It may be raised later if the log shows continued improvement.
- **Selection rule**, fixed before any tuning run: the tuned configuration is the iteration with the lowest mean allocated CPU and memory that has zero OOM kills and p99 latency no worse than 1.1× the static p99. If no iteration meets the constraint, take the one with the lowest p99 and report that the constraint was unmet.

## 8. Metrics and collection

### 8.1 Metrics

| Metric | Definition | Source |
|---|---|---|
| p99 latency | Client-side, completion minus scheduled send time. Timeouts are included at the timeout value. | Client log |
| Throughput | Successful completions per second | Client log |
| Error rate | Timeouts and errors as a share of requests sent, reported separately | Client log |
| CPU and memory usage | Per 1s sample | cgroup sampler |
| Utilization | Usage divided by current request | cgroup sampler, pod status |
| Allocation | Mean requested CPU and memory over the run | Pod status |
| VPA recommendation | Target, lower bound, upper bound over time | VPA object watch |
| Actuation lag | Three timestamps per resize: recommendation updated, pod patched, resize applied by the kubelet | Watch recorder, audit log |
| Resize count | Applied resizes, split by resource and direction | Watch recorder |
| Failed or deferred resizes | Resizes reported as deferred, infeasible or errored | Pod status and events |
| OOM kills | Count | `memory.events` |
| CPU throttling | Throttled periods and throttled time | `cpu.stat` |

### 8.2 Collectors

No Prometheus. Two purpose-built collectors:

- **Watch recorder**: watches the VPA object, pod spec and status, and events. The API server audit log is enabled and is the authoritative clock for "recommendation updated" and "pod patched".
- **cgroup sampler**: runs on the node and reads the container's `cpu.stat`, `memory.current` and `memory.events` every 1s.

## 9. Run protocol and hand-over

- **Clean state per run**: new pod; VPA object and its checkpoint deleted; recommender restarted. No history carries over.
- **Cold start**: the full 2h is reported. No warm-up period is excluded.
- **One command per run**, taking a setting, a window and a repetition number.
- **Self-describing results directory** per run: the exact configuration and flags, component versions, client log, sampler output, watch recorder output, audit log extract.
- **Setup script** for the full-run machine: kubeadm cluster, VPA with the scaled flags, metrics-server resolution, NUMA placement.
- **Sequential runs only**. The updater's flags are cluster-wide, and concurrent runs would share the kubelet.

## 10. Run plan

Repetition rule: every setting that appears in a results figure gets 3 repetitions, reported as the median with min-to-max range. Tuning iterations get 1 run each.

| Stage | Runs | Time |
|---|---|---|
| Profiling, windows A and B | 2 | 4h |
| Tuning iterations on window A | 3 | 6h |
| Main comparison on A (3 settings × 3 repetitions) | 9 | 18h |
| Transfer on B (3 settings × 3 repetitions) | 9 | 18h |
| **Total** | **23** | **46h** |

Allow about 48h with per-run reset and collection overhead (estimated, not measured). The first default run on A doubles as the starting observation for tuning.

## 11. Prototype gate

One 30-minute run on the Mac minikube cluster, with VPA windows shortened further so that resizes occur within the run. No full run starts until all five checks pass:

1. At least one CPU resize up and one CPU resize down.
2. At least one memory resize.
3. All three lag timestamps captured for every resize.
4. The lowest stock metrics interval confirmed (10s or 15s), by checking that samples change at that period.
5. Figures 1–6 render from the collected data.

## 12. Figures

1. Arrival rate over time for the window.
2. CPU over time: usage, request, VPA target with lower and upper bounds.
3. Memory over time: the same four series.
4. Latency over time (p50 and p99 per 1-minute bucket) with resize events marked.
5. Latency CDF, all three settings overlaid.
6. Lag breakdown per resize: recommendation updated to pod patched, pod patched to kubelet applied.
7. Summary bars with median and range over repetitions: p99, throughput, error rate, mean allocated CPU and memory, mean utilization, resize count, failed or deferred resizes, OOM kills, throttled time.
8. Transfer: figure 7 for window B.
9. Tuning log table: iteration, observation, knob changed, result.

## 13. Items to verify before full runs

- **`n`**: re-measure service time and memory per invocation on the ARM server, then fix `n`. Expected demand at `n` = 3M from the Mac figures: about 1.3 cores mean and 12.6 at the peak minute, memory up to about 800 MB above baseline.
- **Metrics floor**: whether metrics-server on this version accepts 10s, and whether kubelet stats refresh that often.
- **System reserve** for `maxAllowed`: value not yet chosen.
- **Memory per invocation in the container**: the 64 MB figure is a single local measurement outside a container.

## 14. Limitations to state in the paper

- **Loops are scaled less than windows.** Statistical windows are scaled 12×, loop intervals and metrics resolution 6× (4× if the floor is 15s). VPA reacts about 2× slower relative to the workload than in production.
- **Kubelet actuation time is not scaled.** Lag is reported in absolute seconds.
- **Cold start only.** Each run shows VPA's first scaled day, not steady state. With the confidence interval at 2h, the upper bound is about 13× the estimate after 10 minutes, 5× after 30 minutes, 3× after 1 hour and 2× at the end. Scale-downs from the peak allocation therefore mostly wait for the 1h in-bounds threshold.
- **Memory recommendations cannot fall within a run under default**, because one 2h aggregation interval covers the whole run.
- **Tuned is in-sample on window A** by design (H1). Window B measures transfer only against static and default, not against B's own best configuration.
- **One function, one pod, one payload, one trace function.** Results do not cover horizontal scaling, multi-tenancy or other workload shapes.
- **Client and server share one machine** in the full runs, separated by NUMA node.

## 15. Future work

- `k = 4` with fully consistent scaling (6h per run, loops and windows all scaled 4×).
- Pre-warmed history: replay the previous day before each measured run (4h per run).
- Hand-tuning on window B as well, to measure the transfer gap against B's own best.
- A larger tuning budget.
