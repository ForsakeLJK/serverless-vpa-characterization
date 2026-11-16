"""Command line: `vpaexp <command>`. Run from the repository root."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tomllib
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from vpaexp import config, decisions, figures, metrics, sizing
from vpaexp.cluster import Profile
from vpaexp.config import Allocation
from vpaexp.records import Run, load_run
from vpaexp.run import RunPlan, execute, log
from vpaexp.trace import DAY_S, K, WINDOWS, Slice, busiest_slice, days, extract_arrivals, load_arrivals, replay_schedule, save_arrivals
from vpaexp.trace import Floats

TRACE = Path("azure-trace/AzureFunctionsInvocationTraceForTwoWeeksJan2021.txt")
ARRIVALS = Path("data/arrivals.txt.gz")
VALIDATION_S = 3600.0
SETTINGS = ("static", "default", "tuned")
REPETITIONS = 3  # section 10


class Experiment:
    """A profile, its results tree, and the arrival times."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.profile = Profile.load(Path("cluster") / f"{args.profile}.toml")
        self.root = Path(args.results) / self.profile.name
        self.arrivals = load_arrivals(ARRIVALS)

    def payload_cost(self) -> dict[str, float]:
        """Per-request cost: measured on this testbed if `vpaexp measure` has run, else the profile's estimate."""
        path = self.root / "payload-cost.json"
        if path.exists():
            return dict(json.loads(path.read_text()))
        log(f"WARNING: no {path}; using the unverified estimates in cluster/{self.profile.name}.toml")
        p = self.profile
        return {"service_s": p.service_s, "request_mem_bytes": p.request_mem_bytes, "baseline_mem_bytes": p.baseline_mem_bytes}

    def demand(self, s: Slice) -> sizing.Demand:
        c = self.payload_cost()
        return sizing.offline_demand(replay_schedule(self.arrivals, s), c["service_s"], c["request_mem_bytes"], c["baseline_mem_bytes"])

    def sizing_path(self, name: str) -> Path:
        return self.root / "sizing" / f"{name}.json"

    def run_dir(self, name: str, force: bool = False) -> Path:
        d = self.root / "runs" / name
        if d.exists():
            if not force:
                sys.exit(f"{d} already exists; pass --force to replace it")
            shutil.rmtree(d)
        return d

    def runs(self, window: str, setting: str) -> list[Run]:
        found = []
        for d in sorted((self.root / "runs").glob(f"{window}-{setting}-*")):
            if (d / "run.json").exists() and (d / "client.csv").exists():
                found.append(load_run(d))
        return found

    def plan(self, s: Slice, schedule: Floats, duration_s: float, out: Path, setting: str, alloc: Allocation, cap: int,
             flags: config.VpaFlags | None, rep: int = 0, **kw: Any) -> RunPlan:
        return RunPlan(out_dir=out, setting=setting, slice_name=s.name, rep=rep, k=K, schedule=schedule,
                       duration_s=duration_s, allocation=alloc, concurrency_cap=cap, flags=flags,
                       service_s=self.payload_cost()["service_s"], **kw)


def _write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2))
    log(f"wrote {path}")


def cmd_extract(args: argparse.Namespace) -> None:
    arrivals = extract_arrivals(TRACE)
    save_arrivals(ARRIVALS, arrivals)
    print(f"{len(arrivals)} arrivals written to {ARRIVALS}")


def cmd_measure(args: argparse.Namespace) -> None:
    """Section 13: re-measure the payload's service time and memory, in the container, on this testbed."""
    exp = Experiment(args)
    lead_in, n, gap = 10.0, args.requests, 3.0
    schedule = lead_in + gap * np.arange(n, dtype=np.float64)
    s = Slice("measure", 0.0, 0.0)
    out = exp.run_dir("measure", args.force)
    execute(exp.profile, exp.plan(s, schedule, lead_in + gap * n, out, "generous", exp.profile.generous, 4, None))
    run = load_run(out)
    ok = run.client.status == "ok"
    if not ok.all():
        sys.exit(f"{int((~ok).sum())} of {n} requests failed; see {out}")
    if float(np.max(run.samples.memory_peak_bytes, initial=0.0)) <= 0:
        sys.exit("the node's kernel does not expose memory.peak (needs Linux 5.19+); per-request memory cannot be measured")
    idle = run.samples.memory_bytes[run.samples.t < run.t0 + lead_in - 1]
    baseline = float(np.median(idle))
    cost = {
        "service_s": float(np.median(run.client.completed - run.client.sent)),
        "request_mem_bytes": float(np.max(run.samples.memory_peak_bytes)) - baseline,
        "baseline_mem_bytes": baseline,
    }
    _write(exp.root / "payload-cost.json", cost)
    print(f"service time {cost['service_s'] * 1000:.0f} ms (trace function median: 380 ms); "
          f"memory {cost['request_mem_bytes'] / sizing.MI:.0f} MiB above a {baseline / sizing.MI:.0f} MiB baseline")


