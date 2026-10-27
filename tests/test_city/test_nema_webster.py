"""Tests for plans/nema_webster.py — demand-timed patterns on official skeletons.

Each test guards one failure mode of the free-period seeding: barrier
imbalance, cycle mis-sum, ignored minimums, div-by-zero on empty demand,
unfittable minimums, the seed-ladder integration (free hour -> skeleton
reused instead of the static Webster fallback), and window counts read as
hourly volumes (a 10-minute TMC window must be scaled by 3600/window).
"""

from __future__ import annotations

import pytest

from terasim_city.plans.nema_materialize import check_pattern_timing
from terasim_city.plans.nema_webster import (
    phase_demand_veh_h,
    recommended_cycle_s,
    synthesize_free_pattern,
)


def _skeleton() -> dict:
    """A standard 8-phase dual-ring intersection (protected lefts)."""
    movements = {
        1: ["WB-left"], 2: ["EB-through", "EB-right"],
        3: ["SB-left"], 4: ["NB-through", "NB-right"],
        5: ["EB-left"], 6: ["WB-through", "WB-right"],
        7: ["NB-left"], 8: ["SB-through", "SB-right"],
    }
    return {
        "intersection_id": "x",
        "phases": [
            {"phase": p, "movements": m, "yellow_s": 3.0, "all_red_s": 1.0,
             "min_green_s": 5.0}
            for p, m in movements.items()
        ],
        "rings": {
            "ring1": [1, 2, 3, 4], "ring2": [5, 6, 7, 8],
            "barrier_phases": [4, 8], "barrier2_phases": [2, 6],
            "coordinated_phases": [2, 6],
        },
    }


def _counts(ew_through=800, ns_through=300, left=60) -> dict:
    return {
        "EB": {"left": left, "through": ew_through, "right": 50},
        "WB": {"left": left, "through": ew_through, "right": 50},
        "NB": {"left": left, "through": ns_through, "right": 40},
        "SB": {"left": left, "through": ns_through, "right": 40},
    }


def test_phase_demand_maps_movements_to_counts():
    demand = phase_demand_veh_h(_skeleton(), _counts())
    assert demand[2] == 800 + 50  # EB-through + EB-right
    assert demand[1] == 60        # WB-left
    assert demand[4] == 300 + 40


def test_synthesized_pattern_respects_barriers_and_cycle():
    pattern = synthesize_free_pattern(_skeleton(), _counts(), cycle_s=100.0)
    s = {int(k): v for k, v in pattern["splits"].items()}
    # Barrier rule: both rings spend the SAME time in each concurrency group.
    assert s[1] + s[2] == pytest.approx(s[5] + s[6])
    assert s[3] + s[4] == pytest.approx(s[7] + s[8])
    # The segments sum to the corridor cycle exactly.
    assert s[1] + s[2] + s[3] + s[4] == pytest.approx(100.0)
    # Heavier east-west demand gets the longer concurrency group.
    assert s[1] + s[2] > s[3] + s[4]
    check_pattern_timing(_skeleton(), pattern)  # materializable as-is


def test_zero_demand_still_produces_valid_uniform_pattern():
    zero = {d: {t: 0 for t in ("left", "through", "right")}
            for d in ("EB", "WB", "NB", "SB")}
    pattern = synthesize_free_pattern(_skeleton(), zero, cycle_s=80.0)
    s = {int(k): v for k, v in pattern["splits"].items()}
    assert sum(v for p, v in s.items() if p in (1, 2, 3, 4)) == pytest.approx(80.0)
    assert all(v >= 5.0 + 3.0 + 1.0 for v in s.values())  # floors respected


def test_unfittable_minimums_raise():
    # Each ring carries 4 phases x (5+3+1)s = 36s of floors; 30s cannot fit.
    with pytest.raises(ValueError, match="minimum"):
        synthesize_free_pattern(_skeleton(), _counts(), cycle_s=30.0)


def test_recommended_cycle_scales_with_demand_and_clamps():
    light = recommended_cycle_s(_skeleton(), _counts(ew_through=200, ns_through=100))
    heavy = recommended_cycle_s(_skeleton(), _counts(ew_through=1500, ns_through=900))
    assert light < heavy
    assert heavy <= 180.0
    saturated = recommended_cycle_s(
        _skeleton(), _counts(ew_through=4000, ns_through=4000)
    )
    assert saturated == 180.0


