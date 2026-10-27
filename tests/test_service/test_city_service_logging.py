"""The service shows the city pipeline's INFO lines (warm starts, skipped scheduler ticks).

uvicorn configures only its own loggers and leaves the root at WARNING, so without this
the ``terasim_city`` / ``terasim_service.city`` INFO lines never reach the container log.
The configuration is applied by the console entry point only, never by ``create_app``,
so tests that build the app keep the default logging.
"""

from __future__ import annotations

import copy
import subprocess
import sys

from uvicorn.config import LOGGING_CONFIG

from terasim_service.city.common import CITY_LOGGERS, city_log_config


def test_city_log_config_adds_info_loggers_and_keeps_uvicorns():
    original = copy.deepcopy(LOGGING_CONFIG)

    config = city_log_config(LOGGING_CONFIG)

    assert LOGGING_CONFIG == original  # not mutated
    assert set(CITY_LOGGERS) == {"terasim_city", "terasim_service.city"}
    for name in CITY_LOGGERS:
        entry = config["loggers"][name]
        assert entry["level"] == "INFO"
        assert entry["handlers"] and all(h in config["handlers"] for h in entry["handlers"])
        assert entry["propagate"] is False
    for name, entry in original["loggers"].items():
        assert config["loggers"][name] == entry
    assert config["disable_existing_loggers"] is False


def test_city_info_lines_reach_stderr_under_the_config():
    script = (
        "import logging, logging.config\n"
        "from uvicorn.config import LOGGING_CONFIG\n"
        "from terasim_service.city.common import city_log_config\n"
        "logging.config.dictConfig(city_log_config(LOGGING_CONFIG))\n"
        "logging.getLogger('terasim_city.orchestration.corridor_optimization').info('seed used')\n"
        "logging.getLogger('terasim_service.city.scheduler').info('tick skipped')\n"
        "logging.getLogger('terasim_city.other').debug('not shown')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert "INFO terasim_city.orchestration.corridor_optimization: seed used" in done.stderr
    assert "INFO terasim_service.city.scheduler: tick skipped" in done.stderr
    assert "not shown" not in done.stderr


def test_the_entry_point_runs_uvicorn_with_the_city_log_config(monkeypatch):
    import terasim_service.__main__ as entry

    calls: list[dict] = []
    monkeypatch.setattr(entry, "create_app", lambda: "app")
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))

    entry.main()

    (kwargs,) = calls
    assert kwargs["host"] == "0.0.0.0" and kwargs["port"] == 8000
    assert kwargs["log_config"]["loggers"]["terasim_city"]["level"] == "INFO"