def cmd_size(args: argparse.Namespace) -> None:
    """Section 6.1: offline calculation, then a 1h validation replay or a full profiling replay."""
    exp = Experiment(args)
    window = WINDOWS[args.window]
    demand = exp.demand(window)
    record: dict[str, Any] = {"window": window.name, "payload_cost": exp.payload_cost(), "demand": demand.to_json()}
    generous = exp.profile.generous
    if demand.peaks.cpu_cores * 1000 > generous.cpu_millicores or demand.peaks.memory_bytes > generous.memory_bytes:
        log("WARNING: calculated peak demand exceeds the profile's generous limits")
    cap = sizing.CAP_FACTOR * demand.peaks.concurrency

    if args.validate:
        # Centre the hour on the middle of the calculated peak-CPU window.
        lo = min(max(demand.cpu_at_s + sizing.CPU_WINDOW_S / 2 - VALIDATION_S / 2, 0.0), window.replay_duration_s() - VALIDATION_S)
        s = Slice(f"{window.name}-validate", window.trace_start_s + lo * K, VALIDATION_S * K)
        slice_calc = exp.demand(s).peaks
        out = exp.run_dir(s.name, args.force)
        execute(exp.profile, exp.plan(s, replay_schedule(exp.arrivals, s), VALIDATION_S, out, "generous", generous, cap, None))
        measured = metrics.measured_peaks(load_run(out))
        record |= {"method": "validated", "slice_calc": asdict(slice_calc), "slice_measured": asdict(measured)}
        try:
            result = sizing.static_sizing(demand.peaks, slice_calc, measured)
        except sizing.ToleranceExceeded as e:
            _write(exp.sizing_path(window.name + "-failed-validation"), record | {"error": str(e)})
            sys.exit(f"validation failed: {e}\nrun: vpaexp size --window {window.name} --full-profile")
    elif args.full_profile:
        out = exp.run_dir(f"{window.name}-profile", args.force)
        execute(exp.profile, exp.plan(window, replay_schedule(exp.arrivals, window), window.replay_duration_s(), out, "generous", generous, cap, None))
        measured = metrics.measured_peaks(load_run(out))
        record |= {"method": "profiled", "measured": asdict(measured)}
        result = sizing.sizing_from_peaks(measured)
    else:
        record |= {"method": "calculated"}
        result = sizing.static_sizing(demand.peaks)

    record["sizing"] = result.to_json()
    print(json.dumps(record, indent=2))
    if args.validate or args.full_profile or args.write_unvalidated:
        _write(exp.sizing_path(window.name), record)
    else:
        print("not saved: this is the calculation alone. Use --validate (or --write-unvalidated for smoke tests).")


def _load_sizing(exp: Experiment, window: str) -> sizing.Sizing:
    path = exp.sizing_path(window)
    if not path.exists():
        sys.exit(f"no sizing for window {window}; run: vpaexp size --window {window} --validate")
    record = json.loads(path.read_text())
    if record["method"] == "calculated":
        log("WARNING: static sizing is calculated only, not validated; acceptable for smoke tests only")
    return sizing.Sizing.from_json(record["sizing"])


def _flags(setting: str, config_path: str | None) -> config.VpaFlags | None:
    if setting == "static":
        return None
    if setting == "default":
        return config.vpa_flags(K)
    if not config_path:
        sys.exit("--setting tuned needs --config FILE (a TOML file with a [flags] table)")
    return config.vpa_flags(K, tomllib.loads(Path(config_path).read_text())["flags"])


def cmd_run(args: argparse.Namespace) -> None:
    """Section 9: one command per run, taking a setting, a window and a repetition number."""
    exp = Experiment(args)
    window = WINDOWS[args.window]
    size = _load_sizing(exp, window.name)
    schedule, duration = replay_schedule(exp.arrivals, window), window.replay_duration_s()
    label = args.label or f"r{args.rep}"
    if args.limit_s:
        schedule, duration, label = schedule[schedule < args.limit_s], args.limit_s, f"{label}-smoke"
    boundary = DAY_S / K
    out = exp.run_dir(f"{window.name}-{args.setting}-{label}", args.force)
    plan = exp.plan(window, schedule, duration, out, args.setting, Allocation(size.cpu_millicores, size.memory_bytes),
                    size.concurrency_cap, _flags(args.setting, args.config), rep=args.rep,
                    phase_boundary_s=boundary if duration > boundary else None,
                    extra={"label": label, "tuned_config": args.config})
    execute(exp.profile, plan)
    run = load_run(out)
    print(json.dumps(metrics.summarize(run)["whole"], indent=2))
    if args.setting == "static" and not args.limit_s:
        for problem in metrics.static_sizing_too_low(run):
            print(f"SIZING TOO LOW ({problem}): redo sizing with --full-profile and repeat the affected runs")


