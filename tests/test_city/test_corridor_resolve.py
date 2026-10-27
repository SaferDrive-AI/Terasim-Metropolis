"""Tests for corridor resolution on the synthetic demo corridor.

Corridor eligibility comes from the NET: any junction whose node maps to a
TLS in the net resolves, whether or not the additional files carry candidate
programs for it. The probe is the demo net with a TLS additional file that
defines a program for ``J1`` only (``J2``/``J3`` are signalized in the net but
have no additional-file program), plus a two-intersection TMC payload whose
``i1``/``i2`` points sit a known distance off ``J1``/``J2``. This file covers
the dual-intersection from/to walk onto a program-less TLS, the explicit
``tls_ids`` path (validated against the net), the single-TLS fallback ladder
(additional files first, then the net), pure-net resolution on a sumocfg
without any TLS additional files, plus the error paths of the from/to
resolution (unknown ref, out-of-range point, half-specified endpoints).
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from tests.fixtures.demo_corridor import DEMO_COORDS, write_partial_program_sumocfg
from tests.fixtures.tmc_templates import two_signal_tmc, write_tmc
from tests.test_city.conftest import (
    DEMO_NET,
    DEMO_ROU,
    DEMO_TMC_JSON,
    requires_sumolib,
)

pytestmark = [requires_sumolib]


from terasim_city.corridor import (  # noqa: E402
    ResolvedCorridor,
    ResolvedIntersection,
    resolve_corridor,
)

#: The demo signal that carries a candidate program in the additional file.
PROGRAM_TLS = "J1"
#: A demo signal with its net tlLogic only (no additional-file program).
PROGRAM_LESS_TLS = "J2"

#: How far the TMC points sit from their junctions: i1 north of J1, i2 east of J2.
I1_NORTH_M = 1.0
I2_EAST_M = 4.0
_M_PER_DEG_LAT = 111_320.0
_M_PER_DEG_LON = _M_PER_DEG_LAT * math.cos(math.radians(DEMO_COORDS["i1"][0]))
I1_LAT = round(DEMO_COORDS["i1"][0] + I1_NORTH_M / _M_PER_DEG_LAT, 7)
I1_LON = DEMO_COORDS["i1"][1]
I2_LAT = DEMO_COORDS["i2"][0]
I2_LON = round(DEMO_COORDS["i2"][1] + I2_EAST_M / _M_PER_DEG_LON, 7)


@pytest.fixture(scope="module")
def partial_sumocfg(tmp_path_factory) -> Path:
    """The demo net with an additional-file program for J1 only."""
    return write_partial_program_sumocfg(
        tmp_path_factory.mktemp("partial_programs"), (PROGRAM_TLS,)
    )


@pytest.fixture(scope="module")
def offset_tmc_json(tmp_path_factory) -> Path:
    """Two-intersection TMC: i1 1 m north of J1, i2 4 m east of J2."""
    payload = two_signal_tmc()
    i1, i2 = payload["intersections"]
    i1["lat"], i1["lon"] = I1_LAT, I1_LON
    i2["lat"], i2["lon"] = I2_LAT, I2_LON
    return write_tmc(tmp_path_factory.mktemp("offset_tmc"), payload)


def _pure_net_sumocfg(tmp_path: Path) -> Path:
    """A sumocfg for the demo net WITHOUT any TLS additional files.

    Models a pure-OSM map: every controllable signal exists only as the net's
    own tlLogic.
    """
    sumocfg = tmp_path / "pure_net.sumocfg"
    sumocfg.write_text(
        "<configuration><input>"
        f'<net-file value="{DEMO_NET}"/>'
        f'<route-files value="{DEMO_ROU}"/>'
        "</input></configuration>",
        encoding="utf-8",
    )
    return sumocfg


# ---------------------------------------------------------------------------
# Single-TLS fallback (no refs, no tls_ids)
# ---------------------------------------------------------------------------

def test_fallback_resolves_the_single_tls(partial_sumocfg, offset_tmc_json):
    # Three net TLS, but the additional files program only J1: it wins.
    corridor = resolve_corridor(partial_sumocfg, offset_tmc_json)
    assert len(corridor.intersections) == 1
    item = corridor.intersections[0]
    assert item.tls_id == PROGRAM_TLS
    assert item.node_id == PROGRAM_TLS  # the junction hosts its own TLS
    assert item.ref == PROGRAM_TLS


def test_fallback_matches_tmc_metadata_of_nearest_intersection(
    partial_sumocfg, offset_tmc_json
):
    corridor = resolve_corridor(partial_sumocfg, offset_tmc_json)
    item = corridor.intersections[0]
    # The J1 junction sits 1 m from the i1 TMC point.
    assert item.intersection_id == "i1"
    assert item.lat == pytest.approx(I1_LAT)
    assert item.lon == pytest.approx(I1_LON)
    assert item.match_distance_m == pytest.approx(I1_NORTH_M, abs=0.05)
    # The anchor is the junction's own position, not the TMC point.
    assert (item.anchor_lat, item.anchor_lon) == pytest.approx(DEMO_COORDS["i1"], abs=1e-6)


# ---------------------------------------------------------------------------
# Dual-intersection from/to walk (i1 -> i2 along the arterial)
# ---------------------------------------------------------------------------

def test_from_to_resolves_both_signals(partial_sumocfg, offset_tmc_json):
    corridor = resolve_corridor(
        partial_sumocfg,
        offset_tmc_json,
        from_ref="i1",
        to_ref="i2",
    )
    # Order preserved (from -> to); J2 has NO additional-file programs —
    # resolution must not require any (controllability from net).
    assert corridor.tls_ids == (PROGRAM_TLS, PROGRAM_LESS_TLS)
    first, second = corridor.intersections
    assert first.ref == "i1"
    assert first.intersection_id == "i1"
    assert first.match_distance_m == pytest.approx(I1_NORTH_M, abs=0.05)
    assert second.ref == "i2"
    assert second.intersection_id == "i2"
    assert second.match_distance_m == pytest.approx(I2_EAST_M, abs=0.05)


# ---------------------------------------------------------------------------
# Explicit tls_ids
# ---------------------------------------------------------------------------

def test_explicit_tls_ids_path(partial_sumocfg, offset_tmc_json):
    corridor = resolve_corridor(
        partial_sumocfg, offset_tmc_json, tls_ids=[PROGRAM_TLS]
    )
    assert corridor.tls_ids == (PROGRAM_TLS,)
    item = corridor.intersections[0]
    assert item.ref == PROGRAM_TLS
    assert item.intersection_id == "i1"


def test_explicit_tls_ids_win_over_refs(partial_sumocfg, offset_tmc_json):
    corridor = resolve_corridor(
        partial_sumocfg,
        offset_tmc_json,
        from_ref="not_a_real_ref",
        to_ref="also_not_real",
        tls_ids=[PROGRAM_TLS],
    )
    assert corridor.tls_ids == (PROGRAM_TLS,)


def test_explicit_tls_ids_accept_program_less_net_tls(partial_sumocfg, offset_tmc_json):
    # J2 is a net TLS with zero additional-file programs. The old behavior
    # rejected it ("no candidate programs"); the new principle
    # (controllability from the net) must resolve it.
    corridor = resolve_corridor(
        partial_sumocfg,
        offset_tmc_json,
        tls_ids=[PROGRAM_TLS, PROGRAM_LESS_TLS],
    )
    assert corridor.tls_ids == (PROGRAM_TLS, PROGRAM_LESS_TLS)
    assert corridor.intersections[1].intersection_id == "i2"


def test_unknown_tls_id_lists_net_tls_ids(partial_sumocfg, offset_tmc_json):
    # A junction id that is not a TLS of the net must still fail, and the
    # error must list the NET's TLS ids (not additional-file discovery).
    with pytest.raises(ValueError) as exc:
        resolve_corridor(partial_sumocfg, offset_tmc_json, tls_ids=["bogus_tls"])
    message = str(exc.value)
    assert "bogus_tls" in message
    assert PROGRAM_TLS in message
    assert PROGRAM_LESS_TLS in message  # program-less, but a net TLS
    assert "J3" in message


def test_duplicate_tls_ids_rejected(partial_sumocfg, offset_tmc_json):
    with pytest.raises(ValueError, match="duplicate"):
        resolve_corridor(
            partial_sumocfg,
            offset_tmc_json,
            tls_ids=[PROGRAM_TLS, PROGRAM_TLS],
        )


# ---------------------------------------------------------------------------
# Pure-net resolution (no TLS additional files at all)
# ---------------------------------------------------------------------------

def test_explicit_tls_ids_resolve_without_tls_additional_files(tmp_path):
    # A pure-OSM map: the sumocfg lists NO additional files, so candidate
    # discovery finds nothing — the net's own tlLogic makes J1..J3 eligible.
    corridor = resolve_corridor(
        _pure_net_sumocfg(tmp_path), DEMO_TMC_JSON, tls_ids=["J1", "J2", "J3"]
    )
    assert corridor.tls_ids == ("J1", "J2", "J3")
    assert [item.intersection_id for item in corridor.intersections] == [
        "i1",
        "i2",
        "i3",
    ]


def test_pure_net_fallback_with_several_tls_lists_them(tmp_path):
    # No refs, no tls_ids, no additional-file programs: the net fallback
    # cannot auto-pick among the demo net's 3 TLS and must list them.
    with pytest.raises(ValueError) as exc:
        resolve_corridor(_pure_net_sumocfg(tmp_path), DEMO_TMC_JSON)
    message = str(exc.value)
    for tls_id in ("J1", "J2", "J3"):
        assert tls_id in message


# ---------------------------------------------------------------------------
# from/to error paths
# ---------------------------------------------------------------------------

def test_unknown_ref_lists_tmc_candidates(partial_sumocfg, offset_tmc_json):
    with pytest.raises(ValueError) as exc:
        resolve_corridor(
            partial_sumocfg,
            offset_tmc_json,
            from_ref="no_such_intersection",
            to_ref="i1",
        )
    message = str(exc.value)
    assert "no_such_intersection" in message
    assert "i1" in message
    assert "i2" in message


def test_ref_beyond_match_distance_lists_nearest_nodes(partial_sumocfg, offset_tmc_json):
    # A point tens of kilometres away from the mapped area.
    with pytest.raises(ValueError) as exc:
        resolve_corridor(
            partial_sumocfg,
            offset_tmc_json,
            from_ref="37.0,-122.5",
            to_ref="i1",
        )
    message = str(exc.value)
    assert "60.0m" in message
    assert "nearest candidates" in message


def test_half_specified_endpoints_rejected(partial_sumocfg, offset_tmc_json):
    with pytest.raises(ValueError, match="together"):
        resolve_corridor(partial_sumocfg, offset_tmc_json, from_ref="i1")
    with pytest.raises(ValueError, match="together"):
        resolve_corridor(partial_sumocfg, offset_tmc_json, to_ref="i1")


# ---------------------------------------------------------------------------
# Value-object validation
# ---------------------------------------------------------------------------
