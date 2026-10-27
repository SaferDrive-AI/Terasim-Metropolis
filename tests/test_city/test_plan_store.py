"""PlanStore: identity-keyed, last-wins storage of NEMA plans.

Test names deliberately avoid the substrings ``sumo`` and ``simulation`` so the
repo-root conftest does not auto-mark them ``requires_sumo`` — the store is pure
filesystem, no SUMO.
"""

from __future__ import annotations

import json

import pytest

from terasim_city.plans.store import (
    SAME_INTERSECTION_RADIUS_M,
    PlanFormatError,
    PlanStore,
)
from tests.fixtures.nema_plan_files import DEMO_ARTERIAL_PLAN, MAIN_1ST_PLAN, load_plan


def _plan(corridor: str, *intersections: dict) -> dict:
    return {
        "schema": "terasim.nema_plan.v1",
        "corridor_id": corridor,
        "intersections": list(intersections),
        "provenance": {"source_files": ["x.pdf"], "handoff_ready": False, "gaps": []},
    }


_COORD_SEQ = iter(range(10_000))


def _auto_coord():
    """Distinct default coordinates per call (~220 m apart) so records never
    fall inside the store's 30 m same-intersection radius by accident."""
    n = next(_COORD_SEQ)
    return 37.3000 + n * 0.002, -121.9000 + n * 0.002


def _inter(iid: str, *, lat=None, lon=None, cycle: float = 100.0) -> dict:
    return {
        "intersection_id": iid,
        "name": iid.replace("_", " ").upper(),
        **dict(zip(("lat", "lon"), (lat, lon) if lat is not None and lon is not None else _auto_coord())),
        "phases": [{"phase": 2, "movements": ["EB-through"]},
                   {"phase": 6, "movements": ["WB-through"]}],
        "rings": {"ring1": [2], "ring2": [6], "coordinated_phases": [2, 6]},
        "patterns": [{"pattern_id": "am", "cycle_s": cycle, "splits": {"2": 50, "6": 50}}],
        "schedule": [],
    }


# ---------------------------------------------------------------------------
# Primary key
# ---------------------------------------------------------------------------

def test_ingest_and_find_by_key(tmp_path):
    store = PlanStore(tmp_path / "store")
    store.ingest(_plan("main_st", _inter("first_main")))
    sp = store.find_for("main_st", "first_main")
    assert sp is not None
    assert (sp.corridor_id, sp.intersection_id) == ("main_st", "first_main")
    assert sp.matched_by == "key"
    assert sp.plan["intersections"][0]["patterns"][0]["cycle_s"] == 100.0


def test_multi_intersection_split(tmp_path):
    store = PlanStore(tmp_path / "store")
    result = store.ingest(_plan("main_st", _inter("a_main"), _inter("b_main")))
    assert set(result.stored) == {("main_st", "a_main"), ("main_st", "b_main")}
    assert store.list_ids() == [("main_st", "a_main"), ("main_st", "b_main")]
    assert store.find_for("main_st", "a_main") is not None
    assert store.find_for("main_st", "b_main") is not None


# ---------------------------------------------------------------------------
# Last-wins overwrite + archive
# ---------------------------------------------------------------------------

def test_reupload_same_key_overwrites_and_archives(tmp_path):
    store = PlanStore(tmp_path / "store")
    store.ingest(_plan("main_st", _inter("x_main", cycle=100.0)))
    result = store.ingest(_plan("main_st", _inter("x_main", cycle=140.0)))

    sp = store.find_for("main_st", "x_main")
    assert sp.plan["intersections"][0]["patterns"][0]["cycle_s"] == 140.0  # last wins
    assert any("replaced" in n for n in result.notes)
    # The prior version was archived.
    archive = tmp_path / "store" / "plans" / "main_st" / "x_main" / ".archive"
    assert archive.exists()
    versions = list(archive.iterdir())
    assert len(versions) == 1
    old = json.loads((versions[0] / "plan.json").read_text())
    assert old["intersections"][0]["patterns"][0]["cycle_s"] == 100.0


# ---------------------------------------------------------------------------
# 30 m geolocation fallback
# ---------------------------------------------------------------------------

