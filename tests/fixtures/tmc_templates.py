"""TMC / timing-sheet templates for tests (all synthetic).

Every dataset is bound to the synthetic demo corridor (``examples/corridor_demo``:
corridor ``demo_arterial``, intersections ``i1``/``i2``/``i3`` exactly on
signals ``J1``/``J2``/``J3``):

* :func:`demo_tmc`, :func:`baseline_sheet`, :func:`baseline_sheet_uncoordinated`
  and :func:`demo_snapshots` are post-``json.load`` copies of the corridor
  demo's own ``demo_tmc.json``, ``baseline_sheet*.json`` and
  ``snapshots/snap_01..06.json``, transcribed as Python literals so tests do
  not read JSON data files from ``examples/`` at runtime;
* :func:`two_signal_tmc` is a one-hour payload for the first two signals only
  (``i1``/``i2``);
* :func:`ten_minute_tmc` is a production-shaped ten-minute payload for one
  intersection (``i2``): a UTC ``Z`` timestamp with milliseconds, approaches
  listed South/West/North/East, turns Right/Straight/Left, zero left turns on
  three approaches.

Each dataset accessor returns a fresh ``copy.deepcopy`` of the underlying
literal, so callers may freely mutate the returned structure without affecting
the shared template or one another.
"""

import copy
import json
from pathlib import Path

_DEMO_TMC = {
    "corridor_id": "demo_arterial",
    "observation_interval": "1h",
    "timestamp_start": "2026-07-01T08:00:00",
    "intersections": [
        {
            "intersection_id": "i1",
            "lat": 37.335,
            "lon": -121.90339,
            "counts": {
                "East": {"Left": 60, "Straight": 560, "Right": 90},
                "West": {"Left": 50, "Straight": 400, "Right": 80},
                "North": {"Left": 45, "Straight": 120, "Right": 70},
                "South": {"Left": 40, "Straight": 110, "Right": 60},
            },
        },
        {
            "intersection_id": "i2",
            "lat": 37.335,
            "lon": -121.9,
            "counts": {
                "East": {"Left": 70, "Straight": 590, "Right": 80},
                "West": {"Left": 55, "Straight": 430, "Right": 70},
                "North": {"Left": 50, "Straight": 140, "Right": 80},
                "South": {"Left": 45, "Straight": 130, "Right": 65},
            },
        },
        {
            "intersection_id": "i3",
            "lat": 37.335,
            "lon": -121.89661,
            "counts": {
                "East": {"Left": 55, "Straight": 520, "Right": 100},
                "West": {"Left": 60, "Straight": 380, "Right": 90},
                "North": {"Left": 40, "Straight": 100, "Right": 60},
                "South": {"Left": 50, "Straight": 150, "Right": 75},
            },
        },
    ],
}


def _timing_sheet_intersection(intersection_id, tls_id, offset_s):
    return {
        "intersection_id": intersection_id,
        "tls_id": tls_id,
        "offset_s": offset_s,
        "phases": [
            {
                "nema_phases": [2, 6],
                "movements": [
                    "EB-left",
                    "EB-through",
                    "EB-right",
                    "WB-left",
                    "WB-through",
                    "WB-right",
                ],
                "green_s": 48,
                "yellow_s": 3,
                "all_red_s": 1,
            },
            {
                "nema_phases": [4, 8],
                "movements": [
                    "NB-left",
                    "NB-through",
                    "NB-right",
                    "SB-left",
                    "SB-through",
                    "SB-right",
                ],
                "green_s": 34,
                "yellow_s": 3,
                "all_red_s": 1,
            },
        ],
    }


_BASELINE_SHEET = {
    "schema": "terasim.timing_sheet.v1",
    "corridor_id": "demo_arterial",
    "cycle_s": 90,
    "intersections": [
        _timing_sheet_intersection("i1", "J1", 0),
        _timing_sheet_intersection("i2", "J2", 20),
        _timing_sheet_intersection("i3", "J3", 40),
    ],
}


