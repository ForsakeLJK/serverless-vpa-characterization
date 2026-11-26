"""Rebuild the Window A figures and print the numerical evidence from the archived runs."""

import argparse
import json
from datetime import datetime
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from vpaexp.config import MI
from vpaexp.metrics import allocation_steps, oom_kill_times, summarize
from vpaexp.records import load_run
from vpaexp.trace import WINDOWS, load_arrivals, replay_schedule

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUN_IDS = (
    "A-static-r1-20261122-150847",
    "A-default-r1-20261121-124656",
    "A-tuned-it2-20261123-160509",
)
STYLE = {
    "static": ("#343c40", "-"),
    "default": ("#006da0", "--"),
    "tuned": ("#aa4815", "-"),
}
plt.rcParams.update({
    "font.size": 9, "axes.spines.top": False, "axes.spines.right": False,
    "axes.edgecolor": "#666666", "axes.linewidth": 0.6, "svg.fonttype": "none",
    "pdf.fonttype": 42, "legend.frameon": False, "legend.fontsize": 8.5,
})


def step_values(times, values, query):
    return values[np.searchsorted(times, query, side="right") - 1]


def series(run, metric):
    if metric == "allocation":
        times, memory = allocation_steps(run)
        times = np.maximum(times - run.t0, 0)
    else:
        times = np.array([r.t - run.t0 for r in run.recommendations])
        memory = np.array([getattr(r, metric) for r in run.recommendations])
    return np.r_[times, run.duration_s], np.r_[memory, memory[-1]] / MI


def restart_gaps(run):
    """Pair a terminated instance and its successor in the same kubelet status."""
    pairs = set()
    with (run.path / "watch.jsonl").open() as stream:
        for line in stream:
            record = json.loads(line)
            pod = record.get("object") or {}
            if record["kind"] != "pod" or pod["metadata"]["uid"] != run.meta["pod"]["uid"]:
                continue
            for c in pod.get("status", {}).get("containerStatuses", []):
                old = c.get("lastState", {}).get("terminated", {})
                new = c.get("state", {}).get("running", {})
                if c["name"] == "function" and old.get("reason") == "OOMKilled" and new.get("startedAt"):
                    pairs.add((old["finishedAt"], new["startedAt"]))
    return sorted((datetime.fromisoformat(a).timestamp() - run.t0,
                   datetime.fromisoformat(b).timestamp() - run.t0) for a, b in pairs)


def draw_steps(ax, run, metric, label, color, ls, **kwargs):
    t, v = series(run, metric)
    ax.step(t / 3600, v, where="post", label=label, color=color, ls=ls, **kwargs)


def finish_axes(ax):
    ax.set_axisbelow(True)
    ax.grid(axis="y", alpha=0.18, linewidth=0.6)
    ax.tick_params(width=0.65, length=3)