def cmd_prototype(args: argparse.Namespace) -> None:
    """Section 11: a 30-minute pipeline check on a busy 2h slice of day 2, with shortened VPA windows."""
    exp = Experiment(args)
    s = busiest_slice(exp.arrivals, days("day2", 2, 1), 7200.0, "proto")
    size = sizing.static_sizing(exp.demand(s).peaks)
    g = exp.profile.generous
    alloc = Allocation(min(size.cpu_millicores, g.cpu_millicores), min(size.memory_bytes, g.memory_bytes))
    flags = config.vpa_flags(K, config.PROTOTYPE_FLAGS, restrict_to_declared=False)
    out = exp.run_dir("proto-prototype-r1", args.force)
    execute(exp.profile, exp.plan(s, replay_schedule(exp.arrivals, s), s.replay_duration_s(), out, "prototype", alloc,
                                  size.concurrency_cap, flags, rep=1, poll_metrics_api=True))
    sys.exit(_gate(out))


def _gate(run_dir: Path) -> int:
    run = load_run(run_dir)
    checks = decisions.gate_checks(run)
    polls_path = run_dir / "metrics_api.jsonl"
    polls = [json.loads(line) for line in polls_path.read_text().splitlines()] if polls_path.exists() else []
    checks.append(decisions.metrics_refresh_check(polls))
    try:
        paths = figures.run_figures(run, run_dir / "figures")
        checks.append(decisions.Check("5. figures 1-6 render", True, f"{len(paths)} files in {run_dir / 'figures'}"))
    except Exception as e:  # the check is precisely whether rendering fails
        checks.append(decisions.Check("5. figures 1-6 render", False, repr(e)))
    for c in checks:
        print(f"{'PASS' if c.passed else 'FAIL'}  {c.name}: {c.detail}")
    passed = all(c.passed for c in checks)
    print("GATE PASSED" if passed else "GATE FAILED: no full run may start")
    return 0 if passed else 1


def cmd_gate(args: argparse.Namespace) -> None:
    sys.exit(_gate(Path(args.run_dir)))


def cmd_figures(args: argparse.Namespace) -> None:
    run = load_run(Path(args.run_dir))
    for p in figures.run_figures(run, Path(args.run_dir) / "figures", [load_run(Path(d)) for d in args.compare]):
        print(p)


def _repetitions(exp: Experiment, window: str, setting: str) -> list[Run]:
    return [r for r in exp.runs(window, setting) if re.fullmatch(r"r\d+", str(r.meta.get("label", "")))]


def cmd_report(args: argparse.Namespace) -> None:
    """Figures 5, 7 (window A) or 8 (window B), the H1 verdict, and the static sizing check."""
    exp = Experiment(args)
    by_setting = {s: _repetitions(exp, args.window, s) for s in SETTINGS}
    by_setting = {s: runs for s, runs in by_setting.items() if runs}
    if not by_setting:
        sys.exit(f"no completed runs for window {args.window} under {exp.root / 'runs'}")
    summaries = {s: [metrics.summarize(r) for r in runs] for s, runs in by_setting.items()}
    out = exp.root / "report"
    report: dict[str, Any] = {
        "window": args.window,
        "repetitions": {s: len(v) for s, v in summaries.items()},
        "summary": {s: {ph: metrics.aggregate([rep[ph] for rep in reps]) for ph in reps[0]} for s, reps in summaries.items()},
    }
    n = 7 if args.window == "A" else 8
    print(figures.fig7_summary(summaries, out / f"fig{n}-summary-{args.window}.png", f"Window {args.window}"))
    print(figures.fig5_latency_cdf([runs[0] for runs in by_setting.values()], out / f"fig5-latency-cdf-{args.window}.png"))
    short = [s for s in ("default", "tuned") if len(summaries.get(s, [])) < REPETITIONS]
    if short:
        print(f"H1 not evaluated: fewer than {REPETITIONS} repetitions of {' and '.join(short)}")
    else:
        report["h1"] = decisions.h1([s["whole"] for s in summaries["default"]], [s["whole"] for s in summaries["tuned"]])
        print(f"H1 {'supported' if report['h1']['supported'] else 'rejected'} on window {args.window}")
    for run in by_setting.get("static", []):
        for problem in metrics.static_sizing_too_low(run):
            print(f"SIZING TOO LOW in {run.path.name}: {problem}")
    _write(out / f"report-{args.window}.json", report)


