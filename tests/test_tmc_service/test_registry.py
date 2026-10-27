"""IntersectionRegistry: spec-driven activation with a JSON persistence file."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from terasim_tmc_service.registry import (
    REGISTRATION_RADIUS_M,
    CorridorMismatchError,
    IntersectionRegistry,
    IntersectionTooFarError,
    UnknownIntersectionError,
    distance_m,
)

_NOW = datetime(2026, 9, 4, 18, 0, 0, tzinfo=timezone.utc)
_LAT, _LON = 37.335, -121.90339  # the spec location of i1


def _registry(spec, tmp_path=None):
    path = tmp_path / "registry.json" if tmp_path is not None else None
    return IntersectionRegistry(spec, path, clock=lambda: _NOW)


def test_register_known_intersection_returns_record(spec):
    registry = _registry(spec)
    record = registry.register("i1", "Main St & 1st Ave", _LAT, _LON)
    assert record.to_dict() == {
        "intersection_id": "i1",
        "name": "Main St & 1st Ave",
        "lat": 37.335,
        "lon": -121.90339,
        "corridor_id": "demo_arterial",
        "sensors": ["i1-cam1", "i1-cam2", "i1-cam3", "i1-cam4"],
        "registered_at": "2026-09-04T18:00:00Z",
    }
    assert registry.registered_ids() == ["i1"]
    assert registry.get("i1") == record
    assert registry.get("i2") is None


def test_register_unknown_intersection_lists_known_ids(spec):
    with pytest.raises(UnknownIntersectionError, match="i1"):
        _registry(spec).register("oak", "Park Ave & Oak St", _LAT, _LON)


def test_unknown_intersection_message_is_clean(spec):
    with pytest.raises(UnknownIntersectionError) as excinfo:
        _registry(spec).register("oak", "Park Ave & Oak St", _LAT, _LON)
    assert str(excinfo.value) == "unknown intersection_id 'oak'; known ids: ['i1', 'i2', 'i3']"


def test_register_rejects_coordinates_far_from_spec(spec):
    assert REGISTRATION_RADIUS_M == 50.0
    with pytest.raises(IntersectionTooFarError):
        _registry(spec).register("i1", "Main", _LAT + 0.01, _LON)


def test_register_rejects_the_location_of_a_neighbouring_intersection(spec):
    # i2 is about 300 m east of i1: a valid id with the wrong coordinates is refused.
    with pytest.raises(IntersectionTooFarError, match="'i1'"):
        _registry(spec).register("i1", "Main", 37.335, -121.9)


def test_register_accepts_coordinates_within_radius(spec):
    record = _registry(spec).register("i1", "Main", _LAT + 0.0002, _LON)
    assert record.intersection_id == "i1"


def test_register_rejects_corridor_mismatch(spec):
    with pytest.raises(CorridorMismatchError, match="demo_arterial"):
        _registry(spec).register("i1", "Main", _LAT, _LON, corridor_id="other")


def test_register_is_idempotent_and_updates_name(spec):
    registry = _registry(spec)
    registry.register("i1", "old name", _LAT, _LON)
    registry.register("i1", "new name", _LAT, _LON)
    assert [record.name for record in registry.list()] == ["new name"]


def test_registry_persists_and_reloads(spec, tmp_path):
    registry = _registry(spec, tmp_path)
    registry.register("i1", "Main", _LAT, _LON)
    stored = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    assert stored["intersections"][0]["intersection_id"] == "i1"

    reloaded = _registry(spec, tmp_path)
    assert reloaded.registered_ids() == ["i1"]
    assert reloaded.get("i1").name == "Main"


def test_registry_ignores_persisted_ids_missing_from_spec(spec, tmp_path):
    (tmp_path / "registry.json").write_text(
        json.dumps({"intersections": [{"intersection_id": "gone", "name": "x", "lat": 0,
                                       "lon": 0, "corridor_id": "demo_arterial",
                                       "sensors": [], "registered_at": "2026-01-01T00:00:00Z"}]}),
        encoding="utf-8",
    )
    assert _registry(spec, tmp_path).registered_ids() == []


def test_distance_m_haversine():
    assert distance_m(_LAT, _LON, _LAT, _LON) == 0.0
    assert 1100 < distance_m(_LAT, _LON, _LAT + 0.01, _LON) < 1120
