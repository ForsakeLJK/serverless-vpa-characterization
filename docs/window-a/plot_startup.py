"""Plot the first three minutes of the archived Window A VPA replays."""

import numpy as np
from analyze import HERE, ROOT, RUN_IDS, STYLE, finish_axes, series
from matplotlib import pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import StrMethodFormatter

from vpaexp.metrics import container_restarts
from vpaexp.records import load_run


def plot_startup(runs):
    duration = 180
    edges = np.arange(0, duration + 5, 5)
    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(7.05, 4.65))
    fig.subplots_adjust(left=0.112, right=0.982, top=0.945, bottom=0.165, hspace=0.57)
    memory = axes[0]
    offered_color, error_color, oom_color = "#dce1e4", "#37454e", "#9c3320"

    for letter, run, ax in zip(("b", "c"), runs, axes[1:], strict=True):
        policy = run.setting.capitalize()
        color, linestyle = STYLE[run.setting]
        times, limits = series(run, "allocation")
        memory.step(times, limits, where="post", lw=1.6, color=color, ls=linestyle,
                    label=f"{policy} VPA", zorder=3 if run.setting == "default" else 2)

        arrival = run.client.scheduled - run.t0
        inside = (arrival >= 0) & (arrival < duration)
        errors = inside & (run.client.status != "ok")
        offered = np.histogram(arrival[inside], edges)[0]
        failed = np.histogram(arrival[errors], edges)[0]
        exits = [event.exited_t - run.t0 for event in container_restarts(run)
                 if event.oom and run.t0 <= event.exited_t < run.t0 + duration]

        ax.stairs(offered, edges, fill=True, color=offered_color, lw=0)
        ax.bar(edges[:-1], failed, width=np.diff(edges), align="edge",
               color=error_color, linewidth=0)
        # Kubelet exit times have second resolution. Use fine strokes to keep
        # even the two closest exits (two seconds apart) individually visible.
        ax.vlines(exits, 0, 30, color=oom_color, lw=0.65, ls=(0, (2, 2)))
        ax.vlines(exits, 27, 30, color=oom_color, lw=1.25)
        ax.set(ylim=(0, 31), yticks=[0, 10, 20, 30], ylabel="Requests\nper 5 s")
        ax.set_title(f"({letter})  {policy} VPA", loc="left", fontsize=10, weight="bold")
        ax.set_title(f"{failed.sum()} of {offered.sum()} failed ({100 * failed.sum() / offered.sum():.1f}%)"
                     f"   ·   {len(exits)} OOM exits", loc="right", fontsize=9)

    memory.set(ylim=(0, 2800), yticks=[250, 1000, 2000, 2565], ylabel="Limit (MiB)")
    memory.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    memory.set_title("(a)  Applied container memory limit", loc="left", fontsize=10, weight="bold")
    memory.legend(loc="upper right", ncol=2, handlelength=2.5)
    for ax in axes:
        finish_axes(ax)
        ax.set_xlim(0, duration)
    axes[-1].set(xticks=np.arange(0, duration + 1, 15), xlabel="Time since replay start (s)")
    fig.legend(handles=[
        Patch(facecolor=offered_color, label="Offered requests"),
        Patch(facecolor=error_color, label="Failed requests"),
        Line2D([], [], ls="none", marker="|", markersize=9, markeredgewidth=1.25,
               color=oom_color, label="Out-of-memory (OOM) exit"),
    ], loc="lower center", bbox_to_anchor=(0.54, 0.005), ncol=3, handlelength=1.3)
    return fig


if __name__ == "__main__":
    figure = plot_startup([load_run(ROOT / "results" / name) for name in RUN_IDS[1:]])
    for extension in ("pdf", "svg", "png"):
        figure.savefig(HERE / "figures" / f"vpa-startup.{extension}", dpi=240)
    plt.close(figure)