def cmd_tune_select(args: argparse.Namespace) -> None:
    """Section 7 selection rule over the tuning iterations (runs labelled it1, it2, ...)."""
    exp = Experiment(args)
    static = _repetitions(exp, "A", "static")
    iterations = [r for r in exp.runs("A", "tuned") if re.fullmatch(r"it\d+", str(r.meta.get("label", "")))]
    if not static or not iterations:
        sys.exit("need at least one full static run and one tuning iteration (--setting tuned --label itN) on window A")
    static_p99 = float(np.median([metrics.summarize(r)["whole"]["p99_s"] for r in static]))
    candidates = []
    for r in iterations:
        w = metrics.summarize(r)["whole"]
        candidates.append(decisions.Candidate(str(r.meta["label"]), w["alloc_cpu_m"], w["alloc_mem_bytes"], w["p99_s"],
                                             w["oom_kills"] + w["container_restarts"]))  # any restart disqualifies, OOM or not
    chosen, met = decisions.select_tuned(candidates, static_p99, static[0].initial)
    result = {"static_p99_s": static_p99, "candidates": [asdict(c) for c in candidates], "chosen": chosen.name, "constraint_met": met}
    print(json.dumps(result, indent=2))
    if not met:
        print("CONSTRAINT UNMET: no iteration had zero OOM kills and p99 within 1.1x static; lowest p99 chosen. Report this.")
    _write(exp.root / "report" / "tuning-selection.json", result)


def cmd_tuning_log(args: argparse.Namespace) -> None:
    rows = tomllib.loads(Path(args.log).read_text()).get("iteration", [])
    print(figures.fig9_tuning_log(rows, Path(args.out)))


def main() -> None:
    p = argparse.ArgumentParser(prog="vpaexp", description=__doc__)
    p.add_argument("--profile", default="prototype", help="cluster/<profile>.toml (prototype or full)")
    p.add_argument("--results", default="results")
    sub = p.add_subparsers(required=True)

    def command(name: str, fn: Any, help: str) -> argparse.ArgumentParser:
        c = sub.add_parser(name, help=help)
        c.set_defaults(fn=fn)
        return c

    command("extract", cmd_extract, "extract the function's arrival times from the Azure trace")

    c = command("measure", cmd_measure, "measure the payload's service time and memory on this testbed")
    c.add_argument("--requests", type=int, default=20)
    c.add_argument("--force", action="store_true")

    c = command("size", cmd_size, "static sizing for a window")
    c.add_argument("--window", choices=sorted(WINDOWS), required=True)
    how = c.add_mutually_exclusive_group()
    how.add_argument("--validate", action="store_true", help="run the 1h validation replay and save the sizing")
    how.add_argument("--full-profile", action="store_true", help="run a full 24h profiling replay instead")
    how.add_argument("--write-unvalidated", action="store_true", help="save the calculation alone (smoke tests)")
    c.add_argument("--force", action="store_true")

    c = command("run", cmd_run, "one run: a setting, a window, a repetition")
    c.add_argument("--setting", choices=SETTINGS, required=True)
    c.add_argument("--window", choices=sorted(WINDOWS), required=True)
    c.add_argument("--rep", type=int, default=1)
    c.add_argument("--config", help="tuned only: TOML file with a [flags] table of declared knobs")
    c.add_argument("--label", help="run label instead of r<rep>, e.g. it1 for a tuning iteration")
    c.add_argument("--limit-s", type=float, help="smoke test: replay only the first N seconds")
    c.add_argument("--force", action="store_true")

    c = command("prototype", cmd_prototype, "the 30-minute prototype run, then the gate")
    c.add_argument("--force", action="store_true")

    c = command("gate", cmd_gate, "evaluate the five prototype gate checks on a run directory")
    c.add_argument("run_dir")

    c = command("figures", cmd_figures, "render figures 1-6 for a run directory")
    c.add_argument("run_dir")
    c.add_argument("--compare", nargs="*", default=[], help="other runs to overlay in the latency CDF")

    c = command("report", cmd_report, "summary figure (7 or 8), latency CDF and H1 for a window")
    c.add_argument("--window", choices=sorted(WINDOWS), required=True)

    command("tune-select", cmd_tune_select, "apply the selection rule to the tuning iterations")

    c = command("tuning-log", cmd_tuning_log, "render the tuning log table (figure 9)")
    c.add_argument("--log", default="tuning/log.toml")
    c.add_argument("--out", default="tuning/fig9-tuning-log.png")

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
