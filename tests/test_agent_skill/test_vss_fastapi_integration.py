"""Real CityOptimizationClient -> FastAPI VSS workflow contract."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from terasim_agent_skill.client import CityOptimizationClient
from terasim_service.city.asset_api import build_asset_router
from terasim_service.city.ingest_api import (
    InMemorySnapshotBuffer,
    create_snapshot_router,
)
from terasim_service.city.job_store import InMemoryCityJobStore
from terasim_service.city.optimize_api import create_v1_city_router


REPO_ROOT = Path(__file__).resolve().parents[2]


class _TestClientTransport(httpx.BaseTransport):
    """Route an ``httpx.Client`` through Starlette's ``TestClient`` (public API only).

    Starlette 1.x builds its TestClient on ``httpx2``, so its private
    ``_transport`` no longer plugs into an ``httpx`` 0.x client; the seam goes
    through ``TestClient.request`` instead.
    """

    _DROP = {"content-length", "transfer-encoding", "content-encoding"}

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        reply = self._client.request(
            request.method,
            str(request.url),
            content=request.read(),
            headers=dict(request.headers),
        )
        headers = [(k, v) for k, v in reply.headers.multi_items() if k.lower() not in self._DROP]
        return httpx.Response(
            reply.status_code, headers=headers, content=reply.content, request=request
        )


# The synthetic demo corridor (examples/corridor_demo): i1 and i3 are signals J1 and J3.
DEMO_MAP = REPO_ROOT / "examples" / "corridor_demo" / "demo.sumocfg"
CORRIDOR_ID = "demo_arterial"
FROM = {"intersection_id": "i1", "name": "Main St & 1st Ave", "lat": 37.335, "lon": -121.90339}
TO = {"intersection_id": "i3", "name": "Main St & 3rd Ave", "lat": 37.335, "lon": -121.89661}
NOW = datetime.fromisoformat("2026-07-01T09:00:00+00:00").timestamp()


def _snapshot() -> dict:
    counts = {
        direction: {turn: 2 for turn in ("Right", "Straight", "Left")}
        for direction in ("South", "West", "North", "East")
    }
    return {
        "corridor_id": CORRIDOR_ID,
        "timestamp_start": "2026-07-01T08:50:00Z",
        "observation_interval": "10m",
        "intersections": [{**FROM, "counts": counts}, {**TO, "counts": counts}],
    }


def _plan() -> dict:
    intersections = []
    for target in (FROM, TO):
        intersections.append(
            {
                "intersection_id": target["intersection_id"],
                "name": target["name"],
                "lat": target["lat"],
                "lon": target["lon"],
                "phases": [
                    {"phase": 2, "movements": ["EB-through"]},
                    {"phase": 6, "movements": ["WB-through"]},
                ],
                "rings": {
                    "ring1": [2],
                    "ring2": [6],
                    "coordinated_phases": [2, 6],
                },
                "patterns": [
                    {
                        "pattern_id": "am",
                        "cycle_s": 100,
                        "splits": {"2": 50, "6": 50},
                    }
                ],
                "schedule": [],
            }
        )
    return {
        "schema": "terasim.nema_plan.v1",
        "corridor_id": CORRIDOR_ID,
        "intersections": intersections,
        "provenance": {
            "source_files": ["signal_plan.pdf"],
            "handoff_ready": False,
            "gaps": [],
        },
    }


def test_client_drives_snapshot_plan_and_sync_corridor_through_fastapi(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = tmp_path / "data"
    output_root = tmp_path / "outputs"
    monkeypatch.setenv("TERASIM_CITY_DATA_ROOT", str(data_root))
    buffer = InMemorySnapshotBuffer(clock=lambda: NOW)
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    captured: dict = {}

    def acquire_map(tmc_json: Path) -> Path:
        captured["aggregated_tmc"] = json.loads(tmc_json.read_text(encoding="utf-8"))
        return DEMO_MAP

    def optimize(config, *, progress_callback):
        captured["config"] = config
        config.output_dir.mkdir(parents=True, exist_ok=True)
        progress_callback(1, 1, "optimized")
        nema = {
            "schema": "terasim.nema_timing.v1",
            "corridor_id": CORRIDOR_ID,
            "handoff_ready": True,
            "intersections": [{"intersection_id": "i1"}],
        }
        (config.output_dir / "nema_plan.json").write_text(
            json.dumps(nema), encoding="utf-8"
        )
        return {
            "baseline": {
                "score": 1.0,
                "moe": {
                    "control_delay_s_per_veh": 30.0,
                    "stops_per_veh": 1.4,
                },
            },
            "best": {
                "score": 0.75,
                "moe": {
                    "control_delay_s_per_veh": 20.0,
                    "stops_per_veh": 1.1,
                },
            },
        }

    app = FastAPI()
    app.include_router(build_asset_router(data_root))
    app.include_router(create_snapshot_router(buffer, clock=lambda: NOW))
    app.include_router(
        create_v1_city_router(
            store,
            lambda *_args: None,
            output_root=output_root,
            buffer=buffer,
            direct_optimizer=optimize,
            map_acquirer=acquire_map,
        )
    )

    with TestClient(app) as api_client:
        snapshot_client = CityOptimizationClient(
            "http://testserver", transport=_TestClientTransport(api_client)
        )
        plan_client = CityOptimizationClient(
            "http://testserver", transport=_TestClientTransport(api_client)
        )
        optimize_client = CityOptimizationClient(
            "http://testserver", transport=_TestClientTransport(api_client)
        )
        try:
            assert snapshot_client.push_snapshot(_snapshot())["buffered"] == 1
            stored = plan_client.upload_plan(_plan(), corridor_id=CORRIDOR_ID)
            result = optimize_client.optimize_corridor(
                CORRIDOR_ID,
                dict(FROM),
                dict(TO),
                cost={"weights": {"control_delay": 0.8, "stops": 0.2}},
            )
        finally:
            snapshot_client.close()
            plan_client.close()
            optimize_client.close()

    assert stored["stored"] == [
        [CORRIDOR_ID, "i1"],
        [CORRIDOR_ID, "i3"],
    ]
    assert set(result) == {"nema_plan", "kpis"}
    assert "run_id" not in result
    assert result["kpis"] == {
        "cost_weights": {"control_delay": 0.8, "stops": 0.2},
        "baseline": {
            "cost": 1.0,
            "metrics": {
                "control_delay_s_per_veh": 30.0,
                "stops_per_veh": 1.4,
            },
        },
        "optimized": {
            "cost": 0.75,
            "metrics": {
                "control_delay_s_per_veh": 20.0,
                "stops_per_veh": 1.1,
            },
        },
    }
    assert captured["config"].from_ref == "i1"
    assert captured["config"].to_ref == "i3"
    assert captured["config"].base_sumocfg == DEMO_MAP
    assert captured["aggregated_tmc"]["intersections"][0]["lat"] == 37.335
    assert captured["aggregated_tmc"]["intersections"][1]["lon"] == -121.89661
    plan_files = sorted((data_root / "plans" / CORRIDOR_ID).rglob("plan.json"))
    bound = {
        plan["intersections"][0]["intersection_id"]: plan["intersections"][0]
        for plan in (
            json.loads(path.read_text(encoding="utf-8")) for path in plan_files
        )
    }
    assert (bound["i1"]["lat"], bound["i1"]["lon"]) == (37.335, -121.90339)
    assert (bound["i3"]["lat"], bound["i3"]["lon"]) == (37.335, -121.89661)
    # The response carries no run id, but the server keeps the run like any other
    # (origin "direct"): one job record and one run directory under the output root.
    (record,) = store.list_runs()
    assert (record.origin, record.status, record.corridor_id) == (
        "direct", "completed", CORRIDOR_ID,
    )
    assert record.intersection_ids == ["i1", "i3"]
    assert [path.name for path in output_root.iterdir()] == [record.run_id]
