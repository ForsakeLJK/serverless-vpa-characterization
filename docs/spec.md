# Experiment specification: Kubernetes VPA (InPlace) under a serverless workload

Status: agreed design, implemented in this repository. Written 2026-11-12, revised the same day to `k = 4`, four-day windows and a SeBS workload function. Revised 2026-11-16: VPA controls memory only and CPU is fixed and generous in every setting. Target venue: ISPASS 2027.

Items marked **[verify]** are assumptions, single measurements or derivations that must be confirmed before the full runs. They are collected in section 13.

## 1. Goal, hypothesis and research questions

Characterize the Kubernetes Vertical Pod Autoscaler, in InPlace update mode, on a single serverless function driven by real arrival times. Findings come from the results.

**Scope: memory only.** VPA controls the function's memory. Each comparison uses the same fixed CPU quota and NUMA placement. The experiment measures memory autoscaling behavior under that quota, with CPU throttling recorded as context. CPU autoscaling is future work (section 15).

**Design principle: default VPA must behave as it does in production.** Trace time is compressed by a factor `k`, and every time-based VPA setting is divided by exactly the same `k`, including the control-loop intervals. `k = 4` is the largest factor at which stock components allow this (section 6.2). The only delay that is not scaled is the kubelet's own resize work (section 14).

**Research questions.** Every metric and figure must serve one of these.

- **RQ1**: How well does the recommender track serverless memory demand (slack and under-provisioning over time)?
- **RQ2**: How long does the InPlace pipeline take from recommendation to pod patch to kubelet-applied, and how often does it fail or defer?
- **RQ3**: What is the net effect on tail latency and memory efficiency versus static allocation, and how sensitive is it to configuration?

**Hypothesis H1: best VPA performance requires hand-tuning.**

- Test: on window A, compare the default configuration with the best configuration found by hand-tuning on window A.
- Supported if the tuned configuration beats default on mean memory allocation or on p99 latency by more than the spread (min to max) across the 3 repetitions of each.
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
- **Why this function**: it is self-contained, and its memory grows with the input and with the number of requests in flight. Nothing else sits in the request path. Most other SeBS functions call object storage or a database, which adds latency that VPA can neither observe nor reduce.
- **Pinned versions**: SeBS commit `089e5c6ccece0b4d9508e08eee80cf3527590c4f` (2026-10-24), Python 3.12, igraph 0.11.4 (the version SeBS pins for Python 3.12).
- **Payload**: SeBS's `large` input, which is `size = 100000` vertices with `seed = 42`. It is identical for every request in every run, including the prototype. Each response can be checked against SeBS's expected value for that input.
- **What is borrowed from SeBS**: the function code, unmodified, and its input definition. SeBS's deployment tooling is not used, because its only Kubernetes route is OpenWhisk, which creates and destroys its own function containers and conflicts with a single pod resized in place.
- **Our packaging**: the function runs in our own container behind a thin HTTP wrapper, deployed as a plain Deployment with one replica and no horizontal scaling.
- **Deviations from how SeBS functions normally run**, both documented in the paper:
  - **Concurrency inside one pod**: on a FaaS platform each instance serves one request at a time. Here the wrapper runs one process per request, so the arrival rate drives memory (and CPU) within the single pod.
  - **Warm invocations only**: each request's process is forked from a parent that has already imported igraph. Cold starts are out of scope.
- **Concurrency cap**: 4× the peak concurrency from the sizing step for that window (section 6.1), fixed for all settings on that window. Requests beyond the cap queue in the server. The cap stops an overloaded pod from forking without bound; the factor 4 is a judgment call, not a derived value **[verify]**.
- **Resources**: requests equal limits for CPU and memory (Guaranteed QoS). Memory is the resource under study: every change to its allocation alters what the function can use. CPU uses the fixed quota in section 6.
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

