# Kubernetes VPA Characterization on Serverless Functions 

The current experiment compares fixed memory allocation with two configurations of the Kubernetes Vertical Pod Autoscaler (VPA) under the same [Azure Functions](https://github.com/Azure/AzurePublicDataset/blob/master/AzureFunctionsInvocationTrace2021.md) arrival trace, with PageRank function selected from [SeBS benchmark](https://github.com/spcl/serverless-benchmarks). Follow steps 1 through 4 to reproduce the results. With the archived runs, start at step 4. Steps 5 through 7 extend the comparison to three repetitions per setting on two trace windows.

Run commands from the repository root. Run each replay on an idle node and wait for it to finish before starting the next.

## Declaration of Generative AI Usage

Claude Opus and GPT Astra were used to implement the codebase.

## 1. Prepare the cluster

On a fresh ARM server with two NUMA nodes, install Kubernetes and VPA and build the function image. Use the checks printed by the setup script to verify NUMA placement, allocatable CPU and audit logging.

```bash
scripts/setup-fullrun.sh
```

## 2. Measure request cost and size window A

Measure service time and memory per request on the experiment node. The sizing command uses these measurements to calculate a static memory allocation and concurrency cap. It checks the prediction by replaying one hour around the expected memory peak.

```bash
uv run vpaexp --profile full measure
uv run vpaexp --profile full size --window A --validate
```

If validation fails the sizing tolerance check, rerun the sizing command with `--full-profile` in place of `--validate` to measure the peaks over the full window.

## 3. Replay the three window A runs

First, check that each setting can serve the first ten minutes of the trace. The tuned smoke test uses the example configuration.

```bash
uv run vpaexp --profile full run --setting static  --window A --limit-s 600
uv run vpaexp --profile full run --setting default --window A --limit-s 600
uv run vpaexp --profile full run --setting tuned   --window A --limit-s 600 --config tuning/example.toml
```

For the reported comparison, replay all four trace days in 24 hours per run. The static baseline holds memory fixed. Default VPA adjusts memory in place. The tuned configuration (`it2`) shortens the memory aggregation interval from six hours to five minutes.

```bash
uv run vpaexp --profile full run --setting default --window A --rep 1
uv run vpaexp --profile full run --setting static  --window A --rep 1
uv run vpaexp --profile full run --setting tuned   --window A --label it2 --config tuning/it2.toml
```

## 4. Regenerate the window A measurements and figures

The analysis scripts read the three run directories listed in `RUN_IDS` in `docs/window-a/analyze.py`, relative to `results/`. To analyze a fresh replay, set those entries to the new paths under `full/runs/` in static, default, tuned order. The scripts write their plots to `docs/window-a/figures/`.

```bash
uv run python docs/window-a/analyze.py                # Print the measurements and redraw the tracking, latency and burst plots.
uv run python docs/window-a/plot_startup.py           # Plot VPA startup for default and it2.
uv run python docs/window-a/plot_inplace_pipeline.py  # Plot memory recommendations, pod requests and applied limits.
uv run python docs/window-a/plot_resize_latency.py    # Plot the distribution of resize delays.
```

To redraw figures 1 through 6 and the overview for an individual run, pass its directory. This example uses the archived default run.

```bash
uv run vpaexp figures results/A-default-r1-20261121-124656
```

## 5. Select a configuration for the repeated comparison

Run the second candidate, `it1`, which raises the minimum memory recommendation to 2048 MB. The selection command compares both candidates against the window A static baseline using p99 latency and failed-request rate, then records its choice in `results/full/report/tuning-selection.json`.

```bash
uv run vpaexp --profile full run --setting tuned --window A --label it1 --config tuning/it1.toml
uv run vpaexp --profile full tune-select
```

## 6. Complete three repetitions per setting on each window

Size window B using the same validation procedure as in step 2.

```bash
uv run vpaexp --profile full size --window B --validate
```

Reuse the first default and static repetitions from step 3. Run three repetitions of the selected tuned configuration on each window. Using the same configuration on B measures how well the tuning transfers to a different arrival pattern.

```bash
(
  set -e
  chosen=$(uv run python -c 'import json; print(json.load(open("results/full/report/tuning-selection.json"))["chosen"])')
  for window in A B; do
    baseline_reps=(1 2 3)
    if [[ "$window" == A ]]; then baseline_reps=(2 3); fi
    for rep in "${baseline_reps[@]}"; do
      uv run vpaexp --profile full run --setting default --window "$window" --rep "$rep"
      uv run vpaexp --profile full run --setting static  --window "$window" --rep "$rep"
    done
    for rep in 1 2 3; do
      uv run vpaexp --profile full run --setting tuned --window "$window" --rep "$rep" --config "tuning/$chosen.toml"
    done
  done
)
```

## 7. Summarize the repeated runs

```bash
uv run vpaexp --profile full report --window A   # Compare the settings and test H1 on memory allocation and p99 latency.
uv run vpaexp --profile full report --window B   # Compare how the configuration selected on A performs on B.
uv run vpaexp tuning-log                         # Draw figure 9 from the observations in tuning/log.toml.
```

## Rehearse a window in one hour (optional)

After step 2, use `--k 96 --keep-every 4` to replay every fourth arrival across four trace days in one hour. Use these options for each rehearsal command, including tuning selection and reporting. Keep rehearsal tuning files separate because their durations depend on replay speed.

```bash
uv run vpaexp --profile full --k 96 --keep-every 4 size --window A --validate
uv run vpaexp --profile full --k 96 --keep-every 4 run --setting default --window A --rep 1
uv run vpaexp --profile full --k 96 --keep-every 4 report --window A
```

## RQ4: two functions sharing 6.5 GiB on ARM

Use the checkout and `--results` root containing the measured payload cost and both window A sizing files. The examples use `results-window-a/full/`. Run sequentially on the dedicated node. Each function requests and is limited to 30 CPUs. The target and neighbor use separate Services (NodePorts 30080 and 30081), VPA objects and cgroup samplers. The target alone also uses 30 CPUs in RQ4.

First run a ten-minute pilot with the original node memory configuration. It resets the prior experiment, checks the available CPU, replays both traces from a common clock and removes the experiment pods when finished.

```bash
uv run vpaexp --profile full --results results-window-a rq4 --ample-memory --limit-s 600
```

On the kubeadm node, inspect the proposed reservation and apply it between runs. The helper uses the full profile's kubectl context, `sudo`, `flock` and the system Python's PyYAML (`python3-yaml` on Ubuntu). It backs up `/var/lib/kubelet/config.yaml`, changes `systemReserved.memory`, and restarts kubelet. For this request-capacity experiment it sets `enforceNodeAllocatable: [none]` so the reservation affects advertised allocatable without imposing a small aggregate physical memory limit on the control plane. Individual container limits remain enforced. The helper verifies the live capacity and restores the configuration if verification fails.

```bash
scripts/rq4-budget.sh plan
scripts/rq4-budget.sh apply
uv run vpaexp --profile full --results results-window-a rq4 --limit-s 600
uv run vpaexp --profile full --results results-window-a rq4 --rep 1
```

Every constrained run checks for exactly 6,656 MiB after other pods and samplers. Samplers request 128 MiB in total for every condition. Each VPA ceiling permits the entire workload budget. The full replay defaults to a default-VPA target and a present default-VPA neighbor. Use `--setting static` for the static target and `--neighbor absent` for isolated controls. Use the same budget and initial sizing in all four conditions. The 60-CPU runs are calibration evidence.

Outputs are under `results-window-a/full/rq4/`, with separate run, client, watch, audit, sampler and summary files per function. The parent directory includes initial resource accounting and a node snapshot every minute. After the campaign, restore the original node configuration:

```bash
scripts/rq4-budget.sh restore
```
