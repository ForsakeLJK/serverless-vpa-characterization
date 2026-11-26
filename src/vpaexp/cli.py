"""Command line: `vpaexp <command>`. Run from the repository root."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import tomllib
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from vpaexp import config, decisions, figures, metrics, rq4, sizing
from vpaexp.cluster import Kubectl, Profile
from vpaexp.config import Allocation
from vpaexp.records import Run, load_run
from vpaexp.run import RunPlan, Setting, execute, log, write_json
from vpaexp.trace import DAY_S, FUNC_PREFIX, K, WINDOWS, Slice, busiest_slice, days, extract_arrivals, load_arrivals, replay_schedule, save_arrivals
from vpaexp.trace import Floats

TRACE = Path("azure-trace/AzureFunctionsInvocationTraceForTwoWeeksJan2021.txt")
TRACE_INPUTS = {
    "target": (FUNC_PREFIX, Path("data/arrivals.txt.gz")),
    "neighbor": ("155e47f8e7f751d0c845049456d01832013c61336a8cd85901330ac821a71534", Path("data/neighbor-arrivals.txt.gz")),
}
SETTINGS = ("static", "default", "tuned")
REPETITIONS = 3  # section 10


class Experiment:
    """A profile, its results tree, and the arrival times."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.profile = Profile.load(Path("cluster") / f"{args.profile}.toml")
        if args.k < 1 or args.keep_every < 1:
            sys.exit("--k and --keep-every must be positive integers")
        self.k: int = args.k
        self.keep_every: int = args.keep_every
        # The payload cost belongs to the testbed; everything else to the compression, so a
        # rehearsal (k = 96) never mixes with the k = 4 campaign in sizing, runs or reports.
        self.testbed_root = Path(args.results) / self.profile.name
        self.root = Path(args.results) / (self.profile.name + self.compression_suffix())
        self.trace_name: str = args.trace
        self.func_prefix, arrivals_path = TRACE_INPUTS[self.trace_name]
        if self.trace_name != "target":
            self.root = self.root / self.trace_name
        self.arrivals = load_arrivals(arrivals_path)
        if self.root != self.testbed_root:
            log(f"compression k = {self.k}, keeping every {self.keep_every} arrival(s); results under {self.root}")

    def compression_suffix(self) -> str:
        """'' for the campaign (k = 4, every arrival), e.g. '-k96-keep4' otherwise."""
        if self.k == K and self.keep_every == 1:
            return ""
        return f"-k{self.k}" + (f"-keep{self.keep_every}" if self.keep_every != 1 else "")

    def schedule(self, s: Slice) -> Floats:
        """Send times of the slice at this compression: time divided by k, then every Nth arrival kept."""
        return replay_schedule(self.arrivals, s, self.k)[:: self.keep_every]

    def payload_cost(self) -> sizing.PayloadCost:
        """Per-request cost: measured on this testbed if `vpaexp measure` has run, else the profile's estimate."""
        path = self.testbed_root / "payload-cost.json"
        if path.exists():
            return sizing.PayloadCost(**json.loads(path.read_text()))
        log(f"WARNING: no {path}; using the unverified estimates in cluster/{self.profile.name}.toml")
        return self.profile.payload_cost

    def demand(self, s: Slice) -> sizing.Demand:
        return sizing.offline_demand(self.schedule(s), self.payload_cost())

    def sizing_path(self, name: str) -> Path:
        return self.root / "sizing" / f"{name}.json"

    def run_dir(self, name: str) -> Path:
        """A new directory `runs/<name>-<YYYYmmdd-HHMMSS>`: every attempt is kept, none is overwritten."""
        d = self.root / "runs" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
        if d.exists():
            sys.exit(f"{d} already exists; wait a second and start again")
        return d

    def runs(self, window: str, setting: str, label: str) -> list[Run]:
        """The valid, completed runs of a setting on a window whose label matches the pattern.

        Each run directory is a separate attempt. Where a label has several, the latest one counts.
        """
        latest: dict[str, Run] = {}
        for d in sorted((self.root / "runs").glob(f"{window}-{setting}-*")):
            if not ((d / "run.json").exists() and (d / "client.csv").exists()):
                continue
            run = load_run(d)
            if not re.fullmatch(label, run.label):
                continue
            previous = latest.get(run.label)
            if previous is not None and previous.t0 > run.t0:
                run, previous = previous, run
            if previous is not None:
                print(f"SUPERSEDED: {previous.path.name} (a later attempt of {run.label} exists)")
            latest[run.label] = run
        found = []
        for run in latest.values():
            reason = metrics.invalid_reason(run)
            if reason:
                print(f"EXCLUDED, invalid run {run.path.name}: {reason}")
            else:
                found.append(run)
        return found

    def allocation(self, memory_bytes: int) -> Allocation:
        """CPU is fixed at the profile value in every run; only memory varies (section 6)."""
        return Allocation(self.profile.generous.cpu_millicores, memory_bytes)

    def plan(self, s: Slice, out: Path, setting: Setting, alloc: Allocation, cap: int, flags: config.VpaFlags | None, *,
             schedule: Floats | None = None, duration_s: float | None = None, rep: int = 0,
             phase_boundary_s: float | None = None, poll_metrics_api: bool = False,
             extra: dict[str, Any] | None = None) -> RunPlan:
        """A run over the slice: all of it, unless a schedule and duration are given."""
        return RunPlan(
            out_dir=out, setting=setting, slice_name=s.name, rep=rep, k=self.k,
            schedule=self.schedule(s) if schedule is None else schedule,
            duration_s=s.replay_duration_s(self.k) if duration_s is None else duration_s,
            allocation=alloc, concurrency_cap=cap, flags=flags, service_s=self.payload_cost().service_s,
            phase_boundary_s=phase_boundary_s, poll_metrics_api=poll_metrics_api,
            extra={"trace": self.trace_name, "trace_func_prefix": self.func_prefix, "keep_every": self.keep_every, **(extra or {})},
        )

    def sizing_replay(self, s: Slice, name: str, cap: int) -> sizing.Peaks:
        """Replay the slice with a generous memory limit, the fixed CPU and no VPA; return the measured peaks.

        Every request must succeed before its peaks can be used for sizing.
        """
        out = self.run_dir(name)
        execute(self.profile, self.plan(s, out, "generous", self.profile.generous, cap, None))
        run = load_run(out)
        reason = metrics.invalid_reason(run)
        if reason:
            sys.exit(f"SIZING REPLAY INVALID: {reason} ({out})")
        return metrics.measured_peaks(run)


