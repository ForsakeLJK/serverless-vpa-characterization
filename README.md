# Reproducing the Kubernetes VPA experiments

The window A experiment compares fixed memory allocation with two configurations of the Kubernetes Vertical Pod Autoscaler (VPA) under the same Azure Functions arrival trace. Follow steps 1 through 4 to reproduce the results. With the archived runs, start at step 4. Steps 5 through 7 extend the comparison to three repetitions per setting on two trace windows.

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
