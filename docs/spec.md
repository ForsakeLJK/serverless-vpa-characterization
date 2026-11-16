# Experiment specification: Kubernetes VPA (InPlace) under a serverless workload

Status: agreed design, not yet implemented. Written 2026-11-12, revised the same day to `k = 4`, four-day windows and a SeBS workload function. Target venue: ISPASS 2027.

Items marked **[verify]** are assumptions, single measurements or derivations that must be confirmed before the full runs. They are collected in section 13.

## 1. Goal, hypothesis and research questions

Characterize the Kubernetes Vertical Pod Autoscaler, in InPlace update mode, on a single serverless function driven by real arrival times. Findings come from the results.

**Design principle: default VPA must behave as it does in production.** Trace time is compressed by a factor `k`, and every time-based VPA setting is divided by exactly the same `k`, including the control-loop intervals. `k = 4` is the largest factor at which stock components allow this (section 6.2). The only delay that is not scaled is the kubelet's own resize work (section 14).

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

- **Source**: `501.graph-pagerank` from the SeBS benchmark suite (Copik et al., arXiv 2012.14132), Python implementation, at `benchmarks/500.scientific/501.graph-pagerank/python/function.py`. It generates a Barabási–Albert graph with igraph and computes PageRank on it.
- **Why this function**: it is self-contained, and both its CPU time and memory grow with the input. Nothing else sits in the request path. Most other SeBS functions call object storage or a database, which adds latency that VPA can neither observe nor reduce.
- **Pinned versions**: SeBS commit `089e5c6ccece0b4d9508e08eee80cf3527590c4f` (2026-10-24), Python 3.12, igraph 0.11.4 (the version SeBS pins for Python 3.12).
- **Payload**: SeBS's `large` input, which is `size = 100000` vertices with `seed = 42`. It is identical for every request in every run, including the prototype. Each response can be checked against SeBS's expected value for that input.
- **What is borrowed from SeBS**: the function code, unmodified, and its input definition. SeBS's deployment tooling is not used, because its only Kubernetes route is OpenWhisk, which creates and destroys its own function containers and conflicts with a single pod resized in place.
- **Our packaging**: the function runs in our own container behind a thin HTTP wrapper, deployed as a plain Deployment with one replica and no horizontal scaling.
- **Deviations from how SeBS functions normally run**, both documented in the paper:
  - **Concurrency inside one pod**: on a FaaS platform each instance serves one request at a time. Here the wrapper runs one process per request, so the arrival rate drives both CPU and memory within the single pod.
  - **Warm invocations only**: each request's process is forked from a parent that has already imported igraph. Cold starts are out of scope.
- **Concurrency cap**: 4× the peak concurrency from the sizing step for that window (section 6.1), fixed for all settings on that window. Requests beyond the cap queue in the server. The cap stops a throttled pod from forking without bound; the factor 4 is a judgment call, not a derived value **[verify]**.
- **Resources**: requests equal limits for CPU and memory (Guaranteed QoS), so every allocation change alters what the function can use.
- **Resize policy**: `NotRequired` for CPU and memory. No resize may restart the container.

Cost per invocation, measured once on the Mac outside a container **[verify]**. Results matched SeBS's expected values.

| SeBS input size | Vertices | Service time | Memory above a 33 MB baseline |
|---|---|---|---|
| test | 10 | 0.2 ms | 0.3 MB |
| small | 10,000 | 24 ms | 12 MB |
| large (used) | 100,000 | about 330 ms | about 115 MB |

## 4. Arrival process

- **Trace**: Azure Functions Invocation Trace 2021 (`azure-trace/AzureFunctionsInvocationTraceForTwoWeeksJan2021.txt`), 14 days.
- **Arrival time**: `end_timestamp − duration`, since the trace records only end times.
- **Function**: the one whose `func` id starts with `cc5bb2108cc7`. Only its arrival times are used.
- **Why this function**: its recorded execution time matches our workload. Its median duration is 0.38 s (middle 50%: 0.37–0.41 s), against about 0.33 s measured for pagerank at the `large` size. It has 142,856 invocations and load on all 14 days. No other high-volume function with daily load matches a SeBS-defined input size.
- **No rate scaling**: arrival times are used as recorded, apart from time compression.
- **Time compression**: `k = 4`. One trace day replays in 6h.
- **Windows**: four consecutive trace days each, replayed in 24h. Days are 0-indexed from the trace start, each 86,400 s.

