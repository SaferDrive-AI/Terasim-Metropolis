"""Unit tests for fail-loud stalled-vehicle incident file loading (``load_stalls``).

These are pure JSON-contract tests: no SUMO runtime or sample data required.
The key behaviour under test is that a *given* stall file degrades loudly
instead of silently turning into a no-incident run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from terasim_city.evaluation.diagnostics import load_stalls


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_none_path_is_no_incident() -> None:
    assert load_stalls(None) == []


def test_empty_list_is_the_explicit_no_incident_form(tmp_path: Path) -> None:
    path = _write(tmp_path / "stalls.json", {"stalled_vehicles": []})
    assert load_stalls(path) == []


def test_valid_stalls_are_parsed(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "stalls.json",
        {"stalled_vehicles": [{"latitude": 37.3, "longitude": -121.8}]},
    )
    assert load_stalls(path) == [{"latitude": 37.3, "longitude": -121.8}]


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_stalls(tmp_path / "does_not_exist.json")


def test_missing_key_is_not_silently_no_incident(tmp_path: Path) -> None:
    # The footgun: a stall file whose key is absent or typo'd must NOT be read
    # as "no incident" — that would run the wrong scenario yet still report a
    # confident "best plan".
    path = _write(tmp_path / "stalls.json", {"stalled_vehicle": []})
    with pytest.raises(ValueError, match="missing required 'stalled_vehicles' key"):
        load_stalls(path)


def test_non_list_stalled_vehicles_raises(tmp_path: Path) -> None:
    path = _write(tmp_path / "stalls.json", {"stalled_vehicles": {"latitude": 1}})
    with pytest.raises(ValueError, match="must be a list"):
        load_stalls(path)


def test_item_missing_coordinate_raises(tmp_path: Path) -> None:
    path = _write(tmp_path / "stalls.json", {"stalled_vehicles": [{"latitude": 37.3}]})
    with pytest.raises(ValueError, match=r"stalled_vehicles\[0\] missing required field"):
        load_stalls(path)


def test_bool_coordinate_rejected(tmp_path: Path) -> None:
    # JSON ``true`` would otherwise float() to 1.0 and silently place a stall.
    path = _write(
        tmp_path / "stalls.json",
        {"stalled_vehicles": [{"latitude": True, "longitude": -121.8}]},
    )
    with pytest.raises(ValueError, match="must be a number, not a bool"):
        load_stalls(path)


def test_non_finite_coordinate_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "stalls.json",
        {"stalled_vehicles": [{"latitude": "nan", "longitude": -121.8}]},
    )
    with pytest.raises(ValueError, match="must be finite"):
        load_stalls(path)


def test_out_of_range_coordinate_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "stalls.json",
        {"stalled_vehicles": [{"latitude": 999, "longitude": -121.8}]},
    )
    with pytest.raises(ValueError, match="out of range"):
        load_stalls(path)
