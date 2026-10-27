"""Asset upload endpoints: /v1/city/plans and /v1/city/maps.

FastAPI TestClient over the router alone — no Redis, no SUMO.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from terasim_service.city.asset_api import build_asset_router
from tests.test_city.hd_map_fixtures import tiny_lanelet2, tiny_opendrive

REPO_ROOT = Path(__file__).resolve().parents[2]
requires_netconvert = pytest.mark.skipif(
    shutil.which("netconvert") is None, reason="netconvert not on PATH"
)
LOCAL_TMERC = (
    "+proj=tmerc +lat_0=37.30 +lon_0=-121.90 +k=1 +x_0=0 +y_0=0 "
    "+datum=WGS84 +units=m +no_defs"
)


@pytest.fixture()
def client(tmp_path):
    app = FastAPI()
    app.include_router(build_asset_router(tmp_path / "data_root"))
    return TestClient(app)


def _plan(corridor="c1", iid="i1"):
    return {
        "schema": "terasim.nema_plan.v1",
        "corridor_id": corridor,
        "intersections": [{
            "intersection_id": iid, "name": iid.upper(), "lat": 37.0, "lon": -121.0,
            "phases": [{"phase": 2, "movements": ["EB-through"]},
                       {"phase": 6, "movements": ["WB-through"]}],
            "rings": {"ring1": [2], "ring2": [6],
                      "coordinated_phases": [2, 6]},
            "patterns": [{"pattern_id": "am", "cycle_s": 100, "splits": {"2": 50, "6": 50}}],
            "schedule": [],
        }],
        "provenance": {"source_files": ["x.pdf"], "handoff_ready": False, "gaps": []},
    }


def test_upload_plan_and_list(client):
    resp = client.post("/v1/city/plans", json=_plan())
    assert resp.status_code == 201
    assert resp.json()["stored"] == [["c1", "i1"]]

    listing = client.get("/v1/city/plans").json()
    assert listing["count"] == 1 and listing["plans"] == [["c1", "i1"]]


def test_upload_bad_plan_is_400(client):
    resp = client.post("/v1/city/plans", json={"schema": "wrong"})
    assert resp.status_code == 400
    assert "schema" in resp.json()["detail"]


def test_upload_map_requires_path(client):
    resp = client.post("/v1/city/maps", json={})
    assert resp.status_code == 400


def test_upload_map_missing_file_is_400(client):
    resp = client.post("/v1/city/maps", json={"path": "/nope/never.osm"})
    assert resp.status_code == 400


def test_upload_map_and_list(client, tmp_path):
    # A tiny prebuilt scenario the MapStore accepts (net + sumocfg).
    net = tmp_path / "plain.net.xml"
    net.write_text(
        '<?xml version="1.0"?>\n'
        '<net version="1.20" junctionCornerDetail="5" limitTurnSpeed="5.50" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        '  <location netOffset="0.00,0.00" convBoundary="0,0,100,100" '
        'origBoundary="-121.91,37.33,-121.89,37.34" '
        'projParameter="+proj=utm +zone=10 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"/>\n'
        "</net>\n",
        encoding="utf-8",
    )
    routes = tmp_path / "empty.rou.xml"
    routes.write_text("<routes/>\n", encoding="utf-8")
    cfg = tmp_path / "scen.sumocfg"
    cfg.write_text(
        "<configuration><input>"
        f'<net-file value="{net.name}"/><route-files value="{routes.name}"/>'
        "</input></configuration>\n",
        encoding="utf-8",
    )
    resp = client.post("/v1/city/maps", json={"path": str(cfg)})
    assert resp.status_code == 201, resp.text
    map_id = resp.json()["map_id"]
    assert resp.json()["duplicate"] is False

    # Idempotent re-upload.
    again = client.post("/v1/city/maps", json={"path": str(cfg)})
    body = again.json()
    assert (body["map_id"], body["duplicate"], body["source"]) == (map_id, True, "prebuilt")
    assert body["summary"]["format"] == "prebuilt"
    assert body["summary"]["geo_referenced"] is True
    assert client.get("/v1/city/maps").json()["maps"] == [map_id]


@pytest.mark.parametrize("proj", [7, "", "   "])
def test_upload_map_rejects_a_malformed_projection_override(client, tmp_path, proj):
    resp = client.post("/v1/city/maps", json={"path": str(tmp_path / "x.xodr"), "proj": proj})
    assert resp.status_code == 400
    assert "proj" in resp.json()["detail"]


def test_upload_map_refuses_a_projection_for_a_prebuilt_map(client, tmp_path):
    cfg = tmp_path / "scen.sumocfg"
    cfg.write_text("<configuration/>\n", encoding="utf-8")
    resp = client.post(
        "/v1/city/maps",
        json={"path": str(cfg), "proj": "+proj=utm +zone=10 +datum=WGS84 +units=m +no_defs"},
    )
    assert resp.status_code == 400
    assert "projection override" in resp.json()["detail"]


@requires_netconvert
def test_upload_opendrive_without_geo_reference_asks_for_a_projection(client, tmp_path):
    xodr = tmp_path / "local.xodr"
    xodr.write_text(tiny_opendrive(geo_reference=None, origin=(0.0, 0.0)), encoding="utf-8")

    refused = client.post("/v1/city/maps", json={"path": str(xodr)})
    assert refused.status_code == 400
    assert "geoReference" in refused.json()["detail"]
    assert "proj" in refused.json()["detail"]

    resp = client.post("/v1/city/maps", json={"path": str(xodr), "proj": LOCAL_TMERC})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "opendrive" and body["duplicate"] is False
    summary = body["summary"]
    assert (summary["edges"], summary["intersections"], summary["traffic_lights"]) == (6, 1, 1)
    assert summary["geo_referenced"] is True and summary["proj"] == LOCAL_TMERC
    assert client.get("/v1/city/maps").json()["maps"] == [body["map_id"]]


@requires_netconvert
def test_upload_lanelet2_map(client, tmp_path):
    osm = tmp_path / "lanelet2_map.osm"
    osm.write_text(tiny_lanelet2(), encoding="utf-8")
    resp = client.post("/v1/city/maps", json={"path": str(osm)})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "lanelet2"
    assert body["summary"]["traffic_lights"] == 1
    assert body["summary"]["lanelet2"]["driving_side"] == "right"


@requires_netconvert
def test_unconvertible_map_is_422(client, tmp_path):
    empty = tmp_path / "empty.xodr"
    empty.write_text(
        '<?xml version="1.0"?>\n<OpenDRIVE><header revMajor="1" revMinor="6"><geoReference>'
        "<![CDATA[+proj=utm +zone=10 +datum=WGS84 +units=m +no_defs]]></geoReference></header>"
        "</OpenDRIVE>\n",
        encoding="utf-8",
    )
    resp = client.post("/v1/city/maps", json={"path": str(empty)})
    assert resp.status_code == 422
    assert "netconvert failed" in resp.json()["detail"]
