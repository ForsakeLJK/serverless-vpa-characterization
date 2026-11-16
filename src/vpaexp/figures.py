"""Figures 1-9 (spec section 12)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from vpaexp.metrics import Summary, aggregate, allocation_steps, latencies, memory_utilization, oom_kill_times
from vpaexp.records import Resize, Run
from vpaexp.sizing import MI
from vpaexp.trace import Floats

BUCKET_S = 60.0
DPI = 300
OK, TIMEOUT, ERROR, OOM = "#2a9d5c", "#e69f00", "#c0392b", "#d0021b"


def _axis(run: Run) -> tuple[float, str]:
    return (3600.0, "time since replay start (h)") if run.duration_s > 7200 else (60.0, "time since replay start (min)")


def _save(fig: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


def _mark_resizes(ax: Any, run: Run, unit: float) -> None:
    for i, r in enumerate(x for x in run.resizes if x.outcome == "applied"):
        ax.axvline((r.patched_t - run.t0) / unit, color="0.6", lw=0.6, ls=":", label="resize" if i == 0 else None)


def _mark_oom_kills(ax: Any, run: Run, unit: float) -> None:
    """A red line down the plot and a marker on its top edge for every OOM kill."""
    xs = (oom_kill_times(run) - run.t0) / unit
    if not len(xs):
        return
    for x in xs:
        ax.axvline(x, color=OOM, lw=0.7, alpha=0.45)
    ax.plot(xs, np.ones(len(xs)), "v", color=OOM, ms=5, ls="none", clip_on=False, zorder=5,
            transform=ax.get_xaxis_transform(), label=f"OOM kill ({len(xs)})")


def fig1_throughput(run: Run, path: Path) -> Path:
    """Offered load (requests scheduled) and achieved throughput (successful completions), per 1-minute bucket."""
    unit, xlabel = _axis(run)
    edges = _buckets(run)
    c = run.client
    offered, _ = np.histogram(c.scheduled - run.t0, edges)
    achieved, _ = np.histogram(c.completed[c.status == "ok"] - run.t0, edges)
    mid = (edges[:-1] + BUCKET_S / 2) / unit
    fig, ax = plt.subplots(figsize=(8, 3.5))
    ax.plot(mid, offered / BUCKET_S, lw=1.1, color="0.35", label="offered load")
    ax.plot(mid, achieved / BUCKET_S, lw=1.1, color=OK, label="achieved throughput (successful)")
    _mark_oom_kills(ax, run, unit)
    ax.set_ylim(0, 1.25 * max(float(np.max(offered, initial=1)), float(np.max(achieved, initial=1))) / BUCKET_S)
    ax.set(xlabel=xlabel, ylabel="requests per second (1-min buckets)",
           title=f"Throughput: {run.meta.get('setting')}, window {run.meta.get('window')}")
    ax.legend(fontsize=7, loc="upper right", ncols=3)
    return _save(fig, path)


def fig2_memory(run: Run, path: Path) -> Path:
    """Memory over time: usage, request, and the VPA target with its lower and upper bounds."""
    unit, xlabel = _axis(run)
    s = run.samples
    usage = s.memory_bytes / MI
    times, mem = allocation_steps(run)
    request = mem / MI
    times = np.append(np.maximum(times, run.t0), run.t0 + run.duration_s)
    recs = run.recommendations

    def series(which: str) -> list[float]:
        vals = [getattr(r, which) / MI for r in recs]
        return vals + vals[-1:]

    fig, ax = plt.subplots(figsize=(8, 3.5))
    ax.plot((s.t - run.t0) / unit, usage, lw=0.5, color="C0", label="usage (1s)")
    ax.step((times - run.t0) / unit, np.append(request, request[-1]), where="post", color="k", lw=1.2, label="request")
    if recs:
        ts = [(r.t - run.t0) / unit for r in recs] + [run.duration_s / unit]
        target = series("target")
        ax.step(ts, target, where="post", color="C1", lw=1.0, label="VPA target")
        ax.fill_between(ts, series("lower"), series("upper"), step="post", color="C1", alpha=0.15, label="lower to upper bound")
        ax.set_ylim(0, 1.3 * max(float(np.max(request)), max(target), float(np.max(usage, initial=0))))
    ax.set(xlabel=xlabel, ylabel="memory (MiB)",
           title=f"Memory: {run.meta.get('setting')}, window {run.meta.get('window')}")
    _mark_oom_kills(ax, run, unit)
    ax.legend(fontsize=7, loc="upper right")
    return _save(fig, path)


def fig3_memory_utilization(run: Run, path: Path) -> Path:
    """Memory usage as a percentage of the allocation in force: every 1s sample, and the 1-minute mean."""
    unit, xlabel = _axis(run)
    percent = 100 * memory_utilization(run)
    t = run.samples.t - run.t0
    edges = _buckets(run)
    bucket = np.digitize(t, edges) - 1
    mean = np.full(len(edges) - 1, np.nan)
    for b in np.unique(bucket[(bucket >= 0) & (bucket < len(edges) - 1)]):
        mean[b] = np.mean(percent[bucket == b])
    fig, ax = plt.subplots(figsize=(8, 3.5))
    ax.plot(t / unit, percent, lw=0.4, color="C0", alpha=0.45, label="1s samples")
    ax.step(edges / unit, np.append(mean, mean[-1:]), where="post", lw=1.3, color="C0", label="1-min mean")
    ax.axhline(100, color="0.4", lw=0.7, ls="--", label="allocation")
    _mark_resizes(ax, run, unit)
    _mark_oom_kills(ax, run, unit)
    overall = float(np.mean(percent)) if len(percent) else float("nan")
    # A sample that straddles a resize can read far above 100% of the new, smaller allocation;
    # the axis stops at 150% so that those few samples do not flatten everything else.
    ax.set_ylim(0, 150)
    ax.set(xlabel=xlabel, ylabel="memory utilization (% of allocation)",
           title=f"Memory utilization: {run.meta.get('setting')}, window {run.meta.get('window')} (mean {overall:.0f}%)")
    ax.legend(fontsize=7, loc="upper right", ncols=5)
    return _save(fig, path)


def _buckets(run: Run) -> Floats:
    return np.arange(0, np.ceil(run.duration_s / BUCKET_S) + 1) * BUCKET_S


def fig4_latency(run: Run, path: Path) -> Path:
    """p50, p95 and p99 latency per 1-minute bucket, as lines; a bucket with no requests breaks the line."""
    unit, xlabel = _axis(run)
    when, latency = latencies(run)
    edges = _buckets(run)
    bucket = np.digitize(when - run.t0, edges) - 1
    quantiles = np.full((3, len(edges) - 1), np.nan)
    for b in np.unique(bucket[(bucket >= 0) & (bucket < len(edges) - 1)]):
        quantiles[:, b] = np.percentile(latency[bucket == b], [50, 95, 99])
    mid = (edges[:-1] + BUCKET_S / 2) / unit
    fig, ax = plt.subplots(figsize=(8, 3.5))
    for values, label, color in zip(quantiles, ("p50", "p95", "p99"), ("C0", "C1", "C3")):
        ax.plot(mid, values, lw=1.1, color=color, label=label)
    ax.axhline(run.timeout_s, color="0.4", lw=0.7, ls="--", label="timeout")
    _mark_resizes(ax, run, unit)
    _mark_oom_kills(ax, run, unit)
    ax.set_ylim(0, 1.08 * run.timeout_s)
    ax.set(xlabel=xlabel, ylabel="latency (s, 1-min buckets)",
           title=f"Latency: {run.meta.get('setting')}, window {run.meta.get('window')}")
    ax.legend(fontsize=7, ncols=3, loc="lower right")
    return _save(fig, path)


def fig5_outcomes(run: Run, path: Path) -> Path:
    """Requests per 1-minute bucket by outcome (ok, timeout, error), stacked, by scheduled time."""
    unit, xlabel = _axis(run)
    edges = _buckets(run)
    status = run.client.status
    total = max(len(status), 1)
    fig, ax = plt.subplots(figsize=(8, 3.5))
    bottom = np.zeros(len(edges) - 1)
    for name, color in (("ok", OK), ("timeout", TIMEOUT), ("error", ERROR)):
        counts, _ = np.histogram(run.client.scheduled[status == name] - run.t0, edges)
        n = int(np.sum(status == name))
        ax.bar(edges[:-1] / unit, counts, width=BUCKET_S / unit, bottom=bottom, align="edge", color=color,
               linewidth=0, label=f"{name}: {n:,} ({100 * n / total:.1f}%)")
        bottom += counts
    _mark_oom_kills(ax, run, unit)
    ax.set_ylim(0, 1.3 * float(np.max(bottom, initial=1.0)))  # headroom for the legend
    ax.set(xlabel=xlabel, ylabel="requests per minute",
           title=f"Request outcomes: {run.meta.get('setting')}, window {run.meta.get('window')}")
    ax.legend(fontsize=7, loc="upper right", ncols=4)
    return _save(fig, path)


def _resize_label(i: int, r: Resize) -> str:
    def arrow(before: int, after: int) -> str:
        return "\u2191" if after > before else "\u2193" if after < before else ""

    return f"{i}\nmem{arrow(r.before.memory_bytes, r.after.memory_bytes)}"


def fig6_lag(run: Run, path: Path) -> Path:
    """Actuation lag of every applied resize as a stacked bar of its two stages.

    The kubelet stage is usually a few hundred times shorter than the first, so it is also
    written above each bar in milliseconds.
    """
    done = [(i, r) for i, r in enumerate(run.resizes, 1) if r.complete]
    to_patch = np.array([r.patched_t - (r.rec_updated_t or r.patched_t) for _, r in done])
    to_apply = np.array([(r.applied_t or r.patched_t) - r.patched_t for _, r in done])
    xs = np.arange(len(done))
    fig, ax = plt.subplots(figsize=(max(8.0, 0.55 * len(done) + 2), 4))
    ax.bar(xs, to_patch, 0.7, color="C0", label="recommendation updated \u2192 pod patched")
    ax.bar(xs, to_apply, 0.7, bottom=to_patch, color="C1", label="pod patched \u2192 kubelet applied")
    top = float(np.max(to_patch + to_apply, initial=1.0))
    for x, a, b in zip(xs, to_patch, to_apply):
        if a > 0.12 * top:
            ax.text(float(x), a / 2, f"{a:.1f} s", ha="center", va="center", fontsize=6.5, color="white", rotation=90)
        ax.text(float(x), a + b + 0.015 * top, f"{b * 1000:.0f} ms", ha="center", va="bottom", fontsize=6.5, color="C1")
    ax.set_xticks(xs, [_resize_label(i, r) for i, r in done], fontsize=7)
    ax.set_ylim(0, 1.25 * top)
    skipped = len(run.resizes) - len(done)
    note = f" ({skipped} not applied, not shown)" if skipped else ""
    ax.set(xlabel="resize (in order) and what it changed", ylabel="lag (s)",
           title=f"Actuation lag breakdown, {len(done)} resizes{note}")
    ax.legend(fontsize=7, loc="upper right", ncols=2)
    return _save(fig, path)


def overview(panels: list[Path], path: Path, title: str) -> Path:
    """All of a run's figures on one page, two per row, composed from the rendered files."""
    rows = (len(panels) + 1) // 2
    fig, axes = plt.subplots(rows, 2, figsize=(16, 3.6 * rows + 0.4))
    for ax in axes.flat:
        ax.axis("off")
    for ax, panel in zip(axes.flat, panels):
        ax.imshow(plt.imread(panel), interpolation="lanczos")
    fig.suptitle(title, fontsize=13)
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1 - 0.4 / (3.6 * rows + 0.4), wspace=0, hspace=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