def test_find_by_geolocation_within_radius(tmp_path):
    store = PlanStore(tmp_path / "store")
    store.ingest(_plan("main_st", _inter("cam_1", lat=37.3300, lon=-121.9000)))

    # Query a DIFFERENT id but ~15 m away (0.0001 deg lat ≈ 11 m) → same junction.
    sp = store.find_for("other_corr", "different_id", lat=37.3301, lon=-121.9000)
    assert sp is not None and sp.intersection_id == "cam_1"
    assert sp.matched_by == "geolocation"

    # ~150 m away → not the same junction.
    assert store.find_for("other_corr", "different_id", lat=37.3313, lon=-121.9000) is None


def test_reupload_within_radius_replaces_old_key(tmp_path):
    store = PlanStore(tmp_path / "store")
    store.ingest(_plan("main_st", _inter("old_name", lat=37.3300, lon=-121.9000, cycle=100.0)))
    # Same physical spot (~11 m), different corridor/id + newer timing.
    result = store.ingest(
        _plan("main_st", _inter("new_name", lat=37.3301, lon=-121.9000, cycle=140.0))
    )
    assert any("same junction" in n for n in result.notes)
    ids = store.list_ids()
    assert ("main_st", "new_name") in ids
    assert ("main_st", "old_name") not in ids  # the old-keyed record was removed
    assert store.find_for("main_st", "new_name").plan["intersections"][0]["patterns"][0]["cycle_s"] == 140.0


def test_intra_document_close_intersections_are_both_kept(tmp_path):
    # Two DISTINCT intersections in one document ~11 m apart must NOT collapse:
    # geolocation dedup is a cross-upload rule, not an intra-document one.
    store = PlanStore(tmp_path / "store")
    result = store.ingest(_plan(
        "c",
        _inter("a_close", lat=37.3300, lon=-121.9000),
        _inter("b_close", lat=37.3301, lon=-121.9000),
    ))
    assert set(result.stored) == {("c", "a_close"), ("c", "b_close")}
    assert store.list_ids() == [("c", "a_close"), ("c", "b_close")]
    assert store.find_for("c", "a_close") is not None  # not deleted


def test_rekey_replacement_preserves_archive(tmp_path):
    store = PlanStore(tmp_path / "store")
    store.ingest(_plan("c", _inter("old_id", lat=37.3300, lon=-121.9000, cycle=100.0)))
    store.ingest(_plan("c", _inter("new_id", lat=37.3301, lon=-121.9000, cycle=140.0)))
    # The replaced version must survive somewhere (moved under the new key's archive).
    archived = list((tmp_path / "store" / "plans").rglob("plan.json"))
    cycles = {
        json.loads(p.read_text())["intersections"][0]["patterns"][0]["cycle_s"]
        for p in archived
    }
    assert 100.0 in cycles and 140.0 in cycles  # both current and archived survive


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_find_for_without_id_falls_through_to_coords(tmp_path):
    # A resolved corridor junction with no TMC counterpart has
    # intersection_id=None; find_for must skip the key lookup and use
    # coordinates, never crash.
    store = PlanStore(tmp_path / "store")
    store.ingest(_plan("c", _inter("cam", lat=37.3300, lon=-121.9000)))
    assert store.find_for("c", None, None, None) is None          # no id, no coords
    sp = store.find_for("c", None, 37.3300, -121.9000)            # no id, coords match
    assert sp is not None and sp.intersection_id == "cam"
    assert sp.matched_by == "geolocation"


def test_ingest_rejects_intersection_missing_rings(tmp_path):
    store = PlanStore(tmp_path / "store")
    inter = _inter("a")
    del inter["rings"]
    with pytest.raises(PlanFormatError, match="rings"):
        store.ingest(_plan("c", inter))


def test_ingest_rejects_schedule_referencing_unknown_pattern(tmp_path):
    store = PlanStore(tmp_path / "store")
    inter = _inter("a")
    inter["schedule"] = [{"days": ["Mon"], "start": "07:00", "pattern_id": "ghost"}]
    with pytest.raises(PlanFormatError, match="ghost"):
        store.ingest(_plan("c", inter))


def test_bad_schema_raises(tmp_path):
    store = PlanStore(tmp_path / "store")
    with pytest.raises(PlanFormatError, match="schema"):
        store.ingest({"schema": "wrong", "corridor_id": "c", "intersections": []})