def _report_validity(run: Run) -> bool:
    """Print the validity problem, if any, and return whether the run is valid."""
    reason = metrics.invalid_reason(run)
    if reason:
        print(f"RUN INVALID: {reason}")
    return reason is None


def cmd_extract(args: argparse.Namespace) -> None:
    prefix, path = TRACE_INPUTS[args.trace]
    arrivals = extract_arrivals(TRACE, prefix)
    if not len(arrivals):
        sys.exit(f"No arrivals found for {prefix} in {TRACE}")
    save_arrivals(path, arrivals)
    print(f"{len(arrivals)} arrivals written to {path}")


def cmd_measure(args: argparse.Namespace) -> None:
    """Section 13: re-measure the payload's service time and memory, in the container, on this testbed."""
    exp = Experiment(args)
    lead_in, n, gap = 10.0, args.requests, 3.0
    schedule = lead_in + gap * np.arange(n, dtype=np.float64)
    s = Slice("measure", 0.0, 0.0)
    out = exp.run_dir("measure")
    execute(exp.profile, exp.plan(s, out, "generous", exp.profile.generous, 4, None,
                                  schedule=schedule, duration_s=lead_in + gap * n))
    run = load_run(out)
    reason = metrics.invalid_reason(run)
    if reason:
        sys.exit(f"MEASUREMENT INVALID: {reason} ({out})")
    if float(np.max(run.samples.memory_peak_bytes, initial=0.0)) <= 0:
        sys.exit("the node's kernel does not expose memory.peak (needs Linux 5.19+); per-request memory cannot be measured")
    cost = metrics.payload_cost(run, lead_in)
    write_json(exp.testbed_root / "payload-cost.json", asdict(cost))
    print(f"service time {cost.service_s * 1000:.0f} ms (trace function median: 380 ms); "
          f"memory {cost.request_mem_bytes / config.MI:.0f} MiB above a {cost.baseline_mem_bytes / config.MI:.0f} MiB baseline")