Estimated CPU demand at `k = 4`, from per-minute counts and the 0.33 s Mac service time **[verify]**. CPU is not controlled or reported; the table only shows how bursty the load is and gives a lower bound for the fixed CPU (section 6). The prototype measured about 0.47 CPU-seconds per request against 0.30 s of wall time, so real CPU demand is roughly 1.6 times these figures:

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

All three settings use the same window, the same payload, the same initial memory allocation and the same fixed CPU.

**Fixed CPU.** The pod's CPU request and limit are set once per testbed in the cluster profile and are identical in every setting. The arm01 comparison uses 60000 millicores. CPU throttling is recorded for interpretation and is allowed in every setting.

### 6.1 Static (no VPA)

Static memory is sized for the window's peak demand, found by an offline calculation that a short replay then validates. This replaces a full 24h profiling replay per window.

**Offline calculation.** The workload is deterministic. The calculation estimates overlap from isolated request timings and uses the measured per-request memory to predict memory demand. Requests can slow each other down under load, so the estimates are checked in a replay under the same fixed CPU quota.

- the peak concurrency (most requests in flight at one instant);
- the highest memory usage;
- the moment at which each of these occurs.

**Validation replay.** The calculation does not model allocator overhead or the cost of forking, so it is checked against the real system. For each window, a 1h replay centred on the calculated memory peak runs with a generous memory limit, the fixed CPU and no VPA.

- Every request must succeed in both the one-hour validation replay and the full profiling replay. Request failures and timeouts during the comparison runs are experimental outcomes.
- Measured peak concurrency must be within 10% of the calculation. Measured peak memory may be lower than predicted and may exceed the prediction by up to 10%.
- A concurrency difference over 10% or a memory underprediction over 10% triggers a full 24h profiling replay.

**Sizing rule.**

- Static memory = the highest memory usage, plus 15%.
- CPU is not sized: it is the fixed value above.
- Where calculation and validation replay differ within the tolerance, the larger value is used.
- Computed separately for windows A and B.
- The same step yields the peak concurrency used for the concurrency cap.

**Check during the campaign.** If a static run shows an OOM kill, the sizing was too low; it is redone with a full profiling replay and the affected runs are repeated.

**Observed static peak.** Use the full static run's sampled memory peak to estimate a smaller fixed allocation after the run. Keep that estimate distinct from the allocation actually tested. Establish whether the smaller allocation is sufficient with a replay at that allocation.

### 6.2 VPA default

Upstream defaults, with every time-based setting divided by `k = 4`. Everything not listed is stock. The CPU flags are scaled like the rest for consistency, but have no effect on the pod, because the VPA object controls memory only (section 6.4).

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

**Scaling rule for other `k`.** A scaled duration is upstream divided by `k`, rounded to the nearest whole second (halves up) and never below 1s; `recommender-interval` and `updater-interval` are then raised to at least the 15s metrics resolution. At `k = 4` neither step changes any value. Each run records which flags departed from exact division and why. Only the rehearsal (section 10) uses another `k`.

### 6.3 VPA tuned

The configuration chosen by the procedure in section 7.

### 6.4 VPA object, both VPA settings

- `updateMode: InPlace`. No evict-and-recreate.
- `controlledResources: ["memory"]`. VPA neither recommends nor changes CPU.
- `controlledValues: RequestsAndLimits`, so the 1:1 memory request-to-limit ratio is preserved.
- `maxAllowed` (memory): node allocatable memory minus a fixed system reserve **[verify value]**, the same for both settings.
- `minReplicas: 1` in the update policy. The updater skips workloads with fewer replicas than its `--min-replicas` flag (default 2); the per-object setting lets it act on the single function pod without changing a component flag.
- Initial pod allocation: the static memory allocation for that window, and the fixed CPU.

## 7. Tuning procedure

Hand-tuning by diagnose-and-adjust, on window A only, using full four-day runs.

1. Start from the first default run on window A.
2. Record what went wrong (for example, memory pinned at the floor, or the memory target lagging bursts).
3. Change a knob from the declared set to address it, and run once.
4. Repeat, up to the budget.