def test_seed_ladder_uses_skeleton_on_free_hour(tmp_path):
    """Integration: a free-hour corridor seeds from stored skeletons, not the
    static Webster fallback (the exact Sunday-morning case)."""
    import json
    from pathlib import Path

    from terasim_city.flow.descriptors import TMCData
    from terasim_city.plans.store import PlanStore
    from terasim_city.orchestration.corridor_optimization import (
        CorridorOptimizationConfig,
        _load_stored_nema_seed,
    )

    doc = {
        "schema": "terasim.nema_plan.v1",
        "corridor_id": "c1",
        "intersections": [dict(_skeleton(),
            intersection_id="i1", name="i1", lat=37.30, lon=-121.90,
            patterns=[{"pattern_id": "am", "cycle_s": 100.0,
                       "splits": {str(p): 25.0 if p in (2, 4, 6, 8) else 25.0
                                  for p in range(1, 9)}}],
            schedule=[{"days": ["Mon"], "start": "07:00", "pattern_id": "am"},
                      {"days": ["Mon"], "start": "09:00", "pattern_id": "free"}],
        )],
    }
    PlanStore(tmp_path).ingest(doc)

    tmc_payload = {
        "corridor_id": "c1", "observation_interval": "1h",
        # Monday 10:00 -> the schedule says free.
        "timestamp_start": "2026-03-16T10:00:00",
        "intersections": [{
            "intersection_id": "i1", "lat": 37.30, "lon": -121.90,
            "counts": {"East": {"Left": 60, "Straight": 800, "Right": 50},
                       "West": {"Left": 60, "Straight": 800, "Right": 50},
                       "North": {"Left": 60, "Straight": 300, "Right": 40},
                       "South": {"Left": 60, "Straight": 300, "Right": 40}},
        }],
    }
    tmc_file = tmp_path / "tmc.json"
    tmc_file.write_text(json.dumps(tmc_payload))
    tmc = TMCData.from_json(tmc_file)

    class _It:
        intersection_id, tls_id, lat, lon = "i1", "J1", 37.30, -121.90

    class _Corridor:
        intersections = (_It(),)

    cfg = CorridorOptimizationConfig(
        tmc_json=tmc_file, base_sumocfg=None, output_dir=tmp_path / "out",
        plan_data_root=tmp_path,
    )
    seed, fallback_reason = _load_stored_nema_seed(cfg, _Corridor(), "c1", tmc)
    assert seed is not None, (
        f"free hour must reuse the skeleton, not fall back ({fallback_reason})"
    )
    assert seed.plan_id == "stored_nema"
    (entry,) = seed.entries
    assert entry.pattern["pattern_id"] == "webster_free"
    check_pattern_timing(entry.intersection, entry.pattern)


def test_grid_rounding_never_dips_below_floor_and_float_noise_is_tolerated():
    """The two halves of the silent-seed-discard bug.

    (a) _allocate's drift correction used to round an item sitting exactly at
        its minimum 0.1s below it; the downstream floor check then discarded
        the whole seed plan.
    (b) check_pattern_timing compared with zero tolerance, so a floor that
        float-sums to 9.600000000000001 rejected a legitimate 9.6s split.
    """
    from terasim_city.plans.nema_webster import _allocate

    # (a) every allocation must respect its floor and still sum exactly.
    floors = [9.6, 9.6, 5.0]
    for amount in (24.3, 24.5, 25.7, 30.1):
        got = _allocate(amount, [0.0, 0.0, 1.0], floors)
        assert sum(got) == pytest.approx(amount)
        for value, floor in zip(got, floors):
            assert value >= floor - 1e-9, f"{value} dipped below floor {floor}"

    # (b) floor = 5.0 + 3.0 + 1.6 = 9.600000000000001 must accept split 9.6.
    inter = {
        "intersection_id": "X",
        "phases": [{"phase": 1, "movements": ["EB-through"],
                    "min_green_s": 5.0, "yellow_s": 3.0, "all_red_s": 1.6}],
        "rings": {"ring1": [1], "ring2": [], "coordinated_phases": [1]},
    }
    check_pattern_timing(
        inter, {"pattern_id": "p", "cycle_s": 40.0, "splits": {"1": 9.6}}
    )
    # A REAL violation (a full grid step short) is still rejected.
    with pytest.raises(ValueError, match="below its minimum"):
        check_pattern_timing(
            inter, {"pattern_id": "p", "cycle_s": 40.0, "splits": {"1": 9.5}}
        )


# ---------------------------------------------------------------------------
# Observation-window scaling: TMC window counts -> hourly volumes
# ---------------------------------------------------------------------------


def _times(counts: dict, factor: float) -> dict:
    return {
        d: {t: v * factor for t, v in turns.items()} for d, turns in counts.items()
    }


def test_phase_demand_scale_converts_window_counts_to_hourly():
    demand = phase_demand_veh_h(_skeleton(), _counts(), scale=6.0)
    assert demand[2] == pytest.approx(6 * (800 + 50))
    assert demand[1] == pytest.approx(6 * 60)
    assert demand[4] == pytest.approx(6 * (300 + 40))
    # The default leaves counts untouched (already hourly).
    assert phase_demand_veh_h(_skeleton(), _counts()) == phase_demand_veh_h(
        _skeleton(), _counts(), scale=1.0
    )


def test_recommended_cycle_scale_equals_prescaled_counts():
    counts = _counts(ew_through=200, ns_through=100)
    unscaled = recommended_cycle_s(_skeleton(), counts)
    scaled = recommended_cycle_s(_skeleton(), counts, scale=2.0)
    assert scaled == pytest.approx(
        recommended_cycle_s(_skeleton(), _times(counts, 2.0))
    )
    assert scaled > unscaled  # not a vacuous comparison of two clamped values