def cmd_size(args: argparse.Namespace) -> None:
    """Section 6.1: offline calculation, then a 1h validation replay or a full profiling replay."""
    exp = Experiment(args)
    window = WINDOWS[args.window]
    demand = exp.demand(window)
    record: dict[str, Any] = {"window": window.name, "trace": exp.trace_name, "trace_func_prefix": exp.func_prefix, "requests_scheduled": len(exp.schedule(window)), "k": exp.k, "keep_every": exp.keep_every, "payload_cost": asdict(exp.payload_cost()), "demand": demand.to_json()}
    generous = exp.profile.generous
    if demand.peaks.memory_bytes > generous.memory_bytes:
        log("WARNING: calculated peak memory exceeds the profile's generous limit")
    cap = sizing.CAP_FACTOR * demand.peaks.concurrency

    if args.validate:
        s = sizing.validation_slice(window, demand.peak_at_s, exp.k)
        slice_calc = exp.demand(s).peaks
        measured = exp.sizing_replay(s, s.name, cap)
        record |= {"method": "validated", "slice_calc": asdict(slice_calc), "slice_measured": asdict(measured)}
        try:
            result = sizing.static_sizing(demand.peaks, slice_calc, measured)
        except sizing.ToleranceExceeded as e:
            write_json(exp.sizing_path(window.name + "-failed-validation"), record | {"error": str(e)})
            sys.exit(f"validation failed: {e}\nrun: vpaexp size --window {window.name} --full-profile")
    elif args.full_profile:
        measured = exp.sizing_replay(window, f"{window.name}-profile", cap)
        record |= {"method": "profiled", "measured": asdict(measured)}
        result = sizing.sizing_from_peaks(measured)
    else:
        record |= {"method": "calculated"}
        result = sizing.static_sizing(demand.peaks)

    record["sizing"] = result.to_json()
    print(json.dumps(record, indent=2))
    if args.validate or args.full_profile or args.write_unvalidated:
        write_json(exp.sizing_path(window.name), record)
    else:
        print("not saved: this is the calculation alone. Use --validate (or --write-unvalidated for smoke tests).")


def _load_sizing(exp: Experiment, window: str) -> sizing.Sizing:
    path = exp.sizing_path(window)
    if not path.exists():
        sys.exit(f"no sizing for window {window}; run: vpaexp size --window {window} --validate")
    record = json.loads(path.read_text())
    if record.get("trace", "target") != exp.trace_name:
        sys.exit(f"{path} was sized for a different trace")
    sized_at = (record.get("k", K), record.get("keep_every", 1))
    if sized_at != (exp.k, exp.keep_every):
        sys.exit(f"{path} was sized at k = {sized_at[0]}, keep every {sized_at[1]}; this run uses "
                 f"k = {exp.k}, keep every {exp.keep_every}. Size the window again with the same options.")
    if record["method"] == "calculated":
        log("WARNING: static sizing is calculated only, not validated; acceptable for smoke tests only")
    return sizing.Sizing.from_json(record["sizing"])


def _flags(setting: str, config_path: str | None, k: int) -> config.VpaFlags | None:
    if setting == "static":
        return None
    if setting == "default":
        flags = config.vpa_flags(k)
    elif not config_path:
        sys.exit("--setting tuned needs --config FILE (a TOML file with a [flags] table)")
    else:
        flags = config.vpa_flags(k, tomllib.loads(Path(config_path).read_text())["flags"])
    for flag, note in flags.scaling.items():
        log(f"NOTE: {flag} = {note['used_s']}s, not {note['exact_s']:g}s ({note['reason']})")
    return flags


