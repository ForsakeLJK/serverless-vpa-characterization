import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from vpaexp import cli, config, rq4
from vpaexp.config import MI
from vpaexp.run import RunPlan


def test_reservation_accounts_for_background_and_both_samplers() -> None:
    kc = Mock()
    node = {"metadata": {"name": "arm01"}, "status": {"nodeInfo": {"machineID": "arm"},
            "allocatable": {"memory": "16000Mi", "cpu": "64"},
            "conditions": [{"type": "MemoryPressure", "status": "False"}]}}
    kc.json.side_effect = [{"items": []}, {"items": [node]}]
    kc.run.side_effect = ["Allocated resources:\n  cpu  2200m (3%)\n  memory  300Mi (1%)\n",
                          json.dumps({"kubeletconfig": {"systemReserved": {"memory": "1Gi"}}})]
    plan = rq4.reservation_plan(kc)
    assert plan["effective_budget"] == (16000 - 300 - 128) * MI
    assert plan["desired_system_reserved_bytes"] == (1024 + 16000 - 300 - 128 - 6656) * MI
    assert plan["allocated_cpu_m"] == 2200


@pytest.mark.parametrize("setting", ["static", "default"])
@pytest.mark.parametrize("neighbor", ["absent", "present"])
def test_window_a_conditions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str, neighbor: str) -> None:
    for trace, size, cap in [("target", 2565, 72), ("neighbor", 3697, 104)]:
        folder = tmp_path / "full"
        if trace == "neighbor":
            folder /= trace
        folder /= "sizing"
        folder.mkdir(parents=True)
        (folder / "A.json").write_text(json.dumps({"trace": trace, "method": "calculated", "sizing": {
            "memory_bytes": size * MI, "concurrency_cap": cap, "peak_concurrency": cap // 4}}))
    execute = Mock()
    monkeypatch.setattr(rq4, "execute_rq4", execute)
    cli.cmd_rq4(argparse.Namespace(profile="full", k=4, keep_every=1, trace="target", results=tmp_path,
                                   setting=setting, neighbor=neighbor, limit_s=None, ample_memory=False, rep=1))
    _, plans, root, ample = execute.call_args.args
    assert not ample and root.parent == tmp_path / "full/rq4"
    assert len(plans) == (2 if neighbor == "present" else 1)
    target = plans["function"]
    assert len(target.schedule) == 52092 and target.duration_s == 86400
    assert target.allocation == config.Allocation(30000, 2565 * MI)
    assert (target.flags is None) == (setting == "static")
    if neighbor == "present":
        other = plans[rq4.NEIGHBOR]
        assert len(other.schedule) == 151104 and other.flags
        assert other.allocation == config.Allocation(30000, 3697 * MI)
        assert other.concurrency_cap == 104 and target.concurrency_cap == 72
        assert other.out_dir != target.out_dir
        assert other.extra["trace"] == "neighbor"


def test_separate_objects_and_audit(tmp_path: Path) -> None:
    plans: dict[str, RunPlan] = {}
    pods = {}
    events = []
    for i, app in enumerate(["function", rq4.NEIGHBOR]):
        dep = config.deployment_manifest("vpaexp", "image", config.Allocation(30000, 2565 * MI), 72, app)
        service = config.service_manifest("vpaexp", app, 30080 + i)
        assert dep["spec"]["selector"]["matchLabels"] == service["spec"]["selector"] == {"app": app}
        assert config.vpa_manifest("vpaexp", rq4.BUDGET, app)["spec"]["targetRef"]["name"] == app
        folder = tmp_path / app
        folder.mkdir()
        plans[app] = Mock(out_dir=folder)
        pods[app] = app + "-xyz"
        for resource, name in [("pods", pods[app]), ("verticalpodautoscalers", app)]:
            events.append({"objectRef": {"resource": resource, "name": name, "namespace": "vpaexp"}})
    audit = tmp_path / "audit.jsonl"
    audit.write_text("".join(json.dumps(e) + "\n" for e in [*events, {"objectRef": {
        "namespace": "elsewhere", "resource": "pods", "name": "function-xyz"}}]))
    rq4.split_audit(audit, "vpaexp", pods, plans)
    for i, plan in enumerate(plans.values()):
        assert [json.loads(line) for line in (plan.out_dir / "audit.jsonl").read_text().splitlines()] == events[i*2:i*2+2]


def test_pair_replay_records_one_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import numpy as np

    from vpaexp.run import RunPlan

    plans = {}
    urls = {}
    observed: list[tuple[float, float]] = []
    for app in ["function", rq4.NEIGHBOR]:
        folder = tmp_path / app
        folder.mkdir()
        (folder / "run.json").write_text("{}")
        plans[app] = RunPlan(folder, "default", "A", 1, 4, np.array([0.0]), 0.01,
                             config.Allocation(30000, 250 * MI), 16, config.vpa_flags(4), 0.33)
        urls[app] = "http://unused"

    async def fake_replay(*args: Any) -> float:
        observed.append(args[-1])
        return float(args[-1][0])

    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable: 30000 kB\n")
    real_path = Path
    monkeypatch.setattr(rq4, "Path", lambda name: meminfo if name == "/proc/meminfo" else real_path(name))
    monkeypatch.setattr(rq4, "replay_async", fake_replay)
    monkeypatch.setattr(rq4, "capacity", lambda *_: {})
    asyncio.run(rq4.replay_pair(plans, urls, Mock(), tmp_path))
    assert len(observed) == 2 and observed[0] == observed[1]
    for plan in plans.values():
        assert json.loads((plan.out_dir / "run.json").read_text())["replay_start"] == observed[0][0]


def test_live_budget_keeps_samplers_in_background_requests() -> None:
    kc = Mock(namespace="vpaexp")
    kc.json.return_value = {"items": [{"metadata": {"name": "arm01"}, "status": {
        "nodeInfo": {"machineID": "arm"}, "allocatable": {"cpu": "64", "memory": "7000Mi"},
        "conditions": [{"type": "MemoryPressure", "status": "False"}]}}]}
    kc.run.return_value = """Non-terminated Pods: (3 in total)
      vpaexp function-abc 30 (47%) 30 (47%) 250Mi (1%) 250Mi (1%) 3m
      vpaexp function-neighbor-abc 30 (47%) 30 (47%) 400Mi (1%) 400Mi (1%) 3m
      vpaexp sampler 50m (0%) 0 (0%) 64Mi (0%) 0 (0%) 3m
    Allocated resources:
      cpu  60300m (94%)
      memory 994Mi (14%)
    """
    state = rq4.capacity(kc, {"function-abc", "function-neighbor-abc"})
    assert state["allocatable_memory"] - state["other_requests"] == rq4.BUDGET