| | Window A | Window B |
|---|---|---|
| Trace days | 2–5 | 9–12 |
| Use | Tuning and main comparison | Transfer evaluation |
| Invocations | 52,092 | 57,511 |
| Daily volume (thousands) | 26.5, 9.0, 10.7, 5.8 | 14.7, 13.9, 25.4, 3.5 |
| Peak minute per day (invocations) | 122, 71, 85, 39 | 143, 91, 188, 51 |
| Minutes with any load | 41% | 29% |
| Replayed rate, mean / peak minute | 0.6 / 8.1 rps | 0.7 / 12.5 rps |

Estimated CPU demand at `k = 4`, from per-minute counts and the 0.33 s Mac service time **[verify]**:

| | Window A | Window B |
|---|---|---|
| Median minute | 0 cores | 0 cores |
| 90th percentile minute | 0.7 cores | 0.9 cores |
| 99th percentile minute | 1.5 cores | 2.4 cores |
| Peak minute | 2.7 cores | 4.1 cores |
| Requests arriving in minutes above the 90th percentile | 55% | 72% |

- **The load is idle-heavy and bursty.** The pod is idle in most minutes and averages about 0.2 cores over a run. This is typical of the trace and is the serverless property under study.
- **Memory demand is modest**: about 33 MB idle, and roughly 350 MB (window A) to 500 MB (window B) in the peak minute, which is close to VPA's default 250Mi floor.
- **The windows fall in different weeks.** Window A has its busiest day first; window B has it third.

**Why four days.** A single-day run gives VPA one memory data point and shows only first-day behaviour. Four days is the estimated minimum for default VPA to lower a memory recommendation after a first-day peak (section 14) **[verify]**. VPA's full memory aggregation window is 8 intervals, so an 8-day run (48h at `k = 4`) is needed to exercise it completely; that is future work (section 15).

## 5. Load generator

- Open loop: each request is sent at its scheduled time whether or not earlier ones have finished.
- Written in Python, environment managed with uv.
- Timeout: fixed at 10× the unconstrained service time of the payload (about 3.3 s by the Mac measurement).
- Records for every request: scheduled send time, actual send time, completion time, status (ok, error, timeout).

## 6. Settings

All three settings use the same window, the same payload, and the same initial allocation.

### 6.1 Static (no VPA)

Static is sized for the window's peak demand, found by an offline calculation that a short replay then validates. This replaces a full 24h profiling replay per window.

**Offline calculation.** The workload is deterministic: every request does identical work, and on a generously sized pod requests do not slow each other down. Unthrottled demand over the whole window is therefore computed from the trace's arrival times and the per-request service time and memory measured on the full-run machine. The calculation yields, for each window:

- the peak concurrency (most requests in flight at one instant);
- the highest CPU usage in any 15s window;
- the highest memory usage;
- the moment at which each of these occurs.

**Validation replay.** The calculation does not model allocator overhead or the cost of forking, so it is checked against the real system. For each window, a 1h replay centred on the calculated peak runs with generous fixed limits and no VPA.

- The measured peak concurrency, peak 15s CPU and peak memory must each agree with the calculation within 10%.
- If any does not, that window falls back to a full 24h profiling replay.

**Sizing rule.**

- Static CPU = the highest CPU usage in any 15s window, plus 15%.
- Static memory = the highest memory usage, plus 15%.
- Where calculation and validation replay differ within the tolerance, the larger value is used.
- Computed separately for windows A and B.
- The same step yields the peak concurrency used for the concurrency cap.

**Check during the campaign.** If a static run shows an OOM kill, or CPU usage at the limit for a whole 15s window, the sizing was too low; it is redone with a full profiling replay and the affected runs are repeated.

### 6.2 VPA default

Upstream defaults, with every time-based setting divided by `k = 4`. Everything not listed is stock.