def cmd_run(args: argparse.Namespace) -> None:
    """Section 9: one command per run, taking a setting, a window and a repetition number."""
    exp = Experiment(args)
    window = WINDOWS[args.window]
    size = _load_sizing(exp, window.name)
    schedule, duration = exp.schedule(window), window.replay_duration_s(exp.k)
    label = args.label or f"r{args.rep}"
    if args.limit_s:
        schedule, duration, label = schedule[schedule < args.limit_s], args.limit_s, f"{label}-smoke"
    boundary = DAY_S / exp.k
    out = exp.run_dir(f"{window.name}-{args.setting}-{label}")
    plan = exp.plan(window, out, args.setting, exp.allocation(size.memory_bytes),
                    size.concurrency_cap, _flags(args.setting, args.config, exp.k),
                    schedule=schedule, duration_s=duration, rep=args.rep,
                    phase_boundary_s=boundary if duration > boundary else None,
                    extra={"label": label, "tuned_config": args.config})
    execute(exp.profile, plan)
    run = load_run(out)
    print(json.dumps(metrics.summarize(run)["whole"], indent=2))
    print(figures.run_figures(run, out / "figures")[-1])
    if args.setting == "static" and not args.limit_s:
        problem = metrics.static_sizing_too_low(run)
        if problem:
            print(f"SIZING TOO LOW ({problem}): redo sizing with --full-profile and repeat the affected runs")
    if not _report_validity(run):
        sys.exit(1)


def cmd_rq4_budget(args: argparse.Namespace) -> None:
    profile = Profile.load(Path("cluster/full.toml"))
    print(json.dumps(rq4.reservation_plan(Kubectl(profile.context, profile.namespace)), indent=2))


def cmd_rq4(args: argparse.Namespace) -> None:
    if args.profile != "full" or args.trace != "target" or args.keep_every != 1 or args.k != K:
        sys.exit("rq4 uses --profile full, window A at k=4, and the complete trace streams")
    duration = WINDOWS["A"].replay_duration_s()
    if args.limit_s is not None:
        if not 0 < args.limit_s <= duration:
            sys.exit("--limit-s must be positive and within window A")
        duration = args.limit_s
    exp = Experiment(args)
    condition = f"{args.setting}-{args.neighbor}-{'ample' if args.ample_memory else '6656Mi'}"
    if args.limit_s:
        condition += "-smoke"
    root = exp.root / "rq4" / f"{condition}-r{args.rep}-{time.strftime('%Y%m%d-%H%M%S')}"
    plans = {}
    for trace, app in (("target", config.APP), ("neighbor", rq4.NEIGHBOR)):
        if trace == "neighbor" and args.neighbor == "absent":
            continue
        stream = exp if trace == "target" else Experiment(argparse.Namespace(**(vars(args) | {"trace": trace})))
        size = _load_sizing(stream, "A")
        schedule = stream.schedule(WINDOWS["A"])
        setting: Setting = args.setting if trace == "target" else "default"
        plans[app] = stream.plan(WINDOWS["A"], root / trace, setting,
                                 Allocation(rq4.CPU_M, size.memory_bytes), size.concurrency_cap,
                                 _flags(setting, None, K), schedule=schedule[schedule < duration],
                                 duration_s=duration, rep=args.rep, phase_boundary_s=DAY_S / K,
                                 extra={"condition": condition})
    rq4.execute_rq4(exp.profile, plans, root, args.ample_memory)


