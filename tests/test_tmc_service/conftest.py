"""Shared fixtures: the synthetic corridor spec and synthetic VSS behavior objects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tmc_service"
METADATA_PATH = FIXTURE_DIR / "demo_arterial_metadata.json"

# Intersection i1 of the demo corridor (signal J1 of examples/corridor_demo).
I1 = {"intersection_id": "i1", "name": "Main St & 1st Ave", "lat": 37.335, "lon": -121.90339}

# Edge ids are the demo network's node pairs, as listed in the Headings table of
# demo_arterial_metadata.json ("w1-j1" runs from node W1 to node J1).
# sensor -> (entry edge, {movement: exit edge}); the exit edge's heading combined
# with the entry heading selects the movement in the Movements table.
APPROACH: dict[str, tuple[str, dict[str, str]]] = {
    "i1-cam1": (  # southbound
        "j1n1-j1",
        {
            "Straight": "j1-j1s1",
            "Right": "j1-w1",
            "Left": "j1-j2",
        },
    ),
    "i1-cam2": (  # westbound
        "j2-j1",
        {
            "Straight": "j1-w1",
            "Right": "j1-j1n1",
            "Left": "j1-j1s1",
        },
    ),
    "i1-cam3": (  # northbound
        "j1s1-j1",
        {
            "Straight": "j1-j1n1",
            "Right": "j1-j2",
            "Left": "j1-w1",
        },
    ),
    "i1-cam4": (  # eastbound
        "w1-j1",
        {
            "Straight": "j1-j2",
            "Right": "j1-j1s1",
            "Left": "j1-j1n1",
        },
    ),
}

WINDOW_START = "2026-09-04T18:20:00Z"
WINDOW_END = "2026-09-04T18:29:59Z"


def make_behavior(
    sensor: str,
    object_id,
    edges,
    *,
    end: str = "2026-09-04T18:25:00.000Z",
    object_type: str = "Vehicle",
) -> dict:
    """One behavior object in the shape the VSS behavior API returns."""
    return {
        "sensor": {"id": sensor},
        "object": {"id": str(object_id), "type": object_type},
        "edges": list(edges),
        "end": end,
    }


def approach_behaviors(sensor: str, counts: dict[str, int], *, start_id: int = 1) -> list[dict]:
    """``counts`` maps movement -> number of vehicles for one camera/approach."""
    entry_edge, exit_edges = APPROACH[sensor]
    behaviors = []
    object_id = start_id
    for movement, number in counts.items():
        for _ in range(number):
            behaviors.append(make_behavior(sensor, object_id, [entry_edge, exit_edges[movement]]))
            object_id += 1
    return behaviors


@pytest.fixture
def metadata_payload() -> dict:
    return json.loads(METADATA_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def spec():
    from terasim_tmc_service.spec import CorridorSpec

    return CorridorSpec.from_json(METADATA_PATH)


@pytest.fixture
def sample_behaviors() -> dict[str, list[dict]]:
    """Per-sensor behaviors with known expected counts plus objects that must be skipped."""
    cam1 = approach_behaviors("i1-cam1", {"Straight": 3, "Right": 1, "Left": 1})
    cam1.append(make_behavior("i1-cam1", "p1", [APPROACH["i1-cam1"][0]], object_type="Person"))
    cam1.append(make_behavior("i1-cam1", "e1", []))
    cam1.append(make_behavior("i1-cam1", "u1", ["unknown-edge-a", "unknown-edge-b"]))
    return {
        "i1-cam1": cam1,
        "i1-cam2": approach_behaviors("i1-cam2", {"Straight": 2}, start_id=100),
        "i1-cam3": approach_behaviors("i1-cam3", {"Straight": 1, "Right": 1}, start_id=200),
        "i1-cam4": approach_behaviors("i1-cam4", {"Straight": 2, "Left": 1}, start_id=300),
    }


EXPECTED_COUNTS = {
    "South": {"Right": 1, "Straight": 3, "Left": 1},
    "West": {"Right": 0, "Straight": 2, "Left": 0},
    "North": {"Right": 1, "Straight": 1, "Left": 0},
    "East": {"Right": 0, "Straight": 2, "Left": 1},
}