| Flag | Component | Upstream | Used |
|---|---|---|---|
| `cpu-histogram-decay-half-life` | recommender | 24h | 6h |
| `memory-histogram-decay-half-life` | recommender | 24h | 6h |
| `memory-aggregation-interval` (count stays 8) | recommender | 24h | 6h |
| `confidence-interval-cpu`, `confidence-interval-memory` | recommender | 24h | 6h |
| `in-recommendation-bounds-eviction-lifetime-threshold` | updater | 12h | 3h |
| `evict-after-oom-threshold` | updater | 10 min | 150s |
| `recommender-interval` | recommender | 1 min | 15s |
| `updater-interval` | updater | 1 min | 15s |
| metrics-server `--metric-resolution` | metrics-server | 60s | 15s |
| `target-cpu-percentile`, `target-memory-percentile` | recommender | 0.9 | 0.9 |
| `recommendation-margin-fraction` | recommender | 0.15 | 0.15 |
| `pod-recommendation-min-cpu-millicores` | recommender | 25 | 25 |
| `pod-recommendation-min-memory-mb` | recommender | 250 | 250 |
| `pod-update-threshold` | updater | 0.1 | 0.1 |

**Why `k = 4` and not higher.** VPA reads usage from metrics-server, which scrapes the kubelet. Two limits apply:

- metrics-server rejects a `--metric-resolution` below 10s (checked in its source, master branch).
- Its FAQ recommends not going below 15s, which it gives as the resolution of the kubelet's own metrics.

A recommender that polls faster than the metrics refresh sees repeated samples, and each CPU sample is an average over the scrape window. The whole loop therefore cannot usefully run faster than 15s, which is 1 minute divided by 4. At larger `k`, VPA would react more slowly relative to the workload than it does in production (3× slower at `k = 12`, 6× at `k = 24`, with a 15s floor). No component is patched or replaced.

### 6.3 VPA tuned

The configuration chosen by the procedure in section 7.

### 6.4 VPA object, both VPA settings

- `updateMode: InPlace`. No evict-and-recreate.
- `controlledValues: RequestsAndLimits`, so the 1:1 request-to-limit ratio is preserved.
- `maxAllowed`: node allocatable minus a fixed system reserve **[verify value]**, the same for both settings.
- Initial pod allocation: the static allocation for that window.

## 7. Tuning procedure

Hand-tuning by diagnose-and-adjust, on window A only, using full four-day runs.

1. Start from the first default run on window A.
2. Record what went wrong (for example, memory pinned at the floor, or CPU target lagging bursts).
3. Change a knob from the declared set to address it, and run once.
4. Repeat, up to the budget.

Guardrails:

- **Declared knobs**: CPU and memory half-life, memory aggregation interval, confidence intervals, target percentiles, safety margin, the 25m / 250Mi floor, in-bounds lifetime threshold, update threshold. Nothing else may change. Loop intervals are excluded, since 15s is already the practical floor.
- **Tuning log**: each iteration records the observation, the knob changed and why, and the result. The log is a paper artifact (figure 9).
- **Budget**: 3 iterations to start. It may be raised later if the log shows continued improvement.
- **Selection rule**, fixed before any tuning run: the tuned configuration is the iteration with the lowest mean allocated CPU and memory that has zero OOM kills and p99 latency no worse than 1.1× the static p99. If no iteration meets the constraint, take the one with the lowest p99 and report that the constraint was unmet.

An alternative ordering of the stages is recorded in Appendix A as an open question.

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

Every metric is reported for the whole run and split into two phases: the first scaled day (cold start, hours 0–6) and scaled days 2–4 (warmed, hours 6–24).

### 8.2 Collectors

No Prometheus. Two purpose-built collectors:

- **Watch recorder**: watches the VPA object, pod spec and status, and events. The API server audit log is enabled and is the authoritative clock for "recommendation updated" and "pod patched".
- **cgroup sampler**: runs on the node and reads the container's `cpu.stat`, `memory.current` and `memory.events` every 1s.

## 9. Run protocol and hand-over