def test_corridor_id_is_an_optional_label(tmp_path):
    """corridor_id: document value > upload value > 'unassigned'; conflict rejected."""
    store = PlanStore(tmp_path / "store")

    # Neither document nor upload supplies one: filed under the default label.
    result = store.ingest(
        {"schema": "terasim.nema_plan.v1", "intersections": [_inter("a")]}
    )
    assert result.stored == (("unassigned", "a"),)

    # Document omits it, the upload supplies it: the upload value is used.
    result = store.ingest(
        {"schema": "terasim.nema_plan.v1", "intersections": [_inter("b")]},
        default_corridor_id="main_st",
    )
    assert result.stored == (("main_st", "b"),)

    # Document and upload disagree: rejected (misfiling guard).
    with pytest.raises(PlanFormatError, match="conflict"):
        store.ingest(
            {
                "schema": "terasim.nema_plan.v1",
                "corridor_id": "main_st",
                "intersections": [_inter("c")],
            },
            default_corridor_id="other_st",
        )


def test_intersection_without_coordinates_rejected(tmp_path):
    """A plan the optimizer can never find by location is refused at the door."""
    store = PlanStore(tmp_path / "store")
    inter = _inter("no_coords")
    inter["lat"] = None
    inter["lon"] = None
    with pytest.raises(PlanFormatError, match="lat.*lon|coordinates"):
        store.ingest(
            {
                "schema": "terasim.nema_plan.v1",
                "corridor_id": "main_st",
                "intersections": [inter],
            }
        )


# ---------------------------------------------------------------------------
# Complete nema_plan.v1 documents (the synthetic plan fixtures)
# ---------------------------------------------------------------------------

def test_ingest_full_corridor_document_round_trips(tmp_path):
    """A three-intersection corridor file splits into three self-contained records."""
    store = PlanStore(tmp_path / "store")
    result = store.ingest(DEMO_ARTERIAL_PLAN)
    expected_ids = [("demo_arterial", "i1"), ("demo_arterial", "i2"), ("demo_arterial", "i3")]
    assert list(result.stored) == expected_ids
    assert store.list_ids() == expected_ids

    for source in load_plan(DEMO_ARTERIAL_PLAN)["intersections"]:
        sp = store.find_for("demo_arterial", source["intersection_id"])
        assert sp is not None and sp.matched_by == "key"
        assert sp.plan["schema"] == "terasim.nema_plan.v1"
        assert len(sp.plan["intersections"]) == 1
        # Phases, rings, every pattern and the time-of-day schedule survive unchanged.
        assert sp.plan["intersections"][0] == source
        assert (sp.lat, sp.lon) == (source["lat"], source["lon"])

    i1 = store.get("demo_arterial", "i1").plan["intersections"][0]
    assert [p["pattern_id"] for p in i1["patterns"]] == [
        "am_peak", "midday", "pm_peak", "weekend_day", "weekend_evening",
    ]


def test_ingest_full_intersection_document_keeps_per_phase_timing(tmp_path):
    store = PlanStore(tmp_path / "store")
    store.ingest(MAIN_1ST_PLAN)
    source = load_plan(MAIN_1ST_PLAN)["intersections"][0]

    sp = store.find_for("main_st", "main_1st")
    assert sp is not None
    stored = sp.plan["intersections"][0]
    def timing(inter):
        return {
            p["phase"]: (p["min_green_s"], p["yellow_s"], p["all_red_s"])
            for p in inter["phases"]
        }

    assert timing(stored) == timing(source)
    assert len(timing(stored)) == 8
    assert {p["pattern_id"]: p["offset_s"] for p in stored["patterns"]} == {
        "am_peak": 12, "midday_peak": 8, "pm_peak": 64,
    }

    # The same record is found from its coordinates alone (different id/corridor).
    geo = store.find_for("other_corridor", "other_id", lat=source["lat"], lon=source["lon"])
    assert geo is not None
    assert (geo.corridor_id, geo.intersection_id) == ("main_st", "main_1st")
    assert geo.matched_by == "geolocation"
    assert geo.match_distance_m == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Ingest-time timing validation (same rules as the materializer)
# ---------------------------------------------------------------------------


def test_ingest_rejects_nonpositive_cycle(tmp_path):
    store = PlanStore(tmp_path / "store")
    inter = _inter("a")
    inter["patterns"][0]["cycle_s"] = 0
    with pytest.raises(PlanFormatError, match="cycle_s must be > 0"):
        store.ingest(_plan("c", inter))


def test_ingest_rejects_split_below_floor(tmp_path):
    store = PlanStore(tmp_path / "store")
    inter = _inter("a")
    inter["patterns"][0]["splits"]["2"] = 4        # < default 5+3+2 floor
    with pytest.raises(PlanFormatError, match="below its minimum green"):
        store.ingest(_plan("c", inter))