def run_figures(run: Run, out_dir: Path) -> list[Path]:
    """Figures 1-6 for one run, then an overview page with all of them together."""
    panels = [
        fig1_throughput(run, out_dir / "fig1-throughput.png"),
        fig2_memory(run, out_dir / "fig2-memory.png"),
        fig3_memory_utilization(run, out_dir / "fig3-memory-utilization.png"),
        fig4_latency(run, out_dir / "fig4-latency.png"),
        fig5_outcomes(run, out_dir / "fig5-outcomes.png"),
        fig6_lag(run, out_dir / "fig6-lag.png"),
    ]
    throughput, memory, utilization, latency, outcomes, lag = panels
    title = f"{run.meta.get('setting')}, window {run.meta.get('window')}, {run.path.name}"
    # On the overview, memory sits beside its utilization.
    rows = [throughput, outcomes, memory, utilization, latency, lag]
    return panels + [overview(rows, out_dir / "overview.png", title)]


SUMMARY_PANELS = [
    ("p99_s", "p99 latency (s)", 1.0),
    ("throughput_rps", "throughput (rps)", 1.0),
    ("timeout_rate", "timeouts (% of sent)", 100.0),
    ("error_rate", "errors (% of sent)", 100.0),
    ("alloc_mem_bytes", "mean allocated memory (MiB)", 1 / MI),
    ("util_mem", "mean memory utilization (%)", 100.0),
    ("resizes", "applied resizes", 1.0),
    ("resizes_failed_or_deferred", "failed or deferred resizes", 1.0),
    ("oom_kills", "OOM kills", 1.0),
]


def fig7_summary(by_setting: dict[str, list[dict[str, Summary]]], path: Path, title: str) -> Path:
    """Summary bars: median with min-to-max range over repetitions, whole run and per phase.

    `by_setting` maps a setting to one {phase: summary} per repetition. Also used for figure 8.
    """
    settings = list(by_setting)
    phase_names = list(next(iter(by_setting.values()))[0])
    fig, axes = plt.subplots(3, 3, figsize=(12, 9))
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