def cmd_prototype(args: argparse.Namespace) -> None:
    """Section 11: a 30-minute pipeline check on a busy 2h slice of day 2, with shortened VPA windows."""
    exp = Experiment(args)
    if exp.compression_suffix():
        sys.exit("the prototype run is defined at k = 4 with every arrival (section 11); drop --k and --keep-every")
    s = busiest_slice(exp.arrivals, days("day2", 2, 1), 7200.0, "proto")
    size = sizing.static_sizing(exp.demand(s).peaks)
    alloc = exp.allocation(min(size.memory_bytes, exp.profile.generous.memory_bytes))
    flags = config.vpa_flags(K, config.PROTOTYPE_FLAGS, restrict_to_declared=False)
    out = exp.run_dir("proto-prototype-r1")
    execute(exp.profile, exp.plan(s, out, metrics.PROTOTYPE, alloc, size.concurrency_cap, flags, rep=1, poll_metrics_api=True))
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
    valid = _report_validity(run)
    whole = metrics.summarize(run)["whole"]
    if whole["cpu_throttled_s"] > 0:
        print(f"NOTE: CPU throttling occurred in {whole['cpu_throttled_period_pct']:.1f}% of recorded quota periods.")
    passed = valid and all(c.passed for c in checks)
    print("GATE PASSED" if passed else "GATE FAILED: no full run may start")
    return 0 if passed else 1


def cmd_gate(args: argparse.Namespace) -> None:
    sys.exit(_gate(Path(args.run_dir)))


def cmd_figures(args: argparse.Namespace) -> None:
    run = load_run(Path(args.run_dir))
    for p in figures.run_figures(run, Path(args.run_dir) / "figures"):
        print(p)


REPETITION, ITERATION = r"r\d+", r"it\d+"  # run labels: a repetition of a setting, a tuning iteration


def cmd_report(args: argparse.Namespace) -> None:
    """Figure 7 and the H1 verdict (window A) or figure 8 (window B), and the static sizing check."""
    exp = Experiment(args)
    by_setting = {s: exp.runs(args.window, s, REPETITION) for s in SETTINGS}
    by_setting = {s: runs for s, runs in by_setting.items() if runs}
    if not by_setting:
        sys.exit(f"no valid completed runs for window {args.window} under {exp.root / 'runs'}")
    summaries = {s: [metrics.summarize(r) for r in runs] for s, runs in by_setting.items()}
    out = exp.root / "report"
    report: dict[str, Any] = {
        "window": args.window,
        "repetitions": {s: len(v) for s, v in summaries.items()},
        "summary": {s: {ph: metrics.aggregate([rep[ph] for rep in reps]) for ph in reps[0]} for s, reps in summaries.items()},
    }
    n = 7 if args.window == "A" else 8
    print(figures.fig7_summary(summaries, out / f"fig{n}-summary-{args.window}.png", f"Window {args.window}"))
    short = [s for s in ("default", "tuned") if len(summaries.get(s, [])) < REPETITIONS]
    if args.window != "A":
        pass  # H1 is tested on window A only (section 1); window B is the transfer evaluation
    elif short:
        print(f"H1 not evaluated: fewer than {REPETITIONS} valid repetitions of {' and '.join(short)}")
    else:
        report["h1"] = decisions.h1([s["whole"] for s in summaries["default"]], [s["whole"] for s in summaries["tuned"]])
        print(f"H1 {'supported' if report['h1']['supported'] else 'rejected'} on window A")
    for run in by_setting.get("static", []):
        problem = metrics.static_sizing_too_low(run)
        if problem:
            print(f"SIZING TOO LOW in {run.path.name}: {problem}")
    write_json(out / f"report-{args.window}.json", report)


def cmd_tune_select(args: argparse.Namespace) -> None:
    """Section 7 selection rule over the tuning iterations (runs labelled it1, it2, ...)."""
    exp = Experiment(args)
    static = exp.runs("A", "static", REPETITION)
    iterations = exp.runs("A", "tuned", ITERATION)
    if not static or not iterations:
        sys.exit("need at least one valid full static run and one valid tuning iteration "
                 "(--setting tuned --label itN) on window A")
    selection = decisions.select_tuned(
        {r.label: metrics.summarize(r)["whole"] for r in iterations},
        [metrics.summarize(r)["whole"] for r in static],
    )
    result = {
        "static_p99_s": selection.static_p99_s,
        "static_failed_rate": selection.static_failed_rate,
        "static_runs": len(static),
        "candidates": [asdict(c) for c in selection.candidates],
        "chosen": selection.chosen.name,
        "constraint_met": selection.quality_kept,
    }
    print(json.dumps(result, indent=2))
    if not selection.quality_kept:
        print("SERVICE QUALITY NOT KEPT by any iteration (p99 and failed-request rate no worse than static); "
              "the one with the fewest failed requests was chosen. Report this.")
    write_json(exp.root / "report" / "tuning-selection.json", result)


