"""POST /v1/city/optimize on a corridor whose scheduled entry names a timing sheet.

The on-demand request covers one intersection, or a from/to pair inside the corridor,
while the entry's ``baseline_sheet`` describes the whole corridor. The run takes the
sheet's entries for its own signals (200); a signal the sheet has no entry for is a
422 that names it (it used to be an unhandled error: HTTP 500).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from terasim_city.plans.baseline_sheet import TimingSheetCoverageError
from terasim_service.city.ingest_api import InMemorySnapshotBuffer
from terasim_service.city.job_store import InMemoryCityJobStore
from terasim_service.city.optimize_api import create_v1_city_router

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
_ENDPOINT = "/v1/city/optimize"
_NOW = datetime.fromisoformat("2026-07-01T08:30:00+00:00").timestamp()
_I = {"i1": (37.335, -121.90339), "i2": (37.335, -121.9), "i3": (37.335, -121.89661)}


def _target(intersection_id: str) -> dict:
    lat, lon = _I[intersection_id]
    return {"intersection_id": intersection_id, "lat": lat, "lon": lon}


def _client(tmp_path: Path, *, sheet: dict, direct_optimizer=None):
    tmc = json.loads((DEMO_DIR / "demo_tmc.json").read_text(encoding="utf-8"))
    tmc["timestamp_start"] = "2026-07-01T08:00:00Z"
    buffer = InMemorySnapshotBuffer(clock=lambda: _NOW)
    buffer.append("demo_arterial", tmc)
    sheet_path = tmp_path / "baseline_sheet.json"
    sheet_path.write_text(json.dumps(sheet), encoding="utf-8")
    corridors = {
        "demo_arterial": {
            "corridor_id": "demo_arterial",
            "base_sumocfg": str(DEMO_DIR / "demo.sumocfg"),
            "corridor": {"tls_ids": ["J1", "J2", "J3"]},
            "baseline_sheet": str(sheet_path),
            "optimizer": "exhaustive",
            "max_trials": 1,
            "sim_end": 120,
            "n_workers": 1,
        }
    }
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    app = FastAPI()
    app.include_router(create_v1_city_router(
        store, lambda *_a: None, output_root=tmp_path / "out", buffer=buffer,
        corridors=corridors, direct_optimizer=direct_optimizer,
    ))
    return TestClient(app), store


def _demo_sheet() -> dict:
    return json.loads((DEMO_DIR / "baseline_sheet.json").read_text(encoding="utf-8"))


def test_a_sheet_coverage_error_is_a_422_naming_the_signal(tmp_path: Path) -> None:
    def optimizer(config, *, progress_callback):
        raise TimingSheetCoverageError(
            "timing sheet /x/sheet.json has no entry for signal(s) ['J2'] of the "
            "requested ['J1', 'J2', 'J3']; it describes ['J1', 'J3']"
        )

    client, store = _client(tmp_path, sheet=_demo_sheet(), direct_optimizer=optimizer)

    response = client.post(_ENDPOINT, json={
        "type": "corridor", "corridor_id": "demo_arterial",
        "corridor": {"from": _target("i1"), "to": _target("i3")},
    })

    assert response.status_code == 422
    assert "has no entry for signal(s) ['J2']" in response.json()["detail"]
    (record,) = store.list_runs()
    assert record.status == "failed" and "J2" in record.error


def _sumo_runtime() -> bool:
    return shutil.which("sumo") is not None and importlib.util.find_spec("libsumo") is not None


@pytest.mark.parametrize(
    ("body", "signals"),
    [
        ({"type": "intersection", "intersection": _target("i2")}, ["J2"]),
        ({"type": "corridor", "corridor": {"from": _target("i1"), "to": _target("i2")}},
         ["J1", "J2"]),
    ],
    ids=["single_intersection", "from_to_two_of_three"],
)
@pytest.mark.integration
@pytest.mark.requires_sumo
@pytest.mark.skipif(not _sumo_runtime(), reason="needs SUMO (sumo binary and libsumo)")
def test_the_sheet_plan_restricted_to_the_requested_signals(tmp_path, body, signals):
    client, store = _client(tmp_path, sheet=_demo_sheet())

    response = client.post(_ENDPOINT, json={"corridor_id": "demo_arterial", **body})

    assert response.status_code == 200, response.text
    nema = response.json()["nema_plan"]
    assert [entry["tls_id"] for entry in nema["intersections"]] == signals
    sheet = {entry["tls_id"]: entry for entry in _demo_sheet()["intersections"]}
    for entry in nema["intersections"]:
        # The baseline is the sheet's own timing for that signal (exhaustive: the
        # only seed, hence also the exported plan): NEMA 2+6 then 4+8, cycle 90.
        greens = [row["green_s"] for row in entry["phases"]]
        assert greens == [p["green_s"] for p in sheet[entry["tls_id"]]["phases"]
                          for _ in p["nema_phases"]]
    assert nema["coordination"]["cycle_length_s"] == 90.0
    (record,) = store.list_runs()
    assert record.status == "completed"


@pytest.mark.integration
@pytest.mark.requires_sumo
@pytest.mark.skipif(not _sumo_runtime(), reason="needs SUMO (sumo binary and libsumo)")
def test_a_requested_signal_missing_from_the_sheet_is_a_422(tmp_path: Path) -> None:
    sheet = _demo_sheet()
    sheet["intersections"] = [copy.deepcopy(e) for e in sheet["intersections"]
                              if e["tls_id"] != "J2"]
    client, store = _client(tmp_path, sheet=sheet)

    # From i1 to i3 the path crosses J2, which this sheet does not describe.
    response = client.post(_ENDPOINT, json={
        "type": "corridor", "corridor_id": "demo_arterial",
        "corridor": {"from": _target("i1"), "to": _target("i3")},
    })

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "has no entry for signal(s) ['J2']" in detail
    (record,) = store.list_runs()
    assert record.status == "failed"
