"""Shared fixtures for the Plugin Interface / SDK tests.

The SDK (``packages/terasim-plugin-sdk``) and the example plugin
(``examples/plugins/webster_fixed_time``) are put on ``sys.path`` here, so the
tests run from a plain checkout without installing either distribution.

Tests that run SUMO carry ``needs_sumo`` (the ``sumo`` binary and ``libsumo`` must be
available) plus the repository's ``integration`` / ``requires_sumo`` markers. Test
names avoid the substrings the repo-root conftest turns into markers.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_DIR = REPO_ROOT / "packages" / "terasim-plugin-sdk"
EXAMPLE_DIR = REPO_ROOT / "examples" / "plugins" / "webster_fixed_time"
DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"

for _path in (SDK_DIR, EXAMPLE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _autodetect_sumo_home() -> None:
    """Point SUMO_HOME at the installed SUMO when unset (the repo-root conftest needs it)."""
    current = os.environ.get("SUMO_HOME")
    if current and Path(current).is_dir():
        return
    spec = importlib.util.find_spec("sumo")
    if spec is not None and spec.origin:
        os.environ["SUMO_HOME"] = str(Path(spec.origin).parent)
        return
    binary = shutil.which("sumo")
    if binary:
        candidate = Path(binary).resolve().parent.parent
        if (candidate / "bin").is_dir():
            os.environ["SUMO_HOME"] = str(candidate)


_autodetect_sumo_home()


def _sumo_runtime_available() -> bool:
    return (
        shutil.which("sumo") is not None
        and importlib.util.find_spec("libsumo") is not None
        and importlib.util.find_spec("sumolib") is not None
    )


needs_sumo = pytest.mark.skipif(
    not _sumo_runtime_available(),
    reason="needs the sumo binary plus libsumo/sumolib (install eclipse-sumo)",
)

needs_demo = pytest.mark.skipif(
    not (DEMO_DIR / "demo.sumocfg").is_file(),
    reason=f"demo corridor not present at {DEMO_DIR}",
)

#: Every test that simulates: skip cleanly without SUMO, and carry the repo markers.
simulates = [
    pytest.mark.integration,
    pytest.mark.requires_sumo,
    needs_sumo,
    needs_demo,
]


def apply(marks):
    """Decorator applying a list of marks."""

    def decorate(func):
        for mark in reversed(marks):
            func = mark(func)
        return func

    return decorate


@pytest.fixture
def demo_dir() -> Path:
    return DEMO_DIR


@pytest.fixture
def fresh_default_registry():
    """A clean process-wide registry for the test, restored afterwards."""
    from terasim_plugin_sdk import reset_default_registry

    reset_default_registry()
    yield
    reset_default_registry()


# ---------------------------------------------------------------------------
# A NEMA plan document for the demo corridor
# ---------------------------------------------------------------------------

#: The demo corridor's TMC intersections: ids and coordinates (exactly on J1/J2/J3).
DEMO_INTERSECTIONS = (
    ("i1", 37.335, -121.90339),
    ("i2", 37.335, -121.9),
    ("i3", 37.335, -121.89661),
)


def _nema_intersection(intersection_id: str, lat: float, lon: float) -> dict:
    """Standard 8-phase dual-ring NEMA intersection; rights ride with the throughs."""
    movements = {
        1: ["WB-left"],
        2: ["EB-through", "EB-right"],
        3: ["SB-left"],
        4: ["NB-through", "NB-right"],
        5: ["EB-left"],
        6: ["WB-through", "WB-right"],
        7: ["NB-left"],
        8: ["SB-through", "SB-right"],
    }
    return {
        "intersection_id": intersection_id,
        "name": f"Demo {intersection_id}",
        "lat": lat,
        "lon": lon,
        "phases": [{"phase": number, "movements": moves} for number, moves in movements.items()],
        "rings": {
            "ring1": [1, 2, 3, 4],
            "ring2": [5, 6, 7, 8],
            "barrier_phases": [4, 8],
            "barrier2_phases": [2, 6],
            "coordinated_phases": [2, 6],
        },
        "patterns": [
            {
                "pattern_id": "am_peak",
                "cycle_s": 100,
                "offset_s": 0,
                "splits": {"1": 15, "2": 35, "3": 15, "4": 35, "5": 15, "6": 35, "7": 15, "8": 35},
            }
        ],
        "schedule": [
            {
                "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                "start": "07:00",
                "pattern_id": "am_peak",
            },
        ],
    }


def write_demo_nema_plan(directory: Path, *, intersections=DEMO_INTERSECTIONS) -> Path:
    """Write a ``terasim.nema_plan.v1`` document for (some of) the demo intersections."""
    document = {
        "schema": "terasim.nema_plan.v1",
        "corridor_id": "demo_arterial",
        "intersections": [_nema_intersection(*item) for item in intersections],
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "demo_nema_plan.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# An installed-looking distribution that publishes entry points
# ---------------------------------------------------------------------------


def make_distribution(
    site: Path,
    name: str,
    entry_points: "dict[str, str]",
    *,
    modules: "dict[str, str] | None" = None,
    version: str = "1.0.0",
) -> Path:
    """Create ``<site>/<name>-<version>.dist-info`` (+ modules) that importlib.metadata finds.

    Put ``site`` on ``sys.path`` (``monkeypatch.syspath_prepend``) to "install" it.
    """
    site.mkdir(parents=True, exist_ok=True)
    dist_info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
    )
    lines = ["[terasim.operation_plugins]"] + [
        f"{key} = {value}" for key, value in entry_points.items()
    ]
    (dist_info / "entry_points.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for module, source in (modules or {}).items():
        (site / f"{module}.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return dist_info
