from pathlib import Path

from vpaexp.figures import fig7_summary, fig9_tuning_log, run_figures
from vpaexp.metrics import summarize
from vpaexp.records import load_run

from .synthetic import scenario, write_run


def test_figures_1_to_6_render_from_a_collected_run(tmp_path: Path) -> None:
    run = load_run(scenario(tmp_path / "run"))
    paths = run_figures(run, tmp_path / "figs")
    assert [p.name for p in paths][-1] == "overview.png" and len(paths) == 7
    assert not any("cpu" in p.name for p in paths)
    assert paths[0].name == "fig1-throughput.png"
    assert all(p.stat().st_size > 5_000 for p in paths)


def test_figures_render_for_a_static_run_with_no_vpa_data(tmp_path: Path) -> None:
    d = write_run(tmp_path / "run", client=[(1.0, 0.1, "ok")], sampler=[(float(i), 0.2, 50.0, 0, 0, 0.0) for i in range(5)])
    assert len(run_figures(load_run(d), tmp_path / "figs")) == 7


def test_summary_and_tuning_log_figures_render(tmp_path: Path) -> None:
    reps = [summarize(load_run(scenario(tmp_path / f"r{i}"))) for i in range(3)]
    out = fig7_summary({"static": reps, "default": reps, "tuned": reps}, tmp_path / "fig7.png", "Window A")
    assert out.stat().st_size > 5_000
    row = {"name": "it1", "observation": "memory pinned at the floor", "knob": "min-memory-mb=50", "result": "better"}
    assert fig9_tuning_log([row], tmp_path / "fig9.png").stat().st_size > 1_000
