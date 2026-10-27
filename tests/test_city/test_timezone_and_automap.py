"""Regression tests for time-of-day plan selection with timezone-aware times.

An aware ``plan_time`` (or TMC ``timestamp_start``) must be converted into the
corridor's local wall-clock time — derived from the corridor's own
coordinates — before a stored schedule is consulted, never just stripped.
The corridor is the synthetic demo corridor (US Pacific time).
"""

from __future__ import annotations

import pytest

from tests.fixtures.tmc_templates import demo_tmc, write_tmc
from tests.test_city.conftest import DEMO_COORDS, DEMO_CORRIDOR_ID, DEMO_SUMOCFG


# --- timezone: aware plan_time must be converted, not stripped --------------

def _wd_am_plan() -> dict:
    return {
        "patterns": [{"pattern_id": "am", "cycle_s": 100},
                     {"pattern_id": "pm", "cycle_s": 100}],
        "schedule": [
            {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "07:00", "pattern_id": "am"},
            {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "15:00", "pattern_id": "pm"},
        ],
    }


def _seed_cfg(tmp_path, plan_time):
    from terasim_city.orchestration.corridor_optimization import (
        CorridorOptimizationConfig,
    )

    (tmp_path / "store").mkdir(parents=True, exist_ok=True)
    return CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_SUMOCFG,
        output_dir=tmp_path / "out",
        plan_data_root=tmp_path / "store",
        plan_time=plan_time,
    )


class _EmptyCorridor:
    intersections = ()


def test_timezone_for_locates_the_demo_corridor():
    from terasim_city.geo_timezone import timezone_for

    for lat, lon in DEMO_COORDS.values():
        assert timezone_for(lat, lon) == "America/Los_Angeles"


def test_aware_time_converts_via_coordinate_derived_timezone(tmp_path, monkeypatch):
    """16:16Z on the demo corridor is 09:16 local (PDT) — AM peak, not PM.

    The timezone comes from the corridor's own coordinates; no configuration
    exists. plan_selections must therefore be queried with the LOCAL 09:16.
    """
    from terasim_city.flow.descriptors import TMCData
    from terasim_city.orchestration import corridor_optimization as co
    from terasim_city.plans.assemble import Selection

    seen = {}

    def _capture(corridor_id, intersections, store, when):
        seen["when"] = when
        # One no_plan row: _load_stored_nema_seed then exits via the static
        # ladder, keeping this test about the time conversion only.
        return [(Selection(None, "J1", "no_plan", None, None, None), None, None)], []

    monkeypatch.setattr(co, "plan_selections", _capture)
    cfg = _seed_cfg(tmp_path, "2026-07-13T16:16:00Z")  # a Monday, UTC
    tmc = TMCData.from_json(cfg.tmc_json)

    co._load_stored_nema_seed(cfg, _EmptyCorridor(), DEMO_CORRIDOR_ID, tmc)

    assert (seen["when"].hour, seen["when"].minute) == (9, 16)
    assert seen["when"].tzinfo is None


def test_aware_time_fails_loud_when_derivation_unavailable(tmp_path, monkeypatch):
    from terasim_city.flow.descriptors import TMCData
    from terasim_city import geo_timezone
    from terasim_city.orchestration.corridor_optimization import (
        _load_stored_nema_seed,
    )

    monkeypatch.setattr(geo_timezone, "timezone_for", lambda lat, lon: None)
    cfg = _seed_cfg(tmp_path, "2026-07-13T16:16:00Z")
    tmc = TMCData.from_json(cfg.tmc_json)

    with pytest.raises(ValueError, match="could not be derived"):
        _load_stored_nema_seed(cfg, _EmptyCorridor(), DEMO_CORRIDOR_ID, tmc)