def tracking(static, default, tuned):
    fig, axes = plt.subplots(2, 1, figsize=(7.0, 3.45), layout="constrained")
    ax, zoom = axes
    t = static.samples.t - static.t0
    valid = (t >= 0) & (t < static.duration_s)
    peak = np.zeros(1440)
    np.maximum.at(peak, (t[valid] // 60).astype(int), static.samples.memory_bytes[valid] / MI)
    ax.stairs(peak, np.arange(1441) / 60, fill=True, color="#a9bdbb", label="Static usage peak")
    for r in (static, default, tuned):
        draw_steps(ax, r, "allocation", r.setting.capitalize() + " limit", *STYLE[r.setting], lw=1.35)
    ax.set(xlim=(0, 24), ylim=(0, 2840), yticks=[0, 1000, 2000], xticks=np.arange(0, 25, 3),
           xlabel="Replay time (h)", ylabel="Memory (MiB)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, 1.31), ncol=4)
    draw_steps(zoom, tuned, "allocation", "Tuned limit", *STYLE["tuned"], lw=1.5)
    draw_steps(zoom, tuned, "target", "Tuned target", "#383e60", "--", lw=1.35)
    kills = (oom_kill_times(tuned) - tuned.t0) / 3600
    zoom.scatter(kills, np.full_like(kills, 1190), marker="|", s=36, c="#393939", label="OOM kill")
    zoom.set(xlim=(12, 20), ylim=(600, 1250), yticks=[650, 800, 950, 1100],
             xlabel="Replay time (h), expanded interval", ylabel="Memory (MiB)")
    zoom.legend(ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.31))
    for a in axes:
        finish_axes(a)
    return fig


def tails(runs):
    fig, (ax, tail) = plt.subplots(1, 2, figsize=(7, 2.5), width_ratios=[1.45, 1], layout="constrained")
    for run, marker in zip(runs, ("o", "s", "^"), strict=True):
        c = run.client
        lat = np.sort((c.completed - c.scheduled)[c.status == "ok"])
        color, ls = STYLE[run.setting]
        # Each completion removes 1/N from the offered-request miss fraction.
        miss = 100 * (1 - np.arange(1, len(lat) + 1) / len(c.status))
        mask = (lat >= 0.8) & (miss > 0)
        x, y = lat[mask], miss[mask]
        if len(lat) < len(c.status):
            x, y = np.r_[x, 4], np.r_[y, y[-1]]
        ax.step(x, y, where="post", color=color, ls=ls, lw=1.4, label=run.setting.capitalize())
        tail.plot([0, 1, 2], np.percentile(lat, [99, 99.5, 99.9]), color=color, label=run.setting.capitalize(),
                  marker=marker, ls=ls, lw=1.2, ms=5, markerfacecolor="white")
    ax.set(xlim=(0.8, 4), yscale="log", ylim=(0.001, 110), xticks=[1, 2, 3, 4],
           xlabel="Completion deadline (s)", ylabel="All arrivals missing deadline (%)")
    tail.legend(loc="upper left")
    tail.set(xlim=(-0.16, 2.16), ylim=(1, 3.65), xticks=[0, 1, 2], xticklabels=["99", "99.5", "99.9"],
             xlabel="Percentile among successes", ylabel="Scheduled-to-completion time (s)")
    for a in (ax, tail):
        finish_axes(a)
    return fig


def burst(tuned):
    fig, ax = plt.subplots(figsize=(7, 2.1), layout="constrained")
    start, end = 3.8 * 3600, 4.05 * 3600
    bins = np.arange(start, end + 5, 5)
    c = tuned.client
    for i, (left, right) in enumerate((a,b) for a,b in restart_gaps(tuned) if a < end and b > start):
        ax.axvspan((left-start)/60, (right-start)/60, color="#e8ddd3", lw=0,
                   label="OOM exit to restart" if i == 0 else None)
    for mask, label, color, ls in [(np.ones(len(c.status), dtype=bool), "Offered", "#313a42", "--"),
                                    (c.status == "ok", "Successful", "#aa4815", "-")]:
        count = np.histogram(c.scheduled[mask] - tuned.t0, bins)[0]
        ax.stairs(count, (bins-start)/60, color=color, ls=ls, lw=1.25, label=label)
    ax.set(xlim=(0,15), ylim=(0,None), xticks=range(0,16,3), xlabel="Minutes since replay hour 3.80",
           ylabel="Requests per 5 s")
    ax.legend(ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.26))
    finish_axes(ax)
    return fig


def trace_overlap(service):
    arrivals = [replay_schedule(load_arrivals(ROOT / "data" / f), WINDOWS["A"])
                for f in ("arrivals.txt.gz", "neighbor-arrivals.txt.gz")]
    edges = np.unique(np.r_[0, 86400, *arrivals, *(a + service for a in arrivals)])
    edges = edges[edges <= 86400]
    active = [np.searchsorted(a, edges[:-1], side="right") -
              np.searchsorted(a + service, edges[:-1], side="right") for a in arrivals]
    both = (active[0] >= 3) & (active[1] >= 3)
    return np.diff(edges)[both].sum(), np.max(active[0] + active[1])


def evidence(runs):
    static = runs[0]
    ref = static.client
    rt = static.samples.t - static.t0
    rv = static.samples.memory_bytes / MI
    for run in runs:
        c = run.client
        schedule = c.scheduled - run.t0
        np.testing.assert_allclose(schedule, ref.scheduled - static.t0, atol=2e-6, rtol=0)
        ok = c.status == "ok"
        latency = c.completed - c.scheduled
        print(run.path.name)
        summary = summarize(run)
        archive = json.loads((run.path / "summary.json").read_text())["phases"]
        for phase, metrics in summary.items():
            for key, value in metrics.items():
                np.testing.assert_allclose(value, archive[phase][key], rtol=1e-8)
            print(phase, metrics)
        print("Successful-request percentiles (99, 99.5, 99.9)", np.percentile(latency[ok], [99,99.5,99.9]))
        print("Matched static p99", np.percentile((ref.completed - ref.scheduled)[ok], 99))
        print("Successful within 2 s", np.sum(ok & (latency <= 2)))
        minute = (schedule // 60).astype(int)
        counts = np.bincount(minute, minlength=1440)
        cutoff = np.sort(counts)[-144]
        busy = counts[minute] >= cutoff
        print("Busiest minutes incl. ties: cutoff, minutes, requests, errors in/out",
              cutoff, sum(counts >= cutoff), sum(busy), sum(busy & ~ok), sum(~busy & ~ok))
        if not run.resizes:
            continue
        for metric in ("target", "allocation"):
            t, v = series(run, metric)
            m = (rt >= max(0, t[0])) & (rt < run.duration_s)
            slack = step_values(t, v, rt[m]) - rv[m]
            print(metric, "aligned samples, median slack in MiB, negative samples",
                  len(slack), np.median(slack), sum(slack < 0))
        gaps = restart_gaps(run)
        inside = np.zeros(len(ok), dtype=bool)
        for a,b in gaps:
            inside |= (schedule >= a) & (schedule < b)
        print("Restart gaps: count, seconds, arrivals, errors", len(gaps),
              sum(b-a for a,b in gaps), sum(inside), sum(inside & ~ok))
        pending = np.zeros(len(ok), dtype=bool)
        for z in run.resizes:
            pending |= (c.scheduled >= z.patched_t) & (c.scheduled < z.applied_t)
            print("Resize: replay hour, new MiB, issues, two leg durations (s)",
                  (z.patched_t-run.t0)/3600, z.after.memory_bytes/MI, z.issues,
                  z.patched_t-z.rec_updated_t, z.applied_t-z.patched_t)
        print("Arrivals and errors between patch and application", sum(pending), sum(pending & ~ok))
        if run.setting == "tuned":
            # Longest interval from an applied change to the next patch.
            previous, following = max(zip(run.resizes[:-1], run.resizes[1:], strict=True),
                                      key=lambda pair: pair[1].patched_t - pair[0].applied_t)
            begin, end = previous.applied_t, following.patched_t
            points = np.arange(begin - run.t0, end - run.t0)
            limit = previous.after.memory_bytes / MI
            target, lower, upper = (step_values(*series(run, field), points)
                                    for field in ("target", "lower", "upper"))
            during = (c.scheduled >= begin) & (c.scheduled < end)
            kills = oom_kill_times(run)
            print("Plateau: hours, MiB, hours below target, share inside bounds, OOMs, errors, arrivals",
                  (end-begin)/3600, limit, sum(target > limit)/3600,
                  np.mean((lower <= limit) & (limit <= upper)), sum((kills >= begin) & (kills < end)),
                  sum(during & ~ok), sum(during))
            longest = max(gaps, key=lambda gap: gap[1] - gap[0])
            m = (schedule >= longest[0]) & (schedule < longest[1])
            print("Longest OOM-to-start gap (s), arrivals, errors", longest[1]-longest[0], sum(m), sum(m & ~ok))
    for service in (0.33, np.median(ref.completed - ref.sent)):
        print("Modeled overlap: service time, seconds with >=3 on both, joint peak", service, *trace_overlap(service))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, help="also export vector figures as PDFs")
    args = parser.parse_args()
    runs = [load_run(ROOT / "results" / name) for name in RUN_IDS]
    evidence(runs)
    for name, fig in (("tracking", tracking(*runs)), ("tails", tails(runs)), ("burst", burst(runs[2]))):
        fig.savefig(HERE / "figures" / (name + ".svg"))
        if args.pdf_dir:
            args.pdf_dir.mkdir(parents=True, exist_ok=True)
            fig.savefig(args.pdf_dir / (name + ".pdf"))
        plt.close(fig)