def test_synthesize_free_pattern_forwards_scale_without_moving_splits(monkeypatch):
    # Splits are demand-proportional, so a uniform scale cannot show in the
    # output; the forwarding (demand in the same unit as the cycle) is pinned
    # directly instead.
    from terasim_city.plans import nema_webster

    seen = []
    real = nema_webster.phase_demand_veh_h

    def spy(intersection, counts, *, scale=1.0):
        seen.append(scale)
        return real(intersection, counts, scale=scale)

    monkeypatch.setattr(nema_webster, "phase_demand_veh_h", spy)
    base = synthesize_free_pattern(_skeleton(), _counts(), cycle_s=100.0)
    scaled = synthesize_free_pattern(
        _skeleton(), _counts(), cycle_s=100.0, scale=6.0
    )
    assert seen == [1.0, 6.0]
    assert scaled == base


def _free_hour_seed_cycle(root, interval: str, counts: dict) -> float:
    """Seed a Monday-10:00 (scheduled free) TMC window from a stored skeleton;
    return the cycle the synthesized pattern is timed to."""
    import json

    from terasim_city.flow.descriptors import TMCData
    from terasim_city.plans.store import PlanStore
    from terasim_city.orchestration.corridor_optimization import (
        CorridorOptimizationConfig,
        _load_stored_nema_seed,
    )

    root.mkdir(parents=True)
    PlanStore(root).ingest({
        "schema": "terasim.nema_plan.v1",
        "corridor_id": "c1",
        "intersections": [dict(_skeleton(),
            intersection_id="i1", name="i1", lat=37.30, lon=-121.90,
            patterns=[{"pattern_id": "am", "cycle_s": 100.0,
                       "splits": {str(p): 25.0 for p in range(1, 9)}}],
            schedule=[{"days": ["Mon"], "start": "07:00", "pattern_id": "am"},
                      {"days": ["Mon"], "start": "09:00", "pattern_id": "free"}],
        )],
    })
    tmc_file = root / "tmc.json"
    tmc_file.write_text(json.dumps({
        "corridor_id": "c1", "observation_interval": interval,
        "timestamp_start": "2026-03-16T10:00:00",
        "intersections": [{
            "intersection_id": "i1", "lat": 37.30, "lon": -121.90,
            "counts": counts,
        }],
    }))

    class _It:
        intersection_id, tls_id, lat, lon = "i1", "J1", 37.30, -121.90

    class _Corridor:
        intersections = (_It(),)

    cfg = CorridorOptimizationConfig(
        tmc_json=tmc_file, base_sumocfg=None, output_dir=root / "out",
        plan_data_root=root,
    )
    seed, fallback_reason = _load_stored_nema_seed(
        cfg, _Corridor(), "c1", TMCData.from_json(tmc_file)
    )
    assert seed is not None, fallback_reason
    (entry,) = seed.entries
    assert entry.pattern["pattern_id"] == "webster_free"
    return entry.pattern["cycle_s"]


#: A 10-minute TMC window; x6 is the hourly demand the tests below expect.
_TEN_MINUTE_COUNTS = {
    "East": {"Left": 10, "Straight": 100, "Right": 10},
    "West": {"Left": 10, "Straight": 100, "Right": 10},
    "North": {"Left": 10, "Straight": 50, "Right": 10},
    "South": {"Left": 10, "Straight": 50, "Right": 10},
}


def test_free_hour_seed_reads_window_counts_as_hourly_demand(tmp_path):
    """A 10-minute window must time the free-hour skeleton like the same
    traffic expressed hourly. Fed raw, its counts look 6x lighter than they
    are and the cycle collapses to the 45 s floor."""
    ten_minute = _free_hour_seed_cycle(tmp_path / "ten", "10m", _TEN_MINUTE_COUNTS)
    one_hour = _free_hour_seed_cycle(
        tmp_path / "hour", "1h", _times(_TEN_MINUTE_COUNTS, 6)
    )

    # Hand-computed on the skeleton: critical flow ratios 720/1800 + 420/1800,
    # lost time 16 s -> C = (1.5 * 16 + 5) / (1 - 0.6333) = 79.1 s.
    assert one_hour == pytest.approx(79.09, abs=0.01)
    assert ten_minute == pytest.approx(one_hour)


def test_free_hour_seed_passes_the_window_scale_to_both_webster_calls(
    tmp_path, monkeypatch
):
    """The synthesis call's scale is invisible in its output (proportional
    splits), so the wiring is pinned directly: both calls get 3600 / window."""
    from terasim_city.orchestration import corridor_optimization as co

    scales = {}

    def spy(name):
        real = getattr(co, name)

        def wrapper(*args, **kwargs):
            scales[name] = kwargs.get("scale")
            return real(*args, **kwargs)

        monkeypatch.setattr(co, name, wrapper)

    spy("recommended_cycle_s")
    spy("synthesize_free_pattern")
    _free_hour_seed_cycle(tmp_path / "ten", "10m", _TEN_MINUTE_COUNTS)

    assert scales == {
        "recommended_cycle_s": pytest.approx(6.0),
        "synthesize_free_pattern": pytest.approx(6.0),
    }
