"""Shared fixtures and path constants for the terasim-city test suite.

Every map- and data-dependent test runs on the synthetic demo corridor
(``examples/corridor_demo``): a three-signal arterial (``J1``/``J2``/``J3``)
with TMC intersections ``i1``/``i2``/``i3`` exactly on the signals. The paths
and the map-variant builders live in :mod:`tests.fixtures.demo_corridor`, the
TMC payloads in :mod:`tests.fixtures.tmc_templates` and the NEMA plans in
:mod:`tests.fixtures.nema_plan_files`; this module re-exports the common paths
and wraps the payloads as session-scoped files.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

import pytest

from tests.fixtures.demo_corridor import (  # noqa: F401  (re-exported)
    DEMO_COORDS,
    DEMO_CORRIDOR_ID,
    DEMO_DIR,
    DEMO_INTERSECTION_TLS,
    DEMO_NET,
    DEMO_ROU,
    DEMO_SCENARIO_SUMOCFG,
    DEMO_SUMOCFG,
    DEMO_TLS,
    DEMO_TLS_IDS,
    DEMO_TMC_JSON,
    REPO_ROOT,
)
from tests.fixtures.tmc_templates import ten_minute_tmc, two_signal_tmc, write_tmc


# ---------------------------------------------------------------------------
# SUMO_HOME auto-detection
# ---------------------------------------------------------------------------
# The repo-root ``tests/conftest.py`` has a ``pytest_runtest_setup`` hook
# that skips any test carrying ``@pytest.mark.requires_sumo`` unless
# ``$SUMO_HOME`` is set to an existing directory, defaulting to
# ``/usr/share/sumo``. That default works on Linux package installs but
# not on Homebrew / pyenv / conda installs (where ``sumo`` lives under
# the env's own ``share/sumo``). To avoid false-skipping the local dev
# loop, derive ``SUMO_HOME`` from wherever the ``sumo`` binary actually
# lives, but only if not already explicitly set by the user.

def _autodetect_sumo_home() -> None:
    current = os.environ.get("SUMO_HOME")
    if current and Path(current).is_dir():
        return  # honour the user's explicit setting
    sumo_bin = shutil.which("sumo")
    if not sumo_bin:
        return  # nothing to derive; tests will skip cleanly via the marker
    # Conventionally ``$SUMO_HOME/bin/sumo`` so ``SUMO_HOME`` is two parents up.
    candidate = Path(sumo_bin).resolve().parent.parent
    if (candidate / "tools").is_dir() or (candidate / "bin").is_dir():
        os.environ["SUMO_HOME"] = str(candidate)


_autodetect_sumo_home()


# ---------------------------------------------------------------------------
# Synthetic payloads as files
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def demo_sumocfg() -> Path:
    """The demo corridor's base sumocfg (net + placeholder routes + programs)."""
    return DEMO_SUMOCFG


@pytest.fixture(scope="session")
def two_signal_tmc_json(tmp_path_factory) -> Path:
    """One-hour TMC payload for ``i1``/``i2`` only, written to a file."""
    return write_tmc(tmp_path_factory.mktemp("two_signal_tmc"), two_signal_tmc())


@pytest.fixture(scope="session")
def ten_minute_tmc_json(tmp_path_factory) -> Path:
    """Production-shaped ten-minute TMC payload for ``i2``, written to a file."""
    return write_tmc(tmp_path_factory.mktemp("ten_minute_tmc"), ten_minute_tmc())


# ---------------------------------------------------------------------------
# Skip helpers
# ---------------------------------------------------------------------------

def _sumolib_importable() -> bool:
    """Return True iff ``sumolib`` can be imported in this environment.

    The pipeline integration tests need ``sumolib`` (pure Python; bundled with
    the SUMO Python distribution). We test for importability rather than the
    presence of ``$SUMO_HOME`` because libsumo / TraCI are not required —
    only the network-parsing helpers are.
    """
    return importlib.util.find_spec("sumolib") is not None


def _sumo_runtime_available() -> bool:
    """Return True iff a full SUMO runtime is available.

    Stricter than :func:`_sumolib_importable`: sumolib is pure Python and can
    parse ``net.xml`` without anything else, but actually running a simulation
    requires:

      * The ``sumo`` (or ``sumo-gui``) binary on ``PATH``,
      * The ``traci`` Python module — what TeraSim's ``Simulator`` uses to
        drive the simulation step-by-step.

    Tests that call ``Simulator(...).run()`` (i.e. the incident-injection
    integration test) need this stricter check so they cleanly skip on
    machines that only have sumolib installed.
    """
    return (
        shutil.which("sumo") is not None
        and importlib.util.find_spec("traci") is not None
    )


requires_sumolib = pytest.mark.skipif(
    not _sumolib_importable(),
    reason="sumolib not importable (install eclipse-sumo or pip install sumolib)",
)

requires_sumo_runtime = pytest.mark.skipif(
    not _sumo_runtime_available(),
    reason="needs `sumo` binary on PATH and the `traci` Python module "
           "(install eclipse-sumo)",
)

requires_netconvert = pytest.mark.skipif(
    shutil.which("netconvert") is None,
    reason="needs the `netconvert` binary on PATH (install eclipse-sumo)",
)