- **Clean state per run**: new pod; VPA object and its checkpoint deleted; recommender restarted. No history carries over.
- **Cold start**: the full 24h is reported. No warm-up period is excluded.
- **One command per run**, taking a setting, a window and a repetition number.
- **Self-describing results directory** per run: the exact configuration and flags, component versions, client log, sampler output, watch recorder output, audit log extract.
- **Setup script** for the full-run machine: kubeadm cluster, VPA with the scaled flags, metrics-server resolution, NUMA placement.
- **Sequential runs only**. The updater's flags are cluster-wide, and concurrent runs would share the kubelet.
- **Smoke test on the full-run machine**: one short run per setting before the campaign starts, to catch setup problems before committing days of machine time.

## 10. Run plan

Repetition rule: every setting that appears in a results figure gets 3 repetitions, reported as the median with min-to-max range. Tuning iterations get 1 run each.

| Stage | Runs | Time |
|---|---|---|
| Sizing: offline calculation plus a 1h validation replay per window | 2 short | 2h |
| Tuning iterations on window A | 3 | 72h |
| Main comparison on A (3 settings × 3 repetitions) | 9 | 216h |
| Transfer on B (3 settings × 3 repetitions) | 9 | 216h |
| **Total** | **21 full runs, 2 short** | **506h, about 21 days** |

Per-run reset and collection overhead is extra and not yet measured. A window whose validation replay fails the 10% tolerance adds a 24h profiling replay. The first default run on A doubles as the starting observation for tuning.

## 11. Prototype gate

One 30-minute run on the Mac minikube cluster. The prototype only verifies the pipeline, so it does not follow the `k = 4` principle: it replays a busy 2h slice of day 2 at `k = 4`, with VPA windows shortened far below the `k = 4` values so that resizes occur within the run. It uses the same function and payload as the full runs. No full run starts until all five checks pass:

