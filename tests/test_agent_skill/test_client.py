"""Unit tests for CityOptimizationClient against a stub httpx transport."""

from __future__ import annotations

import copy
import json

import httpx
import pytest

from terasim_agent_skill.client import CityOptimizationClient

BASE_URL = "http://testserver"


def _client_with_handler(handler) -> CityOptimizationClient:
    return CityOptimizationClient(
        BASE_URL, timeout_s=5.0, transport=httpx.MockTransport(handler)
    )


class _Recorder:
    """MockTransport handler that records requests and replays responses."""

    def __init__(self, responses):
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def _json_response(payload: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


# -- body construction --------------------------------------------------------


def test_submit_builds_body_with_from_to_aliases():
    recorder = _Recorder([_json_response({"run_id": "r1", "status": "submitted"})])
    with _client_with_handler(recorder) as client:
        response = client.request_signal_optimization(
            tmc_json="tmc.json",
            base_sumocfg="demo.sumocfg",
            from_intersection="i1",
            to_intersection="i3",
            baseline_sheet="sheet.json",
            optimizer="bayes_offsets",
            max_trials=8,
            sim_end=300.0,
            n_workers=2,
            kpi_weights={"control_delay": 0.7, "stops": 0.3},
        )

    assert response == {"run_id": "r1", "status": "submitted"}
    request = recorder.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/city/optimizations"
    body = json.loads(request.content)
    assert body["corridor"] == {"from": "i1", "to": "i3"}
    assert body["tmc_json"] == "tmc.json"
    assert body["base_sumocfg"] == "demo.sumocfg"
    assert body["baseline_sheet"] == "sheet.json"
    assert body["optimizer"] == "bayes_offsets"
    assert body["max_trials"] == 8
    assert body["sim_end"] == 300.0
    assert body["n_workers"] == 2
    assert body["kpi_weights"] == {"control_delay": 0.7, "stops": 0.3}
    # None fields are omitted entirely.
    assert "corridor_id" not in body
    assert "incident" not in body


def test_submit_omits_none_fields_and_corridor_when_unset():
    recorder = _Recorder([_json_response({"run_id": "r2", "status": "submitted"})])
    with _client_with_handler(recorder) as client:
        client.request_signal_optimization(
            tmc_json="tmc.json", base_sumocfg="demo.sumocfg"
        )

    body = json.loads(recorder.requests[0].content)
    assert set(body) == {
        "tmc_json",
        "base_sumocfg",
        "optimizer",
        "max_trials",
        "n_workers",
    }
    assert body["optimizer"] == "auto"
    assert body["max_trials"] == 20
    assert body["n_workers"] == 2


def test_submit_with_tls_ids_corridor():
    recorder = _Recorder([_json_response({"run_id": "r3", "status": "submitted"})])
    with _client_with_handler(recorder) as client:
        client.request_signal_optimization(
            tmc_json="tmc.json",
            base_sumocfg="demo.sumocfg",
            tls_ids=["J1", "J2", "J3"],
        )

    body = json.loads(recorder.requests[0].content)
    assert body["corridor"] == {"tls_ids": ["J1", "J2", "J3"]}


def test_submit_corridor_id_only_body():
    recorder = _Recorder([_json_response({"run_id": "r4", "status": "submitted"})])
    with _client_with_handler(recorder) as client:
        client.request_signal_optimization(corridor_id="demo_arterial")

    body = json.loads(recorder.requests[0].content)
    assert set(body) == {"corridor_id", "optimizer", "max_trials", "n_workers"}
    assert body["corridor_id"] == "demo_arterial"
    assert "tmc_json" not in body
    assert "base_sumocfg" not in body


def test_submit_corridor_id_with_explicit_geometry_body():
    """corridor_id (demand queue) + from/to (geometry) travel together:
    the body carries both corridor_id and the aliased corridor object, plus
    the caller's base_sumocfg for queue-driven corridors with no server-side
    config entry."""
    recorder = _Recorder([_json_response({"run_id": "r7", "status": "submitted"})])
    with _client_with_handler(recorder) as client:
        client.request_signal_optimization(
            corridor_id="demo_arterial",
            base_sumocfg="demo.sumocfg",
            from_intersection="i1",
            to_intersection="i3",
        )

    body = json.loads(recorder.requests[0].content)
    assert body["corridor_id"] == "demo_arterial"
    assert body["corridor"] == {"from": "i1", "to": "i3"}
    assert body["base_sumocfg"] == "demo.sumocfg"
    assert "tmc_json" not in body


def test_submit_incident_passed_through_verbatim():
    recorder = _Recorder([_json_response({"run_id": "r5", "status": "submitted"})])
    incident = {
        "stalled_vehicles": [
            {"latitude": 37.3001, "longitude": -121.9004},
            {"latitude": 37.3002, "longitude": -121.9010},
        ]
    }
    with _client_with_handler(recorder) as client:
        client.request_signal_optimization(
            corridor_id="demo_arterial", incident=incident
        )

    body = json.loads(recorder.requests[0].content)
    assert body["incident"] == incident
    assert body["corridor_id"] == "demo_arterial"


def test_submit_exactly_one_source_422_surfaced():
    detail = "exactly one of tmc_json / corridor_id must be provided"
    recorder = _Recorder([httpx.Response(422, json={"detail": detail})])
    with _client_with_handler(recorder) as client:
        with pytest.raises(RuntimeError) as excinfo:
            client.request_signal_optimization(
                tmc_json="tmc.json",
                base_sumocfg="demo.sumocfg",
                corridor_id="demo_arterial",
            )
    message = str(excinfo.value)
    assert "422" in message
    assert detail in message


def test_submit_corridor_id_only_tool_path():
    """Mirror the MCP tool's corridor_id-only call: every file-path and
    from/to param left as None must be omitted from the body."""
    recorder = _Recorder([_json_response({"run_id": "r6", "status": "submitted"})])
    with _client_with_handler(recorder) as client:
        response = client.request_signal_optimization(
            tmc_json=None,
            base_sumocfg=None,
            corridor_id="demo_arterial",
            from_intersection=None,
            to_intersection=None,
            baseline_sheet=None,
            max_trials=12,
        )

    assert response == {"run_id": "r6", "status": "submitted"}
    body = json.loads(recorder.requests[0].content)
    assert set(body) == {"corridor_id", "optimizer", "max_trials", "n_workers"}
    assert body["corridor_id"] == "demo_arterial"
    assert body["max_trials"] == 12
    assert "corridor" not in body


# -- push_snapshot -------------------------------------------------------------


def test_push_snapshot_body_and_route():
    recorder = _Recorder(
        [_json_response({"corridor_id": "demo_arterial", "buffered": 3}, 202)]
    )
    payload = {
        "corridor_id": "demo_arterial",
        "timestamp_start": "2026-07-06T08:00:00+00:00",
        "observation_interval": "10min",
        "intersections": [
            {
                "intersection_id": "i1",
                "name": "First & Main",
                "lat": 37.335,
                "lon": -121.90339,
                "counts": {"EB": {"through": 42}},
            }
        ],
    }
    with _client_with_handler(recorder) as client:
        response = client.push_snapshot(payload)

    assert response == {"corridor_id": "demo_arterial", "buffered": 3}
    request = recorder.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/city/snapshots"
    assert json.loads(request.content) == payload


def test_push_snapshot_422_surfaces_detail():
    detail = "timestamp_start is 600s in the future; the clock skew tolerance is 300s"
    recorder = _Recorder([httpx.Response(422, json={"detail": detail})])
    payload = {
        "corridor_id": "c1",
        "timestamp_start": "2026-07-22T08:00:00-07:00",
        "observation_interval": "10m",
        "intersections": [
            {
                "intersection_id": "i1",
                "name": "First & Main",
                "lat": 37.1,
                "lon": -121.1,
                "counts": {},
            }
        ],
    }
    with _client_with_handler(recorder) as client:
        with pytest.raises(RuntimeError) as excinfo:
            client.push_snapshot(payload)
    message = str(excinfo.value)
    assert "422" in message
    assert detail in message


def test_request_builders_are_stateless_and_accept_complete_intersections():
    oak = {
        "intersection_id": "oak",
        "name": "Park Ave & Oak St",
        "lat": 37.335,
        "lon": -121.90339,
    }
    cedar = {
        "intersection_id": "cedar",
        "name": "Park Ave & Cedar St",
        "lat": 37.335,
        "lon": -121.89661,
    }

    recorder = _Recorder([_json_response({})])
    with _client_with_handler(recorder) as client:
        intersection_request = client.build_intersection_request(
            "park_ave", oak
        )
        corridor_request = client.build_corridor_request(
            "park_ave", oak, cedar
        )

    assert intersection_request == {
        "type": "intersection",
        "corridor_id": "park_ave",
        "intersection": oak,
    }
    assert corridor_request == {
        "type": "corridor",
        "corridor_id": "park_ave",
        "corridor": {"from": oak, "to": cedar},
    }
    assert recorder.requests == []


def test_optimize_corridor_calls_sync_endpoint_and_returns_nema_and_kpis():
    response = {
        "nema_plan": {"schema": "terasim.nema_timing.v1", "corridor_id": "c1"},
        "kpis": {
            "cost_weights": {"control_delay": 0.7, "stops": 0.3},
            "baseline": {
                "cost": 1.0,
                "metrics": {"control_delay_s_per_veh": 31.2},
            },
            "optimized": {
                "cost": 0.81,
                "metrics": {"control_delay_s_per_veh": 24.7},
            },
        },
    }
    recorder = _Recorder([_json_response(response)])
    first = {
        "intersection_id": "i1",
        "name": "First & Main",
        "lat": 37.1,
        "lon": -121.1,
    }
    second = {
        "intersection_id": "i2",
        "name": "Second & Main",
        "lat": 37.2,
        "lon": -121.2,
    }
    cost = {"weights": {"control_delay": 0.7, "stops": 0.3}}

    with _client_with_handler(recorder) as client:
        result = client.optimize_corridor("c1", first, second, cost=cost)

    assert result == response
    request = recorder.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/city/optimize"
    assert json.loads(request.content) == {
        "type": "corridor",
        "corridor_id": "c1",
        "corridor": {"from": first, "to": second},
        "cost": cost,
    }


def test_optimize_intersection_uses_explicit_intersection_object():
    response = {
        "nema_plan": {"schema": "terasim.nema_timing.v1", "corridor_id": "c1"},
        "kpis": {
            "cost_weights": {"control_delay": 0.7, "stops": 0.3},
            "baseline": {"cost": 1.0, "metrics": {}},
            "optimized": {"cost": 0.5, "metrics": {}},
        },
    }
    recorder = _Recorder([_json_response(response)])
    intersection = {
        "intersection_id": "i1",
        "name": "First & Main",
        "lat": 37.1,
        "lon": -121.1,
    }

    with _client_with_handler(recorder) as client:
        result = client.optimize_intersection("c1", intersection)

    assert result == response
    assert json.loads(recorder.requests[0].content) == {
        "type": "intersection",
        "corridor_id": "c1",
        "intersection": intersection,
    }


def test_sync_optimization_exposes_only_nema_plan_and_kpis():
    recorder = _Recorder(
        [
            _json_response(
                {
                    "run_id": "legacy-run-id",
                    "nema_plan": {"schema": "terasim.nema_timing.v1"},
                    "kpis": {
                        "run_id": "nested-legacy-run-id",
                        "cost_weights": {"control_delay": 0.7, "stops": 0.3},
                        "baseline": {"cost": 1.0, "metrics": {}},
                        "optimized": {"cost": 0.5, "metrics": {}},
                    },
                },
            )
        ]
    )
    intersection = {
        "intersection_id": "i1",
        "name": "First & Main",
        "lat": 37.1,
        "lon": -121.1,
    }

    with _client_with_handler(recorder) as client:
        result = client.optimize_intersection("c1", intersection)

    assert result == {
        "nema_plan": {"schema": "terasim.nema_timing.v1"},
        "kpis": {
            "cost_weights": {"control_delay": 0.7, "stops": 0.3},
            "baseline": {"cost": 1.0, "metrics": {}},
            "optimized": {"cost": 0.5, "metrics": {}},
        },
    }


def test_sync_optimization_keeps_each_sides_timing_block():
    baseline_timing = {
        "cycle_s": {"n34": 140.0, "n16": 140.0},
        "offsets_s": {"n34": 0.0, "n16": 12.0},
        "greens_s": {"n34": [30.0, 20.0], "n16": [40.0, 25.0]},
    }
    optimized_timing = {
        "cycle_s": {"n34": 164.0, "n16": 164.0},
        "offsets_s": {"n34": 0.0, "n16": 9.0},
        "greens_s": {"n34": [36.0, 22.0], "n16": [44.0, 28.0]},
    }
    response = {
        "nema_plan": {"schema": "terasim.nema_timing.v1"},
        "kpis": {
            "cost_weights": {"control_delay": 0.7, "stops": 0.3},
            "baseline": {
                "cost": 1.0,
                "metrics": {"control_delay_s_per_veh": 31.2},
                "timing": baseline_timing,
            },
            "optimized": {
                "cost": 0.81,
                "metrics": {"control_delay_s_per_veh": 24.7},
                "timing": optimized_timing,
            },
        },
    }
    intersection = {
        "intersection_id": "i1",
        "name": "First & Main",
        "lat": 37.1,
        "lon": -121.1,
    }

    with _client_with_handler(_Recorder([_json_response(response)])) as client:
        result = client.optimize_intersection("c1", intersection)

    assert result == response
    assert result["kpis"]["baseline"]["timing"] == baseline_timing
    assert result["kpis"]["optimized"]["timing"] == optimized_timing


_NO_TIMING = object()


@pytest.mark.parametrize(
    "sent",
    [_NO_TIMING, None, ["cycle_s"], "140"],
    ids=["absent", "null", "list", "string"],
)
def test_sync_optimization_without_a_timing_dict_yields_no_timing(sent):
    # Only a dict passes through. Anything else leaves the key out - exactly as the
    # service leaves it out when a result has none - so a response without timing
    # normalises to what it was before timing existed.
    def side(cost: float) -> dict:
        entry = {"cost": cost, "metrics": {}}
        if sent is not _NO_TIMING:
            entry["timing"] = sent
        return entry

    response = {
        "nema_plan": {"schema": "terasim.nema_timing.v1"},
        "kpis": {
            "cost_weights": {"control_delay": 0.7, "stops": 0.3},
            "baseline": side(1.0),
            "optimized": side(0.5),
        },
    }
    intersection = {
        "intersection_id": "i1",
        "name": "First & Main",
        "lat": 37.1,
        "lon": -121.1,
    }

    with _client_with_handler(_Recorder([_json_response(response)])) as client:
        result = client.optimize_intersection("c1", intersection)

    assert result["kpis"] == {
        "cost_weights": {"control_delay": 0.7, "stops": 0.3},
        "baseline": {"cost": 1.0, "metrics": {}},
        "optimized": {"cost": 0.5, "metrics": {}},
    }
    for label in ("baseline", "optimized"):
        assert result["kpis"][label].get("timing") is None


@pytest.mark.parametrize(
    ("optimize_response", "message"),
    [
        ([], "JSON object"),
        (
            {
                "nema_plan": {},
                "kpis": {
                    "cost_weights": {"control_delay": 0.7, "stops": "bad"},
                    "baseline": {"cost": 1.0, "metrics": {}},
                    "optimized": {"cost": 0.5, "metrics": {}},
                },
            },
            "cost_weights",
        ),
        (
            {
                "nema_plan": {},
                "kpis": {
                    "cost_weights": {"unknown": 1.0},
                    "baseline": {"cost": 1.0, "metrics": {}},
                    "optimized": {"cost": 0.5, "metrics": {}},
                },
            },
            "cost_weights",
        ),
        (
            {
                "nema_plan": {},
                "kpis": {
                    "cost_weights": {"stops": 0.0},
                    "baseline": {"cost": 1.0, "metrics": {}},
                    "optimized": {"cost": 0.5, "metrics": {}},
                },
            },
            "cost_weights",
        ),
        (
            {
                "nema_plan": {},
                "kpis": {
                    "cost_weights": {},
                    "baseline": {"cost": 1.0, "metrics": {}},
                    "optimized": {"cost": 0.5, "metrics": {}},
                },
            },
            "cost_weights",
        ),
    ],
)
def test_sync_optimization_rejects_malformed_success_responses(
    optimize_response, message
):
    recorder = _Recorder([httpx.Response(200, json=optimize_response)])
    intersection = {
        "intersection_id": "i1",
        "name": "First & Main",
        "lat": 37.1,
        "lon": -121.1,
    }

    with _client_with_handler(recorder) as client:
        with pytest.raises(RuntimeError, match=message):
            client.optimize_intersection("c1", intersection)


def test_upload_plan_body_route_and_corridor_query():
    recorder = _Recorder(
        [
            _json_response(
                {"stored": [["c1", "i1"]], "notes": ["c1/i1: new"]}, 201
            ),
        ]
    )
    plan = {
        "schema": "terasim.nema_plan.v1",
        "corridor_id": "c1",
        "intersections": [{"intersection_id": "i1", "lat": 37.3, "lon": -121.9}],
    }
    with _client_with_handler(recorder) as client:
        response = client.upload_plan(plan, corridor_id="c1")

    assert response["stored"] == [["c1", "i1"]]
    request = recorder.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/city/plans"
    assert request.url.params.get("corridor_id") == "c1"
    assert json.loads(request.content) == plan


def test_upload_plan_passes_vss_coordinates_through_without_snapshot_or_rewrite():
    recorder = _Recorder(
        [
            _json_response(
                {
                    "stored": [["c1", "i1"], ["c1", "i2"]],
                    "notes": ["c1/i1: new", "c1/i2: new"],
                },
                201,
            ),
        ]
    )
    plan = {
        "schema": "terasim.nema_plan.v1",
        "corridor_id": "c1",
        "intersections": [
            {
                "intersection_id": "i1",
                "lat": 37.123456,
                "lon": -121.123456,
                "patterns": [],
            },
            {
                "intersection_id": "i2",
                "lat": 0.0,
                "lon": 0.0,
                "patterns": [],
            },
        ],
    }

    with _client_with_handler(recorder) as client:
        client.upload_plan(plan, corridor_id="c1")

    posted = json.loads(recorder.requests[0].content)
    assert posted["intersections"][0]["lat"] == 37.123456
    assert posted["intersections"][0]["lon"] == -121.123456
    assert posted["intersections"][1]["lat"] == 0.0
    assert posted["intersections"][1]["lon"] == 0.0
    assert posted == plan
    assert plan["intersections"][1]["lat"] == 0.0


def test_upload_plan_omits_corridor_query_when_unset():
    recorder = _Recorder(
        [
            _json_response({"stored": [["c", "i"]], "notes": []}, 201),
        ]
    )
    plan = {
        "schema": "terasim.nema_plan.v1",
        "corridor_id": "c",
        "intersections": [{"intersection_id": "i", "lat": 37.1, "lon": -121.1}],
    }
    with _client_with_handler(recorder) as client:
        client.upload_plan(plan)
    assert "corridor_id" not in recorder.requests[0].url.params


# -- GET endpoints -------------------------------------------------------------


def test_get_status_result_nema_paths():
    recorder = _Recorder([_json_response({"ok": True})])
    with _client_with_handler(recorder) as client:
        client.get_status("abc")
        client.get_result("abc")
        client.get_nema("abc")

    paths = [request.url.path for request in recorder.requests]
    assert paths == [
        "/v1/city/optimizations/abc/status",
        "/v1/city/optimizations/abc/result",
        "/v1/city/optimizations/abc/nema",
    ]


# -- error surfacing -----------------------------------------------------------


def test_non_2xx_raises_runtimeerror_with_body_text():
    recorder = _Recorder(
        [httpx.Response(400, json={"detail": "input not found: tmc.json"})]
    )
    with _client_with_handler(recorder) as client:
        with pytest.raises(RuntimeError) as excinfo:
            client.get_status("missing")
    message = str(excinfo.value)
    assert "400" in message
    assert "input not found: tmc.json" in message


# -- wait() --------------------------------------------------------------------


def test_wait_polls_until_completed():
    statuses = [
        {"run_id": "r", "status": "running", "progress": {"done": 1, "total": 8}},
        {"run_id": "r", "status": "running", "progress": {"done": 5, "total": 8}},
        {"run_id": "r", "status": "completed", "progress": {"done": 8, "total": 8}},
    ]
    recorder = _Recorder([_json_response(status) for status in statuses])
    with _client_with_handler(recorder) as client:
        final = client.wait("r", timeout_s=5.0, poll_s=0.0)
    assert final["status"] == "completed"
    assert len(recorder.requests) == 3


def test_wait_failed_raises_runtimeerror_with_error_text():
    statuses = [
        {"run_id": "r", "status": "running"},
        {"run_id": "r", "status": "failed", "error": "no valid trial produced"},
    ]
    recorder = _Recorder([_json_response(status) for status in statuses])
    with _client_with_handler(recorder) as client:
        with pytest.raises(RuntimeError, match="no valid trial produced"):
            client.wait("r", timeout_s=5.0, poll_s=0.0)


def test_wait_timeout_raises_timeouterror():
    recorder = _Recorder([_json_response({"run_id": "r", "status": "running"})])
    with _client_with_handler(recorder) as client:
        with pytest.raises(TimeoutError, match="running"):
            client.wait("r", timeout_s=0.05, poll_s=0.01)


def test_upload_map_posts_server_side_path():
    recorder = _Recorder([_json_response({"map_id": "m1", "duplicate": False}, 201)])
    with _client_with_handler(recorder) as client:
        reply = client.upload_map("/data/input/map/demo.sumocfg")
    assert reply == {"map_id": "m1", "duplicate": False}
    request = recorder.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/city/maps"
    assert json.loads(request.content) == {"path": "/data/input/map/demo.sumocfg"}


def test_upload_map_sends_the_projection_override_only_when_given():
    recorder = _Recorder([
        _json_response({"map_id": "m1", "duplicate": False}, 201),
        _json_response({"map_id": "m2", "duplicate": False}, 201),
    ])
    proj = "+proj=utm +zone=10 +datum=WGS84 +units=m +no_defs"
    with _client_with_handler(recorder) as client:
        client.upload_map("/data/input/maps/district/lanelet2_map.osm", proj=proj)
        client.upload_map("/data/input/maps/corridor.xodr")
    assert json.loads(recorder.requests[0].content) == {
        "path": "/data/input/maps/district/lanelet2_map.osm",
        "proj": proj,
    }
    assert json.loads(recorder.requests[1].content) == {"path": "/data/input/maps/corridor.xodr"}


@pytest.mark.parametrize("proj", ["", "   ", 7])
def test_upload_map_rejects_an_empty_projection_override(proj):
    recorder = _Recorder([_json_response({"map_id": "m1", "duplicate": False}, 201)])
    with _client_with_handler(recorder) as client:
        with pytest.raises(ValueError, match="proj"):
            client.upload_map("/data/input/maps/corridor.xodr", proj=proj)
    assert recorder.requests == []


@pytest.mark.parametrize("path", ["", "   ", None])
def test_upload_map_rejects_empty_path(path):
    recorder = _Recorder([_json_response({"map_id": "m1", "duplicate": False}, 201)])
    with _client_with_handler(recorder) as client:
        with pytest.raises(ValueError):
            client.upload_map(path)
    assert recorder.requests == []


def test_upload_map_surfaces_server_error_text():
    recorder = _Recorder([httpx.Response(400, json={"detail": "unsupported map format"})])
    with _client_with_handler(recorder) as client:
        with pytest.raises(RuntimeError, match="unsupported map format"):
            client.upload_map("/data/input/map/bad.txt")


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        ({"n69": ["WB-left", "WB-through"]}, {"n69": ["WB-left", "WB-through"]}),
        ({}, None),
        (["n69"], None),
        (None, None),
    ],
    ids=["some", "empty", "list", "absent"],
)
def test_sync_optimization_passes_unserved_movements_through(sent, kept):
    response = {
        "nema_plan": {"schema": "terasim.nema_timing.v1"},
        "kpis": {
            "cost_weights": {"control_delay": 0.7, "stops": 0.3},
            "baseline": {"cost": 1.0, "metrics": {}},
            "optimized": {"cost": 0.5, "metrics": {}},
        },
    }
    if sent is not None:
        response["unserved_movements"] = sent
    intersection = {"intersection_id": "i1", "name": "First & Main", "lat": 37.1, "lon": -121.1}

    with _client_with_handler(_Recorder([_json_response(response)])) as client:
        result = client.optimize_intersection("c1", intersection)

    if kept is None:
        assert set(result) == {"nema_plan", "kpis"}
    else:
        assert result["unserved_movements"] == kept


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        ({"flow_source": "scenario_routes", "counts_source": "tmc"},
         {"flow_source": "scenario_routes", "counts_source": "tmc"}),
        ("scenario_routes", None),
        (None, None),
    ],
    ids=["dict", "string", "absent"],
)
def test_sync_optimization_passes_the_demand_sources_through(sent, kept):
    response = {
        "nema_plan": {"schema": "terasim.nema_timing.v1"},
        "kpis": {
            "cost_weights": {"control_delay": 0.7, "stops": 0.3},
            "baseline": {"cost": 1.0, "metrics": {}},
            "optimized": {"cost": 0.5, "metrics": {}},
        },
    }
    if sent is not None:
        response["config"] = sent
    intersection = {"intersection_id": "i1", "name": "First & Main", "lat": 37.1, "lon": -121.1}

    with _client_with_handler(_Recorder([_json_response(response)])) as client:
        result = client.optimize_intersection("c1", intersection)

    if kept is None:
        assert set(result) == {"nema_plan", "kpis"}
    else:
        assert result["config"] == kept


