"""Tests for the unified v1 optimization request: corridor_id mode + inline incident.

FastAPI TestClient + in-memory buffer/store + spy launcher (no Redis, no SUMO,
no subprocess). Covers the exactly-one-of tmc_json/corridor_id rule, the inline
incident schema + expedited (emergency headroom) slot acquisition, and
corridor_id mode: snapshot aggregation into a server-generated demand file. The
scheduled-config entry is optional — when present its defaults merge under
explicit request fields; when absent the request must carry base_sumocfg
(corridor_id keys the demand queue, an explicit `corridor` keys the geometry).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from terasim_service.city.ingest_api import InMemorySnapshotBuffer
from terasim_service.city.job_store import InMemoryCityJobStore
from terasim_service.city.optimize_api import create_v1_city_router

from tests.fixtures.tmc_templates import (
    baseline_sheet_uncoordinated,
    demo_snapshots,
    write_tmc,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_DIR = _REPO_ROOT / "examples" / "corridor_demo"

_BASE = "/v1/city/optimizations"
# The ingest contract interprets naive timestamps as UTC, so the fake clock
# must be pinned to UTC as well (a local-time epoch would sit hours away from
# the snapshots' naive stamps and empty the aggregation window).
_NOW = datetime.fromisoformat("2026-07-01T09:00:00+00:00").timestamp()
_STALL = {"latitude": 37.335, "longitude": -121.90045}


def _write_inputs(folder: Path):
    """Minimal tmc.json + sumocfg (+ referenced net) for path validation."""
    folder.mkdir(parents=True, exist_ok=True)
    tmc = folder / "tmc.json"
    tmc.write_text("{}", encoding="utf-8")
    (folder / "osm.net.xml").write_text("<net/>", encoding="utf-8")
    sumocfg = folder / "osm.sumocfg"
    sumocfg.write_text(
        '<configuration><input><net-file value="osm.net.xml"/></input></configuration>',
        encoding="utf-8",
    )
    return str(tmc), str(sumocfg)


def _corridor_cfg(tmp_path: Path) -> dict:
    """Same shape load_scheduled_config produces for the demo corridor."""
    baseline_sheet = write_tmc(
        tmp_path,
        baseline_sheet_uncoordinated(),
        name="baseline_sheet_uncoordinated.json",
    )
    return {
        "corridor_id": "demo_arterial",
        "base_sumocfg": str(_DEMO_DIR / "demo.sumocfg"),
        "corridor": {"tls_ids": ["J1", "J2", "J3"]},
        "baseline_sheet": str(baseline_sheet),
        "sim_end": 600,
        "max_trials": 6,
        "n_workers": 2,
    }


def _demo_snapshots_in_window(n: int = 2) -> list[dict]:
    """First ``n`` demo snapshots, timestamps adjusted into the last hour."""
    snapshots = demo_snapshots()[:n]
    assert len(snapshots) == n, "demo snapshots missing from shared templates"
    for index, payload in enumerate(snapshots):
        payload["timestamp_start"] = f"2026-07-01T08:{30 + 10 * index}:00"
    return snapshots


def _make_client(
    tmp_path: Path,
    *,
    max_concurrency: int = 3,
    emergency_slots: int = 2,
    preload: bool = True,
    corridors: dict | None = None,
    with_buffer: bool = True,
    no_config: bool = False,
):
    store = InMemoryCityJobStore(
        max_concurrency=max_concurrency,
        lease_ttl=100,
        emergency_slots=emergency_slots,
    )
    buffer = InMemorySnapshotBuffer(clock=lambda: _NOW) if with_buffer else None
    if buffer is not None and preload:
        for snapshot in _demo_snapshots_in_window():
            buffer.append("demo_arterial", snapshot)
    if no_config:
        corridors = None  # a deployment with no scheduled corridor config
    elif corridors is None and with_buffer:
        corridors = {"demo_arterial": _corridor_cfg(tmp_path)}
    launched: list[tuple[str, dict, str]] = []
    output_root = tmp_path / "out"
    output_root.mkdir(exist_ok=True)

    app = FastAPI()
    app.include_router(
        create_v1_city_router(
            store,
            lambda run_id, request, out: launched.append((run_id, request, out)),
            output_root=output_root,
            input_root=None,
            buffer=buffer,
            corridors=corridors,
            window_s=3600,
        )
    )
    return TestClient(app), store, output_root, launched


# --- exactly one of tmc_json / corridor_id ---------------------------------------


def test_both_tmc_json_and_corridor_id_422(tmp_path: Path) -> None:
    client, _store, _out, launched = _make_client(tmp_path)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    resp = client.post(
        _BASE,
        json={"tmc_json": tmc, "base_sumocfg": sumocfg, "corridor_id": "demo_arterial"},
    )
    assert resp.status_code == 422
    assert launched == []


def test_neither_tmc_json_nor_corridor_id_422(tmp_path: Path) -> None:
    client, _store, _out, launched = _make_client(tmp_path)
    _tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    resp = client.post(_BASE, json={"base_sumocfg": sumocfg})
    assert resp.status_code == 422
    assert launched == []


# --- inline incident: schema ------------------------------------------------------


# --- inline incident: happy path --------------------------------------------------


def test_incident_happy_path_forwards_stalls_and_trigger(tmp_path: Path) -> None:
    client, store, output_root, launched = _make_client(tmp_path)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")

    resp = client.post(
        _BASE,
        json={
            "tmc_json": tmc,
            "base_sumocfg": sumocfg,
            "n_workers": 2,
            "incident": {"stalled_vehicles": [_STALL]},
        },
    )
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert store.get(run_id).status == "submitted"

    assert len(launched) == 1
    launched_run_id, request, _out_dir = launched[0]
    assert launched_run_id == run_id
    assert request["trigger"] == "incident"
    # The incident never reaches the worker as a model field; it becomes the
    # server-generated stall_data file under output_root/<run_id>/inputs/.
    assert "incident" not in request
    stalls_path = Path(request["stall_data"])
    assert stalls_path == output_root / run_id / "inputs" / "stalls.json"
    stalls = json.loads(stalls_path.read_text(encoding="utf-8"))
    assert stalls == {"stalled_vehicles": [_STALL]}


def test_no_incident_forwards_no_trigger_or_stall_data(tmp_path: Path) -> None:
    client, _store, _out, launched = _make_client(tmp_path)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    resp = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg})
    assert resp.status_code == 202
    request = launched[0][1]
    assert "trigger" not in request
    assert "stall_data" not in request


# --- corridor_id mode --------------------------------------------------------------


def test_corridor_id_happy_path_aggregates_and_merges_defaults(tmp_path: Path) -> None:
    client, store, output_root, launched = _make_client(tmp_path)

    # max_trials overrides the config entry's 6; sim_end stays the config's 600.
    resp = client.post(_BASE, json={"corridor_id": "demo_arterial", "max_trials": 9})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert store.get(run_id).status == "submitted"

    assert len(launched) == 1
    _rid, request, _out_dir = launched[0]

    # Server-generated aggregated demand under output_root/<run_id>/inputs/.
    aggregated_path = Path(request["tmc_json"])
    assert aggregated_path == output_root / run_id / "inputs" / "aggregated_tmc.json"
    aggregated = json.loads(aggregated_path.read_text(encoding="utf-8"))
    assert aggregated["corridor_id"] == "demo_arterial"
    # Two 10-minute snapshots aggregated: counts summed, window 1200 s.
    assert aggregated["observation_interval"] == "1200s"
    snapshots = _demo_snapshots_in_window()
    expected_east_straight = sum(
        snap["intersections"][0]["counts"]["East"]["Straight"]
        for snap in snapshots
    )
    by_id = {item["intersection_id"]: item for item in aggregated["intersections"]}
    assert by_id["i1"]["counts"]["East"]["Straight"] == expected_east_straight

    # Config-entry defaults merged in; explicit request field wins.
    assert request["base_sumocfg"] == str(_DEMO_DIR / "demo.sumocfg")
    assert request["corridor"]["tls_ids"] == ["J1", "J2", "J3"]
    assert request["baseline_sheet"] == str(
        tmp_path / "baseline_sheet_uncoordinated.json"
    )
    assert request["sim_end"] == 600
    assert request["n_workers"] == 2
    assert request["max_trials"] == 9  # request override wins over the config's 6
    assert "corridor_id" not in request


def test_unknown_corridor_queue_without_snapshots_returns_empty_window(
    tmp_path: Path,
) -> None:
    client, _store, _out, launched = _make_client(tmp_path)
    resp = client.post(_BASE, json={"corridor_id": "nope"})
    assert resp.status_code == 409
    assert "no recent traffic data buffered" in resp.json()["detail"]
    assert launched == []


def test_empty_window_409(tmp_path: Path) -> None:
    client, _store, _out, launched = _make_client(tmp_path, preload=False)
    resp = client.post(_BASE, json={"corridor_id": "demo_arterial"})
    assert resp.status_code == 409
    assert "no recent traffic data buffered" in resp.json()["detail"]
    assert launched == []


def test_corridor_id_without_buffer_400(tmp_path: Path) -> None:
    client, _store, _out, launched = _make_client(tmp_path, with_buffer=False)
    resp = client.post(_BASE, json={"corridor_id": "demo_arterial"})
    assert resp.status_code == 400
    assert "snapshot buffer" in resp.json()["detail"]
    assert launched == []


# --- corridor_id mode without a config entry (queue-driven on-demand) --------------


def test_corridor_id_not_in_config_with_request_base_sumocfg_202(
    tmp_path: Path,
) -> None:
    # The queue key is unknown to the scheduled config, but the request brings
    # its own base_sumocfg: buffered demand is aggregated and forwarded.
    _tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    client, store, output_root, launched = _make_client(
        tmp_path, corridors={"some_other_corridor": _corridor_cfg(tmp_path)}
    )
    resp = client.post(
        _BASE, json={"corridor_id": "demo_arterial", "base_sumocfg": sumocfg}
    )
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert store.get(run_id).status == "submitted"

    assert len(launched) == 1
    _rid, request, _out_dir = launched[0]
    aggregated_path = Path(request["tmc_json"])
    assert aggregated_path == output_root / run_id / "inputs" / "aggregated_tmc.json"
    aggregated = json.loads(aggregated_path.read_text(encoding="utf-8"))
    assert aggregated["corridor_id"] == "demo_arterial"
    assert aggregated["observation_interval"] == "1200s"
    # No config entry merged: the request's base_sumocfg is the one forwarded.
    assert request["base_sumocfg"] == sumocfg
    assert request.get("baseline_sheet") is None


def test_no_config_corridor_without_base_sumocfg_defers_to_auto_map(
    tmp_path: Path,
) -> None:
    client, _store, _out, launched = _make_client(tmp_path, no_config=True)
    resp = client.post(_BASE, json={"corridor_id": "demo_arterial"})
    assert resp.status_code == 202
    assert len(launched) == 1
    assert launched[0][1].get("base_sumocfg") is None


def test_corridor_id_with_explicit_corridor_geometry_wins(tmp_path: Path) -> None:
    # corridor_id names the demand QUEUE; corridor names the GEOMETRY. The
    # explicit from/to beats the config entry's tls_ids corridor.
    client, _store, _out, launched = _make_client(tmp_path)
    resp = client.post(
        _BASE,
        json={
            "corridor_id": "demo_arterial",
            "corridor": {"from": "i1", "to": "i3"},
        },
    )
    assert resp.status_code == 202
    _rid, request, _out_dir = launched[0]
    corridor = request["corridor"]
    assert corridor["from_ref"] == "i1"
    assert corridor["to_ref"] == "i3"
    assert corridor["tls_ids"] == []  # config's ["J1","J2","J3"] did not merge
    # Demand still comes from the queue aggregation, defaults still merge.
    assert Path(request["tmc_json"]).name == "aggregated_tmc.json"
    assert request["base_sumocfg"] == str(_DEMO_DIR / "demo.sumocfg")


def test_corridor_id_without_base_sumocfg_defers_to_auto_map(tmp_path: Path) -> None:
    # base_sumocfg is optional now: with neither base_sumocfg nor map_id the
    # run is accepted and the worker auto-selects the covering map at run time.
    cfg = _corridor_cfg(tmp_path)
    del cfg["base_sumocfg"]
    client, _store, _out, launched = _make_client(
        tmp_path, corridors={"demo_arterial": cfg}
    )
    resp = client.post(_BASE, json={"corridor_id": "demo_arterial"})
    assert resp.status_code == 202
    assert len(launched) == 1
    assert launched[0][1].get("base_sumocfg") is None


def test_corridor_id_with_incident_expedites_and_forwards_both(tmp_path: Path) -> None:
    # The old incident-endpoint flow, now inline: corridor demand + stalls.
    client, _store, output_root, launched = _make_client(tmp_path)
    resp = client.post(
        _BASE,
        json={
            "corridor_id": "demo_arterial",
            "incident": {"stalled_vehicles": [_STALL]},
        },
    )
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    _rid, request, _out_dir = launched[0]
    assert request["trigger"] == "incident"
    assert Path(request["tmc_json"]).name == "aggregated_tmc.json"
    stalls_path = Path(request["stall_data"])
    assert stalls_path == output_root / run_id / "inputs" / "stalls.json"
    assert json.loads(stalls_path.read_text(encoding="utf-8")) == {
        "stalled_vehicles": [_STALL]
    }


# --- emergency slots ---------------------------------------------------------------


def test_incident_uses_emergency_headroom_normal_does_not(tmp_path: Path) -> None:
    # Cap 4, emergency headroom 2. The spy launcher never releases slots, so
    # submitted jobs keep holding them (like still-running workers).
    client, _store, _out, _launched = _make_client(
        tmp_path, max_concurrency=4, emergency_slots=2
    )
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    normal = {"tmc_json": tmc, "base_sumocfg": sumocfg, "n_workers": 2}

    # Two normal 2-worker jobs fill the cap.
    assert client.post(_BASE, json=normal).status_code == 202
    assert client.post(_BASE, json=normal).status_code == 202
    # A third normal submit is over the cap; the headroom is NOT for it.
    assert client.post(_BASE, json=normal).status_code == 429

    # An incident submit rides the reserved emergency headroom (4 + 2 slots).
    incident = {**normal, "incident": {"stalled_vehicles": [_STALL]}}
    assert client.post(_BASE, json=incident).status_code == 202

    # Headroom exhausted: even an incident needing one more slot 429s.
    one_more = {
        "tmc_json": tmc,
        "base_sumocfg": sumocfg,
        "n_workers": 1,
        "incident": {"stalled_vehicles": [_STALL]},
    }
    assert client.post(_BASE, json=one_more).status_code == 429



def test_n_workers_beyond_cap_and_headroom_is_422_for_incidents_too(tmp_path: Path) -> None:
    # Cap 3, emergency headroom 2: a normal 4-worker request can never run (422),
    # an incident may use the headroom (up to 5 workers), beyond that 422 as well.
    client, store, _out, launched = _make_client(
        tmp_path, max_concurrency=3, emergency_slots=2
    )
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    normal = {"tmc_json": tmc, "base_sumocfg": sumocfg, "n_workers": 4}

    resp = client.post(_BASE, json=normal)
    assert resp.status_code == 422
    assert "TERASIM_CITY_MAX_CONCURRENCY=3" in resp.json()["detail"]

    incident = {**normal, "incident": {"stalled_vehicles": [_STALL]}}
    assert client.post(_BASE, json=incident).status_code == 202

    too_many = {**incident, "n_workers": 6}
    resp = client.post(_BASE, json=too_many)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "n_workers=6" in detail
    assert "TERASIM_CITY_MAX_CONCURRENCY=3" in detail
    assert "TERASIM_CITY_EMERGENCY_SLOTS=2" in detail
    assert len(launched) == 1


def test_corridor_id_request_ignores_scheduler_only_entry_keys(tmp_path: Path) -> None:
    # rerun_unchanged steers the scheduler; a corridor_id request merging the entry
    # must not forward it into the request model (which rejects unknown fields).
    corridors = {"demo_arterial": {**_corridor_cfg(tmp_path), "rerun_unchanged": True}}
    client, _store, _out, launched = _make_client(tmp_path, corridors=corridors)

    resp = client.post(_BASE, json={"corridor_id": "demo_arterial"})

    assert resp.status_code == 202, resp.text
    assert "rerun_unchanged" not in launched[0][1]