Guardrails:

- **Declared knobs**: memory half-life, memory aggregation interval, memory confidence interval, memory target percentile, safety margin, the 250Mi floor, in-bounds lifetime threshold, update threshold. No CPU flag is a knob. Nothing else may change. Loop intervals are excluded, since 15s is already the practical floor.
- **Tuning log**: each iteration records the observation, the knob changed and why, and the result. The log is a paper artifact (figure 9).
- **Budget**: 3 iterations to start. It may be raised later if the log shows continued improvement.
- **Selection rule**, fixed before any tuning run. The goal of tuning is to avoid memory waste while keeping service quality.
  - **Service quality is kept** when neither p99 latency nor the failed-request rate (timeouts plus errors) is worse than static. The failed-request condition is needed because errors are not part of the latency percentiles. Static's own results vary from run to run, so "no worse than static" means no worse than the worst of the static repetitions on window A available when the rule is applied.
  - **The tuned configuration** is the iteration with the lowest mean allocated memory among those that keep service quality.
  - **OOM kills are expected to be few, not zero.** They do not disqualify an iteration; their cost to users is already limited by the two conditions above. Iterations whose mean allocated memory is within 5% of the lowest count as equal on memory, and among them the one with the fewest OOM kills is chosen.
  - If no iteration keeps service quality, take the one with the fewest failed requests and report that service quality was not kept.
  - The 5% is a judgment call **[verify]**.

An alternative ordering of the stages is recorded in Appendix A as an open question.

## 8. Metrics and collection

### 8.1 Metrics

| Metric | Definition | Source |
|---|---|---|
| p99 latency | Client-side, completion minus scheduled send time. Timeouts are included at the timeout value. | Client log |
| Throughput | Successful completions per second | Client log |
| Error rate | Timeouts and errors as a share of requests sent, reported separately | Client log |
| Memory usage | Per 1s sample | cgroup sampler |
| Memory utilization | Usage divided by current request | cgroup sampler, pod status |
| Memory allocation | Mean requested memory over the run | Pod status |
| VPA recommendation | Target, lower bound, upper bound over time | VPA object watch |
| Actuation lag | Three timestamps per resize: recommendation updated, pod patched, resize applied by the kubelet | Watch recorder, audit log |
| Resize count | Applied memory resizes, split by direction | Watch recorder |
| Failed or deferred resizes | Resizes reported as deferred, infeasible or errored | Pod status and events |
| OOM kills | Count | `memory.events`, and container restarts with reason OOMKilled |

Record the percentage of CPU quota periods with throttling and the cumulative throttled time from the container's `cpu.stat`. The percentage is the increase in `nr_throttled` divided by the increase in `nr_periods`, multiplied by 100. Throttled time accumulates across CPU run queues. Report these counters as context for the memory and latency outcomes.

Every metric is reported for the whole run and split into two phases: the first scaled day (cold start, hours 0–6) and scaled days 2–4 (warmed, hours 6–24).

### 8.2 Collectors

No Prometheus. Two purpose-built collectors:

- **Watch recorder**: watches the VPA object, pod spec and status, and events. The API server audit log is enabled and is the authoritative clock for "recommendation updated" and "pod patched".
- **cgroup sampler**: runs on the node and reads the container's `memory.current`, `memory.peak`, `memory.events`, and `cpu.stat` every 1s.

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

**Rehearsal.** Before the campaign, the whole run plan is executed once at `k = 96` with every 4th arrival kept, so each window replays in 1h with peak concurrency close to `k = 4` (19 and 32 against 17 and 25). It checks the campaign end to end and measures per-run overhead. Its results are not reported: VPA's loop intervals sit at the 15s floor, 24× slower than the design principle asks.

Per-run reset and collection overhead is extra and not yet measured. A window whose validation replay fails the 10% tolerance adds a 24h profiling replay. The first default run on A doubles as the starting observation for tuning.

## 11. Prototype gate

