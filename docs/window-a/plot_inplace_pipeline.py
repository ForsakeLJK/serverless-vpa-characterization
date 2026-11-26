"""Rebuild the Window A InPlace timing, outcome, and memory figure."""

import numpy as np
from analyze import HERE, ROOT, RUN_IDS, STYLE, finish_axes, series
from matplotlib import pyplot as plt
from matplotlib.ticker import NullLocator, StrMethodFormatter

from vpaexp.config import MI
from vpaexp.records import load_run


def timing_panels(delay, summary, runs):
    rows = [
        ["", "Default", "Tuned"],
        ["Median target → patch"],
        ["Median patch → applied"],
        ["Median target → applied"],
        ["Eventually applied"],
        ["Reported resize error"],
        ["Deferred"],
        ["Infeasible"],
    ]
    for run in runs:
        changes = run.resizes
        applied = [r for r in changes if r.outcome == "applied"]
        if not applied or not all(r.complete for r in applied):
            raise ValueError(f"Missing timestamps for {run.setting} resize timing")
        target_to_patch = np.array([r.patched_t - r.rec_updated_t for r in applied])
        patch_to_applied = np.array([r.applied_t - r.patched_t for r in applied])
        total = np.sort(target_to_patch + patch_to_applied)
        color, ls = STYLE[run.setting]
        pct = np.arange(1, len(total) + 1) * 100 / len(total)
        delay.step(np.r_[0.01, total], np.r_[0, pct], where="post",
                   color=color, ls=ls, lw=1.5, label=f"{run.setting.capitalize()} (n = {len(total)})")
        delay.plot(total[-1], 100, marker="o", ms=3.5, color=color)
        for row, values in zip(rows[1:4], (target_to_patch, patch_to_applied, total), strict=True):
            row.append(f"{1000 * np.median(values):.0f} ms")
        rows[4].append(f"{len(applied)}/{len(changes)}")
        for row, issue in zip(rows[5:], ("Error", "Deferred", "Infeasible"), strict=True):
            count = sum(issue in r.issues for r in changes)
            row.append(f"{count}/{len(changes)}")

    delay.set(xscale="log", xlim=(0.01, 500), ylim=(-2, 104), yticks=[0, 25, 50, 75, 100],
              xticks=[0.01, 0.1, 1, 10, 100], xticklabels=["0.01", "0.1", "1", "10", "100"],
              xlabel="Recommendation to applied (s, log scale)", ylabel="Cumulative share (%)")
    delay.xaxis.set_minor_locator(NullLocator())
    delay.legend(loc="lower right", fontsize=8)
    finish_axes(delay)

    summary.axis("off")
    table = summary.table(cellText=rows, colWidths=[0.6, 0.2, 0.2], cellLoc="right",
                          edges="open", bbox=[0, -0.14, 1, 1.16])
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    for (row, col), cell in table.get_celld().items():
        cell.PAD = 0.025
        if col == 0:
            cell.get_text().set_ha("left")
        if row == 0:
            cell.get_text().set_weight("bold")
        if row in (0, 3):
            cell.visible_edges = "B"
            cell.set_edgecolor("#a9b0b4")
            cell.set_linewidth(0.5)


def memory_panel(ax, run):
    # Take the maximum of the available one-second memory.current samples in
    # each minute. Leave minutes without any samples blank in the usage trace.
    seconds = run.samples.t - run.t0
    valid = (seconds >= 0) & (seconds < run.duration_s)
    edges = np.arange(0, run.duration_s + 60, 60)
    peak = np.full(len(edges) - 1, np.nan)
    np.fmax.at(peak, (seconds[valid] // 60).astype(int), run.samples.memory_bytes[valid] / MI)
    ax.stairs(peak, edges / 3600, fill=True, color="#aebdbd", lw=0, label="Usage (1-min maximum)")

    changes = [r for r in run.resizes if r.outcome == "applied"]
    applied_times = [0, *[r.applied_t - run.t0 for r in changes], run.duration_s]
    limits = [run.initial.memory_bytes, *[r.after.memory_bytes for r in changes]]
    ax.step(np.asarray(applied_times) / 3600, np.r_[limits, limits[-1]] / MI, where="post",
            color="#005a82", lw=1.65, label="Applied limit")
    t, target = series(run, "target")
    ax.step(t / 3600, target, where="post", color="#a94717", ls=(0, (3, 2)), lw=1.15,
            label="Recommended target")
    ax.set(xlim=(0, run.duration_s / 3600), ylim=(0, 2700), xticks=np.arange(0, 25, 3),
           yticks=[0, 1000, 2000], ylabel="Memory (MiB)")
    ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    finish_axes(ax)


def plot_pipeline(runs):
    fig = plt.figure(figsize=(7.1, 5.2))
    grid = fig.add_gridspec(3, 2, height_ratios=[1.38, 1, 1], width_ratios=[1.05, 1],
                            left=0.105, right=0.98, top=0.94, bottom=0.15,
                            wspace=0.24, hspace=0.72)
    delay, summary = fig.add_subplot(grid[0, 0]), fig.add_subplot(grid[0, 1])
    timing_panels(delay, summary, runs)
    default = fig.add_subplot(grid[1, :])
    tuned = fig.add_subplot(grid[2, :], sharex=default)
    for ax, run in zip((default, tuned), runs, strict=True):
        memory_panel(ax, run)
    # Mark a sustained target/limit difference on the longest tuned plateau.
    run = runs[1]
    previous, following = max(zip(run.resizes[:-1], run.resizes[1:], strict=True),
                              key=lambda pair: pair[1].patched_t - pair[0].applied_t)
    highest = max((r for r in run.recommendations if previous.applied_t <= r.t < following.patched_t),
                  key=lambda r: r.target)
    tuned.annotate(f"Target {highest.target / MI:,.0f} MiB, limit {previous.after.memory_bytes / MI:,.0f} MiB",
                   xy=((highest.t - run.t0) / 3600, highest.target / MI), xytext=(14, 2070),
                   fontsize=8, arrowprops=dict(arrowstyle="-", lw=0.65, color="#525b61"))
    for ax, title in zip((delay, summary, default, tuned),
                         ("(a)  End-to-end resize latency", "(b)  Resize timing and outcomes",
                          "(c)  Default VPA", "(d)  Tuned VPA"), strict=True):
        ax.set_title(title, loc="left", fontsize=10, weight="bold", pad=6)
    default.tick_params(labelbottom=False)
    tuned.set_xlabel("Time since replay start (h)")
    handles, labels = default.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.54, 0.004), ncol=3)
    return fig


if __name__ == "__main__":
    figure = plot_pipeline([load_run(ROOT / "results" / name) for name in RUN_IDS[1:]])
    for extension in ("pdf", "svg", "png"):
        figure.savefig(HERE / "figures" / f"inplace-pipeline.{extension}", dpi=240)
    plt.close(figure)
