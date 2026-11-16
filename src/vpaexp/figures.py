"""Figures 1-9 (spec section 12)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from vpaexp.metrics import Summary, aggregate, allocation_steps, latencies
from vpaexp.records import Recommendation, Run
from vpaexp.sizing import MI

BUCKET_S = 60.0


def _axis(run: Run) -> tuple[float, str]:
    return (3600.0, "time since replay start (h)") if run.duration_s > 7200 else (60.0, "time since replay start (min)")


def _save(fig: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def _mark_resizes(ax: Any, run: Run, unit: float) -> None:
    for i, r in enumerate(x for x in run.resizes if x.outcome == "applied"):
        ax.axvline((r.patched_t - run.t0) / unit, color="0.6", lw=0.6, ls=":", label="resize" if i == 0 else None)


def fig1_arrival_rate(run: Run, path: Path) -> Path:
    unit, xlabel = _axis(run)
    edges = np.arange(0, run.duration_s + BUCKET_S, BUCKET_S)
    counts, _ = np.histogram(run.client.scheduled - run.t0, edges)
    fig, ax = plt.subplots(figsize=(8, 3))
    ax.step(edges[:-1] / unit, counts / BUCKET_S, where="post", lw=0.8)
    ax.set(xlabel=xlabel, ylabel="arrival rate (rps, 1-min buckets)", title=f"Arrivals, window {run.meta.get('window')}")
    return _save(fig, path)


def _resource_figure(run: Run, path: Path, resource: str) -> Path:
    unit, xlabel = _axis(run)
    cpu = resource == "cpu"
    scale = 1.0 if cpu else float(MI)
    s = run.samples
    usage = s.cpu_cores if cpu else s.memory_bytes / scale
    times, cpu_m, mem = allocation_steps(run)
    request = cpu_m / 1000 if cpu else mem / scale
    end = run.t0 + run.duration_s
    times = np.append(np.maximum(times, run.t0), end)

    def series(recs: list[Recommendation], which: str) -> tuple[list[float], list[float]]:
        ts = [(r.t - run.t0) / unit for r in recs] + [run.duration_s / unit]
        vals = [getattr(getattr(r, which), "cpu_millicores") / 1000 if cpu else getattr(r, which).memory_bytes / scale
                for r in recs]
        return ts, vals + vals[-1:]

    fig, ax = plt.subplots(figsize=(8, 3.5))
    ax.plot((s.t - run.t0) / unit, usage, lw=0.5, color="C0", label="usage (1s)")
    ax.step((times - run.t0) / unit, np.append(request, request[-1]), where="post", color="k", lw=1.2, label="request")
    if run.recommendations:
        ts, target = series(run.recommendations, "target")
        ax.step(ts, target, where="post", color="C1", lw=1.0, label="VPA target")
        _, lower = series(run.recommendations, "lower")
        _, upper = series(run.recommendations, "upper")
        ax.fill_between(ts, lower, upper, step="post", color="C1", alpha=0.15, label="lower to upper bound")
        ax.set_ylim(0, 1.3 * max(float(np.max(request)), float(np.max(target)), float(np.max(usage, initial=0))))
    ax.set(xlabel=xlabel, ylabel="CPU (cores)" if cpu else "memory (MiB)",
           title=f"{'CPU' if cpu else 'Memory'}: {run.meta.get('setting')}, window {run.meta.get('window')}")
    ax.legend(fontsize=7, loc="upper right")
    return _save(fig, path)


def fig2_cpu(run: Run, path: Path) -> Path:
    return _resource_figure(run, path, "cpu")


def fig3_memory(run: Run, path: Path) -> Path:
    return _resource_figure(run, path, "memory")


def fig4_latency(run: Run, path: Path) -> Path:
    unit, xlabel = _axis(run)
    when, latency = latencies(run)
    bucket = ((when - run.t0) // BUCKET_S).astype(int)
    xs, p50, p99 = [], [], []
    for b in np.unique(bucket):
        values = latency[bucket == b]
        xs.append(b * BUCKET_S / unit)
        p50.append(np.percentile(values, 50))
        p99.append(np.percentile(values, 99))
    fig, ax = plt.subplots(figsize=(8, 3.5))
    ax.plot(xs, p99, ".", ms=2, color="C3", label="p99")
    ax.plot(xs, p50, ".", ms=2, color="C0", label="p50")
    _mark_resizes(ax, run, unit)
    ax.set(xlabel=xlabel, ylabel="latency (s, 1-min buckets)", yscale="log",
           title=f"Latency: {run.meta.get('setting')}, window {run.meta.get('window')}")
    ax.legend(fontsize=7)
    return _save(fig, path)


def fig5_latency_cdf(runs: list[Run], path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(5, 3.5))
    for run in runs:
        _, latency = latencies(run)
        ordered = np.sort(latency)
        ax.plot(ordered, np.arange(1, len(ordered) + 1) / max(len(ordered), 1), label=str(run.meta.get("setting")))
    ax.set(xlabel="latency (s)", ylabel="fraction of requests", xscale="log", title="Latency CDF")
    ax.legend(fontsize=7)
    return _save(fig, path)


def fig6_lag(run: Run, path: Path) -> Path:
    done = [r for r in run.resizes if r.complete]
    to_patch = [r.patched_t - (r.rec_updated_t or r.patched_t) for r in done]
    to_apply = [(r.applied_t or r.patched_t) - r.patched_t for r in done]
    xs = np.arange(len(done))
    fig, ax = plt.subplots(figsize=(8, 3.5))
    ax.bar(xs, to_patch, label="recommendation updated to pod patched")
    ax.bar(xs, to_apply, bottom=to_patch, label="pod patched to kubelet applied")
    ax.set_xticks(xs, [str(i + 1) for i in xs])
    ax.set(xlabel="resize (in order)", ylabel="lag (s)", title=f"Actuation lag, {len(done)} resizes")
    ax.legend(fontsize=7)
    return _save(fig, path)


def run_figures(run: Run, out_dir: Path, compare: list[Run] | None = None) -> list[Path]:
    """Figures 1-6 for one run. Figure 5 overlays `compare` (other settings) when given."""
    return [
        fig1_arrival_rate(run, out_dir / "fig1-arrival-rate.png"),
        fig2_cpu(run, out_dir / "fig2-cpu.png"),
        fig3_memory(run, out_dir / "fig3-memory.png"),
        fig4_latency(run, out_dir / "fig4-latency.png"),
        fig5_latency_cdf([run] + (compare or []), out_dir / "fig5-latency-cdf.png"),
        fig6_lag(run, out_dir / "fig6-lag.png"),
    ]


SUMMARY_PANELS = [
    ("p99_s", "p99 latency (s)", 1.0),
    ("throughput_rps", "throughput (rps)", 1.0),
    ("timeout_rate", "timeouts (% of sent)", 100.0),
    ("error_rate", "errors (% of sent)", 100.0),
    ("alloc_cpu_m", "mean allocated CPU (cores)", 1e-3),
    ("alloc_mem_bytes", "mean allocated memory (MiB)", 1 / MI),
    ("util_cpu", "mean CPU utilization (%)", 100.0),
    ("util_mem", "mean memory utilization (%)", 100.0),
    ("resizes", "applied resizes", 1.0),
    ("resizes_failed_or_deferred", "failed or deferred resizes", 1.0),
    ("oom_kills", "OOM kills", 1.0),
    ("throttled_s", "throttled time (s)", 1.0),
]


def fig7_summary(by_setting: dict[str, list[dict[str, Summary]]], path: Path, title: str) -> Path:
    """Summary bars: median with min-to-max range over repetitions, whole run and per phase.

    `by_setting` maps a setting to one {phase: summary} per repetition. Also used for figure 8.
    """
    settings = list(by_setting)
    phase_names = list(next(iter(by_setting.values()))[0])
    fig, axes = plt.subplots(3, 4, figsize=(15, 9))
    width = 0.8 / len(settings)
    for ax, (key, label, factor) in zip(axes.flat, SUMMARY_PANELS):
        for i, setting in enumerate(settings):
            agg = [aggregate([rep[ph] for rep in by_setting[setting]])[key] for ph in phase_names]
            med = np.array([a["median"] for a in agg]) * factor
            lo = np.array([a["min"] for a in agg]) * factor
            hi = np.array([a["max"] for a in agg]) * factor
            xs = np.arange(len(phase_names)) + i * width
            ax.bar(xs, med, width, yerr=[med - lo, hi - med], capsize=2, label=setting)
        ax.set_xticks(np.arange(len(phase_names)) + 0.4 - width / 2, phase_names)
        ax.set_title(label, fontsize=9)
    axes.flat[0].legend(fontsize=7)
    fig.suptitle(title)
    return _save(fig, path)


def fig9_tuning_log(rows: list[dict[str, str]], path: Path) -> Path:
    """Tuning log table: iteration, observation, knob changed, result."""
    cols = ["iteration", "observation", "knob changed", "result"]
    cells = [[r.get("name", ""), r.get("observation", ""), r.get("knob", ""), r.get("result", "")] for r in rows]
    fig, ax = plt.subplots(figsize=(11, 1 + 0.6 * max(len(rows), 1)))
    ax.axis("off")
    table = ax.table(cellText=cells or [[""] * 4], colLabels=cols, loc="center", cellLoc="left",
                     colWidths=[0.1, 0.4, 0.2, 0.3])
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    table.scale(1, 1.6)
    return _save(fig, path)
