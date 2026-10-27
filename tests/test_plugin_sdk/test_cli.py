"""The ``terasim-plugin`` command line: list, run, check and its exit codes."""

from __future__ import annotations

import importlib.util
import json

import pytest
from terasim_plugin_sdk.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK, main

from tests.test_plugin_sdk.conftest import DEMO_DIR, apply, make_distribution, needs_demo, simulates

pytestmark = [
    needs_demo,
    pytest.mark.skipif(importlib.util.find_spec("sumolib") is None, reason="needs sumolib"),
]


@pytest.fixture
def example_installed(tmp_path, monkeypatch, fresh_default_registry):
    """The example plugin published through an entry point, as ``pip install`` would."""
    site = tmp_path / "site"
    make_distribution(
        site,
        "terasim-webster-fixed-time",
        {"webster_fixed_time": "webster_fixed_time:WebsterFixedTimePlugin"},
        version="0.1.0",
    )
    monkeypatch.syspath_prepend(str(site))


def _run_args(out, *extra, name="signal_timing"):
    return [
        "run",
        name,
        "--map",
        str(DEMO_DIR / "demo.sumocfg"),
        "--tmc",
        str(DEMO_DIR / "demo_tmc.json"),
        "--from",
        "i1",
        "--to",
        "i3",
        "--sim-end",
        "300",
        "--out",
        str(out),
        "--quiet",
        *extra,
    ]


def test_list_shows_builtin_and_installed_plugins(example_installed, capsys):
    assert main(["list"]) == EXIT_OK
    text = capsys.readouterr().out
    assert "signal_timing" in text and "builtin (terasim-plugin-sdk)" in text
    assert "webster_fixed_time" in text and "entry_point (terasim-webster-fixed-time 0.1.0)" in text

    assert main(["list", "--json"]) == EXIT_OK
    listing = json.loads(capsys.readouterr().out)
    assert [plugin["name"] for plugin in listing["plugins"]] == [
        "signal_timing",
        "webster_fixed_time",
    ]
    assert listing["load_errors"] == []


def test_list_reports_broken_entry_points_on_stderr(
    tmp_path, monkeypatch, fresh_default_registry, capsys
):
    site = tmp_path / "site"
    make_distribution(site, "broken-ops", {"broken_ops": "missing_module_for_cli_test:Plugin"})
    monkeypatch.syspath_prepend(str(site))
    assert main(["list"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "broken_ops" not in captured.out
    assert "skipped entry point 'broken_ops'" in captured.err


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--weights", "speed=1"], "unknown weight key"),
        (["--weights", "control_delay=-1"], "must be > 0"),
        (["--param", "colour=blue"], "does not accept params"),
        (["--plan", "/nonexistent/sheet.json"], "current plan not found"),
        (["--tls", "J1,J2"], "--tls or --from/--to"),
        (["--plan-time", "2026-07-01T08:00:00"], "--plan-time needs --plan"),
        (["--workers", "0"], "n_workers"),
    ],
)
def test_invalid_requests_exit_2(tmp_path, capsys, fresh_default_registry, argv, message):
    assert main(_run_args(tmp_path / "out", *argv)) == EXIT_INVALID
    assert message in capsys.readouterr().err


def test_missing_map_and_unknown_plugin_exit_2(tmp_path, capsys, fresh_default_registry):
    args = _run_args(tmp_path / "out")
    args[args.index("--map") + 1] = str(tmp_path / "missing.sumocfg")
    assert main(args) == EXIT_INVALID
    assert "map file not found" in capsys.readouterr().err

    assert main(_run_args(tmp_path / "out", name="ramp_metering")) == EXIT_INVALID
    assert "no operation plugin named 'ramp_metering'" in capsys.readouterr().err

    stored = _run_args(tmp_path / "out")
    stored[stored.index("--map") + 1] = "0123456789ab"
    (tmp_path / "empty_store").mkdir()
    assert main([*stored, "--data-root", str(tmp_path / "empty_store")]) == EXIT_INVALID
    assert "no stored map with id" in capsys.readouterr().err


def test_usage_errors_and_version(capsys):
    assert main(["run", "signal_timing"]) == 2  # argparse: required options missing
    assert main(["--version"]) == 0
    assert "terasim-plugin-sdk 0.1.0" in capsys.readouterr().out


@apply(simulates)
def test_run_and_check_the_example_from_the_command_line(tmp_path, capsys, example_installed):
    out = tmp_path / "out"
    code = main(
        _run_args(out, "--plan", str(DEMO_DIR / "baseline_sheet.json"), name="webster_fixed_time")
    )
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    assert "webster_fixed_time 0.1.0 on corridor demo_arterial (J1, J2, J3)" in captured.out
    # Count-derived Webster stages carry no NEMA phase numbers: an engineer assigns them.
    assert "handoff ready: no" in captured.out
    assert (out / "operation_result.json").is_file() and (out / "nema_plan.json").is_file()

    assert main(["check", str(out)]) == EXIT_OK
    assert "conformant" in capsys.readouterr().out

    result = json.loads((out / "operation_result.json").read_text(encoding="utf-8"))
    result["kpis"]["optimized"]["cost"] = result["kpis"]["baseline"]["cost"] + 1.0
    (out / "operation_result.json").write_text(json.dumps(result), encoding="utf-8")
    assert main(["check", str(out / "operation_result.json")]) == EXIT_FAILED
    assert "worse than kpis.baseline.cost" in capsys.readouterr().out
