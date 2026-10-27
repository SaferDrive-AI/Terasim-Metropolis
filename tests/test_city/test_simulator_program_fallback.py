"""Simulator must load a net whose TLS programs sumolib cannot parse."""
from __future__ import annotations

import sumolib

from tests.test_city.conftest import REPO_ROOT

DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"


def test_simulator_loads_net_without_programs_when_sumolib_rejects_durations(monkeypatch, tmp_path):
    """sumolib < 1.27 int()-parses phase durations; Simulator must fall back."""
    from terasim.simulator import Simulator

    calls: list[bool] = []
    real_read_net = sumolib.net.readNet

    def fake_read_net(path, **kwargs):
        calls.append(bool(kwargs.get("withPrograms")))
        if kwargs.get("withPrograms"):
            raise ValueError("invalid literal for int() with base 10: '4.50'")
        return real_read_net(path, **kwargs)

    monkeypatch.setattr(sumolib.net, "readNet", fake_read_net)
    sim = Simulator(
        sumo_net_file_path=DEMO_DIR / "demo.net.xml",
        sumo_config_file_path=DEMO_DIR / "demo.sumocfg",
        gui_flag=False,
        output_path=tmp_path,
    )
    assert calls == [True, False]
    assert sim.sumo_net.getEdges()


def _build_simulators(monkeypatch, tmp_path, count: int) -> list[tuple[str, str]]:
    """Build ``count`` Simulators on a net sumolib rejects; return the fallback log lines."""
    import terasim.simulator as simulator_module
    from loguru import logger

    real_read_net = sumolib.net.readNet

    def fake_read_net(path, **kwargs):
        if kwargs.get("withPrograms"):
            raise ValueError("invalid literal for int() with base 10: '4.50'")
        return real_read_net(path, **kwargs)

    monkeypatch.setattr(sumolib.net, "readNet", fake_read_net)
    lines: list[tuple[str, str]] = []
    sink = logger.add(
        lambda message: lines.append(
            (message.record["level"].name, message.record["message"])
        ),
        level="DEBUG",
        filter=lambda record: "could not parse the TLS programs" in record["message"],
    )
    try:
        for trial in range(count):
            simulator_module.Simulator(
                sumo_net_file_path=DEMO_DIR / "demo.net.xml",
                sumo_config_file_path=DEMO_DIR / "demo.sumocfg",
                gui_flag=False,
                output_path=tmp_path / f"trial_{trial}",
            )
    finally:
        logger.remove(sink)
    return lines


def test_the_fallback_is_a_warning_for_a_standalone_simulator(monkeypatch, tmp_path):
    from terasim.simulator import QUIET_PROGRAM_FALLBACK_ENV

    monkeypatch.delenv(QUIET_PROGRAM_FALLBACK_ENV, raising=False)
    lines = _build_simulators(monkeypatch, tmp_path, 2)
    assert [level for level, _message in lines] == ["WARNING", "WARNING"]


def test_the_fallback_is_debug_when_the_run_already_warned(monkeypatch, tmp_path):
    """The corridor optimization warns once per run and sets the variable for its trials,
    each of which builds its Simulator in a fresh process."""
    from terasim.simulator import QUIET_PROGRAM_FALLBACK_ENV

    assert QUIET_PROGRAM_FALLBACK_ENV == "TERASIM_QUIET_PROGRAM_FALLBACK"
    monkeypatch.setenv(QUIET_PROGRAM_FALLBACK_ENV, "1")
    lines = _build_simulators(monkeypatch, tmp_path, 2)
    assert [level for level, _message in lines] == ["DEBUG", "DEBUG"]
