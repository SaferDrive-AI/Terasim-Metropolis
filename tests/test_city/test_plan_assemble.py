"""Corridor assembly: time-of-day pattern selection + per-intersection lookup.

Net-free (select_pattern / plan_selections); materialization against a net is
covered by test_nema_materialize. Names avoid ``sumo``/``simulation``.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path


from terasim_city.plans.assemble import plan_selections, select_pattern
from terasim_city.plans.store import PlanStore
from tests.fixtures.nema_plan_files import DEMO_ARTERIAL_PLAN, plan_intersection

REPO_ROOT = Path(__file__).resolve().parents[2]

# 2026-07-13 is a Monday; 2026-07-18 is a Saturday.
MON = datetime(2026, 7, 13)
SAT = datetime(2026, 7, 18)


def _i1_plan() -> dict:
    """Demo corridor intersection i1: weekday AM/midday/PM + Free, two weekend patterns."""
    return plan_intersection(DEMO_ARTERIAL_PLAN, "i1")


# ---------------------------------------------------------------------------
# select_pattern (time-of-day)
# ---------------------------------------------------------------------------

def test_weekday_windows_select_the_right_pattern():
    p = _i1_plan()
    assert select_pattern(p, MON.replace(hour=8))["pattern_id"] == "am_peak"    # 06:30–09:30
    assert select_pattern(p, MON.replace(hour=10))["pattern_id"] == "midday"    # 09:30–15:30
    assert select_pattern(p, MON.replace(hour=16))["pattern_id"] == "pm_peak"   # 15:30–19:00


def test_free_window_returns_none():
    p = _i1_plan()
    assert select_pattern(p, MON.replace(hour=20)) is None   # 19:00 -> free


def test_weekend_uses_weekend_schedule():
    p = _i1_plan()
    assert select_pattern(p, SAT.replace(hour=10))["pattern_id"] == "weekend_day"  # 08:00–16:00
    assert select_pattern(p, SAT.replace(hour=17))["pattern_id"] == "weekend_evening"  # 16–21
    assert select_pattern(p, SAT.replace(hour=22)) is None  # 21:00 -> free


def test_no_schedule_uses_first_pattern():
    plan = {"patterns": [{"pattern_id": "only", "cycle_s": 90}], "schedule": []}
    assert select_pattern(plan, MON.replace(hour=12))["pattern_id"] == "only"


def test_early_morning_uses_previous_days_overnight_rule():
    # Weekday and weekend day-types differ; Saturday 03:00 belongs to FRIDAY's
    # overnight rule, not Saturday's last rule.
    plan = {
        "patterns": [{"pattern_id": "wd_night", "cycle_s": 90},
                     {"pattern_id": "we_night", "cycle_s": 90},
                     {"pattern_id": "wd_day", "cycle_s": 100},
                     {"pattern_id": "we_day", "cycle_s": 100}],
        "schedule": [
            {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "06:00", "pattern_id": "wd_day"},
            {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "22:00", "pattern_id": "wd_night"},
            {"days": ["Sat", "Sun"], "start": "08:00", "pattern_id": "we_day"},
            {"days": ["Sat", "Sun"], "start": "23:00", "pattern_id": "we_night"},
        ],
    }
    assert select_pattern(plan, SAT.replace(hour=3))["pattern_id"] == "wd_night"


def test_cycles_compatible_uses_absolute_tolerance():
    from terasim_city.plans.assemble import _cycles_compatible
    assert _cycles_compatible([100, 100]) is True
    assert _cycles_compatible([100, 200]) is True   # clean double
    assert _cycles_compatible([70, 140]) is True     # clean double
    assert _cycles_compatible([138, 140]) is False   # ~2 s off, not a multiple
    assert _cycles_compatible([300, 305]) is False   # ~5 s off, not a multiple


# ---------------------------------------------------------------------------
# plan_selections (corridor-level lookup + cycle compatibility)
# ---------------------------------------------------------------------------

_COORD_SEQ = iter(range(10_000))


def _auto_coord():
    n = next(_COORD_SEQ)
    return 37.4000 + n * 0.002, -121.8000 + n * 0.002


def _inter(iid, cycle, *, lat=None, lon=None):
    if lat is None or lon is None:
        lat, lon = _auto_coord()
    return {
        "intersection_id": iid, "name": iid, "lat": lat, "lon": lon,
        "phases": [{"phase": 2, "movements": ["EB-through"]}, {"phase": 6, "movements": ["WB-through"]}],
        "rings": {"ring1": [2], "ring2": [6], "coordinated_phases": [2, 6]},
        "patterns": [{"pattern_id": "am", "cycle_s": cycle, "splits": {"2": 50, "6": 50}}],
        "schedule": [{"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "07:00", "pattern_id": "am"}],
    }


def _store_with(tmp_path, corridor, inters):
    store = PlanStore(tmp_path / "store")
    store.ingest({"schema": "terasim.nema_plan.v1", "corridor_id": corridor,
                  "intersections": inters, "provenance": {"source_files": [], "gaps": []}})
    return store


def test_selections_pick_nema_and_flag_missing(tmp_path):
    store = _store_with(tmp_path, "c", [_inter("i1", 100), _inter("i2", 100)])
    resolved = [
        {"intersection_id": "i1", "tls_id": "J1", "lat": None, "lon": None},
        {"intersection_id": "i2", "tls_id": "J2", "lat": None, "lon": None},
        {"intersection_id": "i3", "tls_id": "J3", "lat": None, "lon": None},  # no plan
    ]
    rows, warnings = plan_selections("c", resolved, store, MON.replace(hour=8))
    sources = {sel.intersection_id: sel.source for sel, _, _ in rows}
    assert sources == {"i1": "nema", "i2": "nema", "i3": "no_plan"}
    assert {sel.pattern_id for sel, _, _ in rows if sel.source == "nema"} == {"am"}
    assert any("i3" in w and "no stored plan" in w for w in warnings)
    # cycles 100/100 are compatible -> no cycle warning
    assert not any("incompatible cycles" in w for w in warnings)


def test_selection_handles_intersection_without_id(tmp_path):
    # A resolved junction with no TMC counterpart (intersection_id=None) must
    # resolve to no_plan, not crash.
    store = _store_with(tmp_path, "c", [_inter("i1", 100)])
    resolved = [{"intersection_id": None, "tls_id": "Jx", "lat": None, "lon": None}]
    rows, _ = plan_selections("c", resolved, store, MON.replace(hour=8))
    assert rows[0][0].source == "no_plan"


def test_incompatible_cycles_are_flagged(tmp_path):
    store = _store_with(tmp_path, "c", [_inter("i1", 100), _inter("i2", 130)])
    resolved = [
        {"intersection_id": "i1", "tls_id": "J1", "lat": None, "lon": None},
        {"intersection_id": "i2", "tls_id": "J2", "lat": None, "lon": None},
    ]
    _, warnings = plan_selections("c", resolved, store, MON.replace(hour=8))
    assert any("incompatible cycles" in w for w in warnings)


# ---------------------------------------------------------------------------
# End-to-end: store -> assemble -> materialize -> SUMO accepts the NEMA program
# ---------------------------------------------------------------------------

DEMO_NET = REPO_ROOT / "examples" / "corridor_demo" / "demo.net.xml"


def _sumo_binary() -> str | None:
    if importlib.util.find_spec("sumolib") is None:
        return None
    try:
        import sumolib
        return sumolib.checkBinary("sumo")
    except Exception:
        return None


_SUMO = _sumo_binary()
_e2e_ready = _SUMO is not None and DEMO_NET.exists()


def _j1_full_plan() -> dict:
    """Standard 8-phase NEMA for demo J1; rights ride with the through phases."""
    return {"schema": "terasim.nema_plan.v1", "corridor_id": "demo", "intersections": [{
        "intersection_id": "j1_demo", "name": "J1 DEMO", "lat": 37.0, "lon": -121.0,
        "phases": [
            {"phase": 1, "movements": ["WB-left"]},
            {"phase": 2, "movements": ["EB-through", "EB-right"]},
            {"phase": 3, "movements": ["SB-left"]},
            {"phase": 4, "movements": ["NB-through", "NB-right"]},
            {"phase": 5, "movements": ["EB-left"]},
            {"phase": 6, "movements": ["WB-through", "WB-right"]},
            {"phase": 7, "movements": ["NB-left"]},
            {"phase": 8, "movements": ["SB-through", "SB-right"]}],
        "rings": {"ring1": [1, 2, 3, 4], "ring2": [5, 6, 7, 8],
                  "barrier_phases": [4, 8], "barrier2_phases": [2, 6], "coordinated_phases": [2, 6]},
        "patterns": [{"pattern_id": "am_peak", "cycle_s": 100, "offset_s": 0,
                      "splits": {"1": 15, "2": 35, "3": 15, "4": 35, "5": 15, "6": 35, "7": 15, "8": 35}}],
        "schedule": [{"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "07:00", "pattern_id": "am_peak"}],
    }], "provenance": {"source_files": ["demo.pdf"], "handoff_ready": False, "gaps": []}}