def cmd_tuning_log(args: argparse.Namespace) -> None:
    rows = tomllib.loads(Path(args.log).read_text()).get("iteration", [])
    print(figures.fig9_tuning_log(rows, Path(args.out)))


def main() -> None:
    p = argparse.ArgumentParser(prog="vpaexp", description=__doc__)
    p.add_argument("--profile", default="prototype", help="cluster/<profile>.toml (prototype or full)")
    p.add_argument("--trace", choices=sorted(TRACE_INPUTS), default="target", help="Azure arrival stream to replay")
    p.add_argument("--results", default="results")
    p.add_argument("--k", type=int, default=K,
                   help=f"time compression factor (default {K}, the campaign). VPA durations are divided by it")
    p.add_argument("--keep-every", type=int, default=1, metavar="N",
                   help="replay every Nth arrival only (default 1, all of them)")
    sub = p.add_subparsers(required=True)

    def command(name: str, fn: Any, help: str) -> argparse.ArgumentParser:
        c = sub.add_parser(name, help=help)
        c.set_defaults(fn=fn)
        return c

    command("extract", cmd_extract, "extract the function's arrival times from the Azure trace")

    c = command("measure", cmd_measure, "measure the payload's service time and memory on this testbed")
    c.add_argument("--requests", type=int, default=20)

    c = command("size", cmd_size, "static sizing for a window")
    c.add_argument("--window", choices=sorted(WINDOWS), required=True)
    how = c.add_mutually_exclusive_group()
    how.add_argument("--validate", action="store_true", help="run the 1h validation replay and save the sizing")
    how.add_argument("--full-profile", action="store_true", help="run a full 24h profiling replay instead")
    how.add_argument("--write-unvalidated", action="store_true", help="save the calculation alone (smoke tests)")

    c = command("run", cmd_run, "one run: a setting, a window, a repetition")
    c.add_argument("--setting", choices=SETTINGS, required=True)
    c.add_argument("--window", choices=sorted(WINDOWS), required=True)
    c.add_argument("--rep", type=int, default=1)
    c.add_argument("--config", help="tuned only: TOML file with a [flags] table of declared knobs")
    c.add_argument("--label", help="run label instead of r<rep>, e.g. it1 for a tuning iteration")
    c.add_argument("--limit-s", type=float, help="smoke test: replay only the first N seconds")

    command("rq4-budget", cmd_rq4_budget, "calculate the ARM kubelet reservation on the idle testbed")
    c = command("rq4", cmd_rq4, "replay window A with a 30-CPU target and optional VPA neighbor")
    c.add_argument("--setting", choices=("default", "static"), default="default")
    c.add_argument("--neighbor", choices=("present", "absent"), default="present")
    c.add_argument("--rep", type=int, default=1)
    c.add_argument("--limit-s", type=float)
    c.add_argument("--ample-memory", action="store_true", help="run the pilot with ample available node memory")

    c = command("prototype", cmd_prototype, "the 30-minute prototype run, then the gate")

    c = command("gate", cmd_gate, "evaluate the five prototype gate checks on a run directory")
    c.add_argument("run_dir")

    c = command("figures", cmd_figures, "render figures 1-6 and the overview page for a run directory")
    c.add_argument("run_dir")

    c = command("report", cmd_report, "summary figure (7 or 8) and H1 for a window")
    c.add_argument("--window", choices=sorted(WINDOWS), required=True)

    command("tune-select", cmd_tune_select, "apply the selection rule to the tuning iterations")

    c = command("tuning-log", cmd_tuning_log, "render the tuning log table (figure 9)")
    c.add_argument("--log", default="tuning/log.toml")
    c.add_argument("--out", default="tuning/fig9-tuning-log.png")

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
