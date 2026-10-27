"""Tests for generate_flow: one flow file in, map auto-found, rou.xml out.

Uses the corridor demo assets (examples/corridor_demo) ingested into a
temporary MapStore, so no external binary is needed.

Test names deliberately avoid the substrings ``sumo`` and ``simulation`` so
the repo-root conftest does not auto-mark them ``requires_sumo``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from terasim_city.flow.generate import generate_flow
from terasim_city.maps import MANIFEST_NAME, MapPackage, MapStore, read_orig_boundary

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_SUMOCFG = DEMO_DIR / "demo.sumocfg"
DEMO_TMC = DEMO_DIR / "demo_tmc.json"

requires_demo_assets = pytest.mark.skipif(
    not DEMO_SUMOCFG.exists() or not DEMO_TMC.exists(),
    reason=f"corridor demo assets not present under {DEMO_DIR}",
)


@pytest.fixture()
def demo_store(tmp_path) -> tuple[MapStore, str]:
    """A store holding the ingested demo scenario; returns (store, map_id)."""
    store = MapStore(tmp_path / "store")
    result = store.ingest(DEMO_SUMOCFG)
    return store, result.map_id


def _far_away_flow(tmp_path: Path) -> Path:
    """A syntactically valid flow file whose intersections sit at (0, 0)."""
    payload = {
        "corridor_id": "nowhere",
        "observation_interval": "1h",
        "timestamp_start": "2026-07-01T08:00:00",
        "intersections": [
            {
                "intersection_id": "x1",
                "lat": 0.0,
                "lon": 0.0,
                "counts": {"East": {"Left": 5, "Straight": 50, "Right": 5}},
            }
        ],
    }
    out = tmp_path / "nowhere_flow.json"
    out.write_text(json.dumps(payload), encoding="utf-8")
    return out


def _mixed_coverage_flow(tmp_path: Path) -> Path:
    """One intersection inside the demo bbox, one far outside."""
    inside = json.loads(DEMO_TMC.read_text())["intersections"][0]
    payload = {
        "corridor_id": "half_in",
        "observation_interval": "1h",
        "timestamp_start": "2026-07-01T08:00:00",
        "intersections": [
            inside,
            {
                "intersection_id": "far",
                "lat": 0.0,
                "lon": 0.0,
                "counts": {"East": {"Left": 5, "Straight": 50, "Right": 5}},
            },
        ],
    }
    out = tmp_path / "half_in_flow.json"
    out.write_text(json.dumps(payload), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Auto map lookup
# ---------------------------------------------------------------------------

@requires_demo_assets
def test_generate_flow_auto_finds_covering_map(tmp_path, demo_store):
    store, map_id = demo_store
    result = generate_flow(
        DEMO_TMC, tmp_path / "out", store=store, sim_end=60.0
    )
    assert result.map_id == map_id
    assert result.package.base_sumocfg == DEMO_SUMOCFG.resolve()
    routes = Path(result.scenario.routes_file)
    assert routes.exists()
    assert result.scenario.n_flows > 0


@requires_demo_assets
def test_generate_flow_prefers_richer_source(tmp_path, demo_store):
    store, prebuilt_id = demo_store
    # Hand-write an "osm" package over the same demo files/coverage; the richer
    # prebuilt source must win the tie (SOURCE_PREFERENCE = prebuilt > osm).
    osm_id = "aaaosmmap0001"
    boundary = read_orig_boundary(DEMO_DIR / "demo.net.xml")
    MapPackage(
        root=DEMO_DIR,
        net_file=DEMO_DIR / "demo.net.xml",
        base_sumocfg=DEMO_SUMOCFG,
        tls_files=(),
        geo_referenced=True,
        source="osm",
        metadata={"orig_boundary": list(boundary)},
    ).save(store.maps_root / osm_id / MANIFEST_NAME)

    result = generate_flow(DEMO_TMC, tmp_path / "out", store=store, sim_end=60.0)
    assert result.map_id == prebuilt_id
    assert result.package.source == "prebuilt"
    assert osm_id in dict(store.list_packages())  # both still stored


@requires_demo_assets
def test_generate_flow_no_coverage_fails_with_diagnostics(tmp_path, demo_store):
    store, map_id = demo_store
    flow = _far_away_flow(tmp_path)
    with pytest.raises(ValueError, match="not covered by any stored map"):
        generate_flow(flow, tmp_path / "out", store=store)
    # The diagnostic names the stored maps.
    with pytest.raises(ValueError, match=map_id):
        generate_flow(flow, tmp_path / "out", store=store)


@requires_demo_assets
def test_generate_flow_partial_coverage_is_rejected(tmp_path, demo_store):
    store, _ = demo_store
    flow = _mixed_coverage_flow(tmp_path)
    with pytest.raises(ValueError, match="no stored map covers"):
        generate_flow(flow, tmp_path / "out", store=store)


# ---------------------------------------------------------------------------
# Explicit map_id
# ---------------------------------------------------------------------------


@requires_demo_assets
def test_generate_flow_unknown_map_id_raises(tmp_path, demo_store):
    store, _ = demo_store
    with pytest.raises(KeyError, match="deadbeef0000"):
        generate_flow(DEMO_TMC, tmp_path / "out", store=store, map_id="deadbeef0000")


# ---------------------------------------------------------------------------
# Tuning passthrough
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Map selection must also cover the optimization target (extra_points)
# ---------------------------------------------------------------------------

class _FakePackage:
    def __init__(self, boundary):
        self.source = "osm"
        self.metadata = {"orig_boundary": boundary}


class _FakeStore:
    """Minimal MapStore stand-in: one map with a fixed WGS84 bbox."""

    def __init__(self, map_id: str, boundary):
        self._map_id = map_id
        self._pkg = _FakePackage(boundary)

    def find_covering(self, lat: float, lon: float):
        lon_min, lat_min, lon_max, lat_max = self._pkg.metadata["orig_boundary"]
        if lon_min <= lon <= lon_max and lat_min <= lat <= lat_max:
            return [(self._map_id, self._pkg)]
        return []

    def list_packages(self):
        return [(self._map_id, self._pkg)]


def test_map_selection_requires_target_coverage(tmp_path):
    """A target outside the TMC footprint must reject a non-covering map."""
    from terasim_city.flow.descriptors import TMCData
    from terasim_city.flow.generate import select_map_for

    from tests.fixtures.tmc_templates import demo_tmc, write_tmc

    tmc = TMCData.from_json(write_tmc(tmp_path, demo_tmc()))
    # Bbox covers the demo TMC (around 37.335, -121.90) but not every target.
    store = _FakeStore("m1", (-121.92, 37.32, -121.88, 37.35))

    map_id, _pkg = select_map_for(store, tmc)
    assert map_id == "m1"

    with pytest.raises(ValueError, match="not covered by any stored map"):
        select_map_for(store, tmc, extra_points=[(37.40, -121.90)])

    map_id, _pkg = select_map_for(store, tmc, extra_points=[(37.34, -121.90)])
    assert map_id == "m1"