1. At least one CPU resize up and one CPU resize down.
2. At least one memory resize.
3. All three lag timestamps captured for every resize. A resize the kubelet refused or deferred and never applied is normal behavior: it is reported, and needs no applied timestamp.
4. At a 15s metrics resolution, samples from the metrics API are confirmed to refresh every 15s (by polling the pod's `timestamp` and `window` fields under load).
5. Figures 1–6 render from the collected data.

## 12. Figures

1. Arrival rate over time for the window.
2. CPU over time: usage, request, VPA target with lower and upper bounds.
3. Memory over time: the same four series.
4. Latency over time as a line chart (p50, p95 and p99 per 1-minute bucket) with resize events marked.
5. Request outcomes over time: requests per 1-minute bucket that completed, timed out or errored, stacked.
6. Lag breakdown per resize as a stacked bar: recommendation updated to pod patched, pod patched to kubelet applied.

Figures 2 and 3 each have a companion (2b, 3b): utilization over time, as usage in percent of the allocation in force. Figures 2–5, 2b and 3b mark every OOM kill. Figures are rendered at 300 dpi, and every run also gets an overview page with all of its figures together.
7. Summary bars with median and range over repetitions, for the whole run and per phase: p99, throughput, error rate, mean allocated CPU and memory, mean utilization, resize count, failed or deferred resizes, OOM kills, throttled time.
8. Transfer: figure 7 for window B.
9. Tuning log table: iteration, observation, knob changed, result.

## 13. Items to verify before full runs

- **Payload cost**: re-measure pagerank's service time and memory per invocation at the `large` size on the ARM server, in a container. The duration match with the trace function and all demand estimates in section 4 depend on it.
- **Concurrency cap factor**: 4× is a judgment call. Consider one run at a different factor to show the results do not hinge on it.
- **Metrics refresh at 15s**: prototype gate check 4.
- **System reserve** for `maxAllowed`: value not yet chosen.
- **Four-day minimum for memory release**: derived by hand from the decaying-histogram rule (section 14), not checked against the 1.7.1 code. It can be verified offline with synthetic daily peaks.
- **Peak placement**: the derivation assumes the highest memory peak falls on the window's first day. That holds for window A by peak-minute rate but not for window B, whose busiest day is the third. The sizing step (section 6.1) must confirm where the memory peaks actually fall.
- **Memory window enforcement**: the docs define the window as 8 intervals with one peak each, but no code was found that removes peaks older than that. Read the checkpoint and aggregate-state code before making claims about the window in the paper.
- **Design-history citations** for the motivation in section 1.
- **Per-run overhead**: not measured.

## 14. Limitations to state in the paper

- **Kubelet actuation time is not scaled.** It is real work, not a timer. A fixed actuation delay covers a 4× larger share of each burst than in production. Lag is reported in absolute seconds, and the prototype measures how large it is relative to the roughly 45s of loop delay before it.
- **Each run starts cold.** The first scaled day shows wide bounds: the upper bound is the estimate × (1 + 1/days of history), so about 2× at the end of day 1, 1.5× after day 2, 1.33× after day 3 and 1.25× after day 4. Results are therefore reported per phase.
- **Half of the memory window is exercised.** A four-day run yields 4 of the 8 daily peaks in VPA's aggregation window.
- **Memory release under default needs about four scaled days.** The memory target is the 90th percentile of daily peaks, with each peak's weight halving per day of age. A first-day peak holds 33% of the weight after 2 days, 14% after 3 and 6.7% after 4, so it stops setting the target only on day 4 **[verify]**.
- **Tuned is in-sample on window A** by design (H1). Window B measures transfer only against static and default, not against B's own best configuration.
- **Tuning cannot explore faster loops**, because 15s is already the practical floor of the metrics path.
- **One function, one pod, one payload, one trace function.** Results do not cover horizontal scaling, multi-tenancy or other workload shapes.
- **No scale-to-zero.** The pod stays alive through idle periods, which are most of each run. Idle-time cost is not compared against a platform that scales idle functions to zero.
- **Concurrency inside one pod, warm invocations only.** Both differ from how a FaaS platform runs SeBS functions (section 3).
- **Compute-bound function only.** VPA observes CPU and memory, not time spent waiting on storage or the network, so these results do not carry over to I/O-heavy functions.
- **Memory demand sits near VPA's default floor.** Default memory recommendations are expected to be pinned at or modestly above 250Mi, which limits how much memory behaviour the default setting can show.
- **Only arrival times come from the trace.** The trace function's own work is unknown; it was chosen because its recorded duration matches the SeBS function.
- **Client and server share one machine** in the full runs, separated by NUMA node.

## 15. Future work

- Eight-day runs (48h each at `k = 4`), to exercise VPA's full 8-interval memory window.
- Other SeBS functions. The storage-backed ones (`210.thumbnailer`, `311.compression`, `220.video-processing`) would test how VPA behaves when part of the latency is I/O that it cannot observe or reduce.
- Hand-tuning on window B as well, to measure the transfer gap against B's own best.
- A larger tuning budget.
- Higher compression (`k = 12` or `k = 24`) with a validation run against `k = 4`, to quantify how much compression changes the conclusions.

## Appendix A. Open question: alternative stage order

Status: an open thought from the experimenter, to be discussed with collaborators. It is not part of the agreed design.

**Idea.** To save time, run the stages in this order:

1. Static (no VPA) first.
2. Tuned VPA next, choosing a good configuration directly from what the static run shows about the workload.
3. Default VPA last.

**How it differs from section 7.** The agreed procedure starts tuning from a default run and adjusts in response to what default gets wrong. In this alternative, tuning starts from the workload's measured demand in the static run, without first seeing default's behaviour.

**Points to discuss.**

- **What the time saving is.** The stages are the same; the saving comes from getting a tuned result earlier, and from possibly needing fewer tuning iterations if the static run gives enough information to pick a configuration directly.
- **Effect on H1.** The hypothesis compares tuned with default, so default runs are still needed before H1 can be tested. Running default last delays that test but does not remove it.
- **Effect on the tuning log.** The log currently records "what default got wrong, which knob fixed it". Under this order the rationale becomes "what the workload looks like, which configuration fits it". Both are hand-tuning, but they tell different stories about how much effort tuning takes.
- **Selection rule.** The rule in section 7 depends only on static p99, allocation and OOM kills, so it works unchanged in this order.
- **Risk.** A configuration chosen without seeing default may fix problems default does not have, or miss ones it does.