One 30-minute run on the Mac minikube cluster. The prototype only verifies the pipeline, so it does not follow the `k = 4` principle: it replays a busy 2h slice of day 2 at `k = 4`, with VPA windows shortened far below the `k = 4` values so that resizes occur within the run. It uses the same function and payload as the full runs. CPU throttling is reported. No full run starts until all five checks pass:

1. At least one memory resize up and one memory resize down.
2. CPU is never resized: the pod's CPU request and limit stay at the fixed value for the whole run.
3. All three lag timestamps captured for every resize. A resize the kubelet refused or deferred and never applied is normal behavior: it is reported, and needs no applied timestamp.
4. At a 15s metrics resolution, samples from the metrics API are confirmed to refresh every 15s (by polling the pod's `timestamp` and `window` fields under load).
5. Figures 1–6 render from the collected data.

## 12. Figures

1. Server throughput over time as a line chart with two lines per 1-minute bucket: offered load (the request rate of the load) and achieved throughput (the rate of successful requests).
2. Memory over time: usage, request, VPA target with lower and upper bounds.
3. Memory utilization over time: usage in percent of the allocation in force.
4. Latency over time as a line chart (p50, p95 and p99 per 1-minute bucket) with resize events marked.
5. Request outcomes over time: requests per 1-minute bucket that completed, timed out or errored, stacked.
6. Lag breakdown per resize as a stacked bar: recommendation updated to pod patched, pod patched to kubelet applied.

Figures 1–5 mark every OOM kill. Figures are rendered at 300 dpi, and every run also gets an overview page with figures 1–6 together.

7. Summary bars with median and range over repetitions, for the whole run and per phase: p99, throughput, error rate, mean allocated memory, mean memory utilization, resize count, failed or deferred resizes, OOM kills.
8. Transfer: figure 7 for window B.
9. Tuning log table: iteration, observation, knob changed, result.

## 13. Items to verify before full runs

- **Payload cost**: re-measure pagerank's service time and memory per invocation at the `large` size on the ARM server, in a container. The duration match with the trace function and all demand estimates in section 4 depend on it.
- **Fixed CPU and NUMA placement**: confirm that the quota and placement are identical for all comparison runs.
- **Concurrency cap factor**: 4× is a judgment call. Consider one run at a different factor to show the results do not hinge on it.
- **Metrics refresh at 15s**: prototype gate check 4.
- **System reserve** (memory) for `maxAllowed`: value not yet chosen.
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
- **Memory only.** The comparison measures memory autoscaling under the testbed's fixed CPU quota. Memory settings can affect latency through changes in request overlap and CPU throttling, as well as OOM kills and restarts.
- **One function, one pod, one payload, one trace function.** Results do not cover horizontal scaling, multi-tenancy or other workload shapes.
- **No scale-to-zero.** The pod stays alive through idle periods, which are most of each run. Idle-time cost is not compared against a platform that scales idle functions to zero.
- **Concurrency inside one pod, warm invocations only.** Both differ from how a FaaS platform runs SeBS functions (section 3).
- **Compute-bound function only.** Nothing in the request path waits on storage or the network, so these results do not carry over to I/O-heavy functions.
- **Memory demand sits near VPA's default floor.** Default memory recommendations are expected to be pinned at or modestly above 250Mi, which limits how much memory behaviour the default setting can show.
- **Only arrival times come from the trace.** The trace function's own work is unknown; it was chosen because its recorded duration matches the SeBS function.
- **Client and server share one machine** in the full runs, separated by NUMA node.

## 15. Future work

- CPU autoscaling: let VPA control CPU as well. It needs a definition of CPU demand that accounts for latency, and a measured CPU cost per request.
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
- **Selection rule.** The rule in section 7 depends only on the static run's p99 and failed-request rate, and on each iteration's memory allocation and OOM kills, so it works unchanged in this order.
- **Risk.** A configuration chosen without seeing default may fix problems default does not have, or miss ones it does.