_BASELINE_SHEET_UNCOORDINATED = {
    "schema": "terasim.timing_sheet.v1",
    "corridor_id": "demo_arterial",
    "cycle_s": 90,
    "intersections": [
        _timing_sheet_intersection("i1", "J1", 0),
        _timing_sheet_intersection("i2", "J2", 0),
        _timing_sheet_intersection("i3", "J3", 0),
    ],
}


_TWO_SIGNAL_TMC = {
    "corridor_id": "demo_arterial",
    "observation_interval": "1h",
    "timestamp_start": "2026-07-01T08:00:00",
    "intersections": [
        {
            "intersection_id": "i1",
            "lat": 37.335,
            "lon": -121.90339,
            "counts": {
                "East": {"Left": 110, "Straight": 240, "Right": 90},
                "West": {"Left": 80, "Straight": 230, "Right": 100},
                "North": {"Left": 85, "Straight": 120, "Right": 55},
                "South": {"Left": 45, "Straight": 105, "Right": 50},
            },
        },
        {
            "intersection_id": "i2",
            "lat": 37.335,
            "lon": -121.9,
            "counts": {
                "East": {"Left": 95, "Straight": 230, "Right": 100},
                "West": {"Left": 75, "Straight": 215, "Right": 95},
                "North": {"Left": 60, "Straight": 115, "Right": 45},
                "South": {"Left": 40, "Straight": 100, "Right": 55},
            },
        },
    ],
}


_TEN_MINUTE_TMC = {
    "corridor_id": "demo_arterial",
    "observation_interval": "10m",
    "timestamp_start": "2026-07-01T15:20:00.000Z",
    "intersections": [
        {
            "intersection_id": "i2",
            "lat": 37.335,
            "lon": -121.9,
            "counts": {
                "South": {"Right": 5, "Straight": 21, "Left": 0},
                "West": {"Right": 7, "Straight": 96, "Left": 0},
                "North": {"Right": 4, "Straight": 18, "Left": 0},
                "East": {"Right": 9, "Straight": 104, "Left": 3},
            },
        },
    ],
}


def demo_tmc():
    """Return a fresh copy of ``examples/corridor_demo/demo_tmc.json``."""
    return copy.deepcopy(_DEMO_TMC)


def baseline_sheet():
    """Return a fresh copy of ``examples/corridor_demo/baseline_sheet.json``."""
    return copy.deepcopy(_BASELINE_SHEET)


def baseline_sheet_uncoordinated():
    """Return a fresh copy of ``examples/corridor_demo/baseline_sheet_uncoordinated.json``."""
    return copy.deepcopy(_BASELINE_SHEET_UNCOORDINATED)


def two_signal_tmc():
    """Return a fresh one-hour TMC payload for ``i1``/``i2`` (signals ``J1``/``J2``)."""
    return copy.deepcopy(_TWO_SIGNAL_TMC)


def ten_minute_tmc():
    """Return a fresh production-shaped ten-minute TMC payload for ``i2`` (``J2``)."""
    return copy.deepcopy(_TEN_MINUTE_TMC)


def write_tmc(directory, data, name="tmc.json"):
    """Write ``data`` as JSON into ``directory/name``; return the file ``Path``."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with open(path, "w") as f:
        json.dump(data, f)
    return path


def write_snapshots(directory, snapshots):
    """Write each snapshot to ``directory/snap_{i:02d}.json`` (i from 1); return the dir ``Path``."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for i, snap in enumerate(snapshots, start=1):
        with open(directory / f"snap_{i:02d}.json", "w") as f:
            json.dump(snap, f)
    return directory