# -- traffic model -------------------------------------------------------------


_SYNC_RESPONSE = {
    "nema_plan": {"schema": "terasim.nema_timing.v1"},
    "kpis": {
        "cost_weights": {"control_delay": 0.7, "stops": 0.3},
        "baseline": {"cost": 1.0, "metrics": {}},
        "optimized": {"cost": 0.5, "metrics": {}},
    },
}
_TARGET = {"intersection_id": "i1", "name": "First & Main", "lat": 37.1, "lon": -121.1}


def test_sync_optimization_sends_the_traffic_model_only_when_given():
    recorder = _Recorder([_json_response(_SYNC_RESPONSE)])

    with _client_with_handler(recorder) as client:
        client.optimize_intersection("c1", _TARGET, traffic_model="cnde")
        client.optimize_corridor("c1", _TARGET, {**_TARGET, "intersection_id": "i2"},
                                 traffic_model="cnde")
        client.optimize_intersection("c1", _TARGET)

    bodies = [json.loads(request.content) for request in recorder.requests]
    assert [body.get("traffic_model") for body in bodies] == ["cnde", "cnde", None]
    assert "traffic_model" not in bodies[2]


def test_submit_sends_the_traffic_model_only_when_given():
    recorder = _Recorder([_json_response({"run_id": "r1", "status": "submitted"})])

    with _client_with_handler(recorder) as client:
        client.request_signal_optimization(corridor_id="c1", traffic_model="cnde")
        client.request_signal_optimization(corridor_id="c1")

    bodies = [json.loads(request.content) for request in recorder.requests]
    assert bodies[0]["traffic_model"] == "cnde"
    assert "traffic_model" not in bodies[1]


