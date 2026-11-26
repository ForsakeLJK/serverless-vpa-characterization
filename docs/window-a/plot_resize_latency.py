"""Plot the empirical CDF of recommendation-to-application latency in Window A."""

import numpy as np
from analyze import HERE, ROOT, RUN_IDS, STYLE, finish_axes
from matplotlib import pyplot as plt
from matplotlib.ticker import NullLocator

from vpaexp.records import load_run


def plot_resize_latency(runs):
    fig, ax = plt.subplots(figsize=(3.55, 2.45))
    fig.subplots_adjust(left=0.165, right=0.97, bottom=0.22, top=0.9)
    left, right = 0.03, 500
    for run in runs:
        applied = [r for r in run.resizes if r.outcome == "applied"]
        if not applied or any(not r.complete for r in applied):
            raise ValueError(f"Incomplete resize timing for {run.setting}")
        seconds = np.sort([r.applied_t - r.rec_updated_t for r in applied])
        fraction = np.arange(1, len(seconds) + 1) / len(seconds)
        color, linestyle = STYLE[run.setting]
        median_ms = 1000 * np.median(seconds)
        label = f"{run.setting.capitalize()} (n = {len(seconds)}), median {median_ms:.0f} ms"
        ax.step(np.r_[left, seconds], np.r_[0, 100 * fraction], where="post",
                color=color, ls=linestyle, lw=1.5, label=label)
        ax.plot(seconds[-1], 100, "o", color=color, ms=3.5)
        ax.annotate(f"{seconds[-1]:.1f} s", (seconds[-1], 100), xytext=(0, 5),
                    textcoords="offset points", ha="center", color=color, fontsize=8)

    ax.set(xscale="log", xlim=(left, right), ylim=(-2, 104), yticks=[0, 25, 50, 75, 100],
           xticks=[0.1, 1, 10, 100], xticklabels=["0.1", "1", "10", "100"],
           xlabel="Recommendation to applied limit (s, log scale)",
           ylabel="Cumulative share of resizes (%)")
    ax.xaxis.set_minor_locator(NullLocator())
    ax.legend(loc="lower right", fontsize=8, handlelength=2.1, borderaxespad=0.1)
    finish_axes(ax)
    return fig


if __name__ == "__main__":
    figure = plot_resize_latency([load_run(ROOT / "results" / name) for name in RUN_IDS[1:]])
    for extension in ("pdf", "svg", "png"):
        figure.savefig(HERE / "figures" / f"resize-latency-cdf.{extension}", dpi=300)
    plt.close(figure)