_DEMO_SNAPSHOTS = [{'corridor_id': 'demo_arterial',
  'observation_interval': '10min',
  'timestamp_start': '2026-07-01T08:00:00',
  'intersections': [{'intersection_id': 'i1',
                     'lat': 37.335,
                     'lon': -121.90339,
                     'counts': {'East': {'Left': 8, 'Straight': 79, 'Right': 13},
                                'West': {'Left': 7, 'Straight': 57, 'Right': 12},
                                'North': {'Left': 6, 'Straight': 17, 'Right': 10},
                                'South': {'Left': 6, 'Straight': 16, 'Right': 9}}},
                    {'intersection_id': 'i2',
                     'lat': 37.335,
                     'lon': -121.9,
                     'counts': {'East': {'Left': 10, 'Straight': 84, 'Right': 12},
                                'West': {'Left': 8, 'Straight': 61, 'Right': 10},
                                'North': {'Left': 7, 'Straight': 20, 'Right': 12},
                                'South': {'Left': 6, 'Straight': 18, 'Right': 9}}},
                    {'intersection_id': 'i3',
                     'lat': 37.335,
                     'lon': -121.89661,
                     'counts': {'East': {'Left': 8, 'Straight': 74, 'Right': 15},
                                'West': {'Left': 8, 'Straight': 54, 'Right': 13},
                                'North': {'Left': 6, 'Straight': 14, 'Right': 9},
                                'South': {'Left': 7, 'Straight': 21, 'Right': 11}}}]},
 {'corridor_id': 'demo_arterial',
  'observation_interval': '10min',
  'timestamp_start': '2026-07-01T08:10:00',
  'intersections': [{'intersection_id': 'i1',
                     'lat': 37.335,
                     'lon': -121.90339,
                     'counts': {'East': {'Left': 10, 'Straight': 91, 'Right': 14},
                                'West': {'Left': 8, 'Straight': 65, 'Right': 12},
                                'North': {'Left': 7, 'Straight': 20, 'Right': 11},
                                'South': {'Left': 7, 'Straight': 18, 'Right': 9}}},
                    {'intersection_id': 'i2',
                     'lat': 37.335,
                     'lon': -121.9,
                     'counts': {'East': {'Left': 11, 'Straight': 96, 'Right': 12},
                                'West': {'Left': 9, 'Straight': 70, 'Right': 11},
                                'North': {'Left': 8, 'Straight': 23, 'Right': 12},
                                'South': {'Left': 7, 'Straight': 21, 'Right': 10}}},
                    {'intersection_id': 'i3',
                     'lat': 37.335,
                     'lon': -121.89661,
                     'counts': {'East': {'Left': 9, 'Straight': 85, 'Right': 15},
                                'West': {'Left': 10, 'Straight': 62, 'Right': 14},
                                'North': {'Left': 7, 'Straight': 16, 'Right': 9},
                                'South': {'Left': 8, 'Straight': 24, 'Right': 12}}}]},
 {'corridor_id': 'demo_arterial',
  'observation_interval': '10min',
  'timestamp_start': '2026-07-01T08:20:00',
  'intersections': [{'intersection_id': 'i1',
                     'lat': 37.335,
                     'lon': -121.90339,
                     'counts': {'East': {'Left': 10, 'Straight': 95, 'Right': 16},
                                'West': {'Left': 8, 'Straight': 68, 'Right': 14},
                                'North': {'Left': 8, 'Straight': 20, 'Right': 12},
                                'South': {'Left': 7, 'Straight': 19, 'Right': 10}}},
                    {'intersection_id': 'i2',
                     'lat': 37.335,
                     'lon': -121.9,
                     'counts': {'East': {'Left': 12, 'Straight': 100, 'Right': 14},
                                'West': {'Left': 9, 'Straight': 73, 'Right': 12},
                                'North': {'Left': 8, 'Straight': 24, 'Right': 14},
                                'South': {'Left': 8, 'Straight': 22, 'Right': 11}}},
                    {'intersection_id': 'i3',
                     'lat': 37.335,
                     'lon': -121.89661,
                     'counts': {'East': {'Left': 9, 'Straight': 88, 'Right': 18},
                                'West': {'Left': 10, 'Straight': 65, 'Right': 16},
                                'North': {'Left': 7, 'Straight': 17, 'Right': 10},
                                'South': {'Left': 8, 'Straight': 25, 'Right': 13}}}]},
 {'corridor_id': 'demo_arterial',
  'observation_interval': '10min',
  'timestamp_start': '2026-07-01T08:30:00',
  'intersections': [{'intersection_id': 'i1',
                     'lat': 37.335,
                     'lon': -121.90339,
                     'counts': {'East': {'Left': 12, 'Straight': 107, 'Right': 18},
                                'West': {'Left': 10, 'Straight': 77, 'Right': 16},
                                'North': {'Left': 9, 'Straight': 23, 'Right': 14},
                                'South': {'Left': 8, 'Straight': 21, 'Right': 12}}},
                    {'intersection_id': 'i2',
                     'lat': 37.335,
                     'lon': -121.9,
                     'counts': {'East': {'Left': 13, 'Straight': 113, 'Right': 16},
                                'West': {'Left': 11, 'Straight': 82, 'Right': 14},
                                'North': {'Left': 10, 'Straight': 27, 'Right': 16},
                                'South': {'Left': 9, 'Straight': 25, 'Right': 13}}},
                    {'intersection_id': 'i3',
                     'lat': 37.335,
                     'lon': -121.89661,
                     'counts': {'East': {'Left': 11, 'Straight': 100, 'Right': 20},
                                'West': {'Left': 12, 'Straight': 73, 'Right': 18},
                                'North': {'Left': 8, 'Straight': 19, 'Right': 12},
                                'South': {'Left': 10, 'Straight': 29, 'Right': 15}}}]},
 {'corridor_id': 'demo_arterial',
  'observation_interval': '10min',
  'timestamp_start': '2026-07-01T08:40:00',
  'intersections': [{'intersection_id': 'i1',
                     'lat': 37.335,
                     'lon': -121.90339,
                     'counts': {'East': {'Left': 11, 'Straight': 101, 'Right': 15},
                                'West': {'Left': 9, 'Straight': 72, 'Right': 14},
                                'North': {'Left': 8, 'Straight': 22, 'Right': 12},
                                'South': {'Left': 7, 'Straight': 20, 'Right': 10}}},
                    {'intersection_id': 'i2',
                     'lat': 37.335,
                     'lon': -121.9,
                     'counts': {'East': {'Left': 13, 'Straight': 106, 'Right': 14},
                                'West': {'Left': 10, 'Straight': 78, 'Right': 12},
                                'North': {'Left': 9, 'Straight': 25, 'Right': 14},
                                'South': {'Left': 8, 'Straight': 23, 'Right': 11}}},
                    {'intersection_id': 'i3',
                     'lat': 37.335,
                     'lon': -121.89661,
                     'counts': {'East': {'Left': 10, 'Straight': 94, 'Right': 17},
                                'West': {'Left': 11, 'Straight': 68, 'Right': 15},
                                'North': {'Left': 7, 'Straight': 18, 'Right': 10},
                                'South': {'Left': 9, 'Straight': 27, 'Right': 13}}}]},
 {'corridor_id': 'demo_arterial',
  'observation_interval': '10min',
  'timestamp_start': '2026-07-01T08:50:00',
  'intersections': [{'intersection_id': 'i1',
                     'lat': 37.335,
                     'lon': -121.90339,
                     'counts': {'East': {'Left': 9, 'Straight': 86, 'Right': 14},
                                'West': {'Left': 8, 'Straight': 61, 'Right': 13},
                                'North': {'Left': 7, 'Straight': 18, 'Right': 11},
                                'South': {'Left': 6, 'Straight': 17, 'Right': 10}}},
                    {'intersection_id': 'i2',
                     'lat': 37.335,
                     'lon': -121.9,
                     'counts': {'East': {'Left': 11, 'Straight': 91, 'Right': 13},
                                'West': {'Left': 8, 'Straight': 66, 'Right': 11},
                                'North': {'Left': 8, 'Straight': 22, 'Right': 13},
                                'South': {'Left': 7, 'Straight': 20, 'Right': 10}}},
                    {'intersection_id': 'i3',
                     'lat': 37.335,
                     'lon': -121.89661,
                     'counts': {'East': {'Left': 8, 'Straight': 80, 'Right': 16},
                                'West': {'Left': 9, 'Straight': 58, 'Right': 14},
                                'North': {'Left': 6, 'Straight': 15, 'Right': 10},
                                'South': {'Left': 8, 'Straight': 23, 'Right': 12}}}]}]


def demo_snapshots():
    """Six 10-minute demo_arterial snapshots (i1/i2/i3), faithful to
    examples/corridor_demo/snapshots/snap_01..06.json. Sum -> one hour."""
    return copy.deepcopy(_DEMO_SNAPSHOTS)