@pytest.mark.parametrize("traffic_model", ["neural", "CNDE", ""])
def test_an_unknown_traffic_model_is_rejected_before_http(traffic_model):
    recorder = _Recorder([_json_response(_SYNC_RESPONSE)])

    with _client_with_handler(recorder) as client:
        with pytest.raises(ValueError, match="traffic_model"):
            client.optimize_intersection("c1", _TARGET, traffic_model=traffic_model)
        with pytest.raises(ValueError, match="traffic_model"):
            client.request_signal_optimization(corridor_id="c1", traffic_model=traffic_model)

    assert recorder.requests == []


@pytest.mark.parametrize(("sent", "kept"), [("cnde", "cnde"), (None, None), (7, None)])
def test_sync_optimization_passes_the_traffic_model_through(sent, kept):
    response = copy.deepcopy(_SYNC_RESPONSE)
    if sent is not None:
        response["traffic_model"] = sent

    with _client_with_handler(_Recorder([_json_response(response)])) as client:
        result = client.optimize_intersection("c1", _TARGET)

    if kept is None:
        assert set(result) == {"nema_plan", "kpis"}
    else:
        assert result["traffic_model"] == kept


def test_a_cnde_request_the_service_cannot_run_surfaces_its_reason():
    detail = (
        "invalid optimization config: traffic_model='cnde' is not available on this "
        "deployment: torch is not installed"
    )
    recorder = _Recorder([_json_response({"detail": detail}, status_code=422)])

    with _client_with_handler(recorder) as client:
        with pytest.raises(RuntimeError, match="HTTP 422") as excinfo:
            client.optimize_intersection("c1", _TARGET, traffic_model="cnde")

    assert "traffic_model='cnde' is not available on this deployment" in str(excinfo.value)
