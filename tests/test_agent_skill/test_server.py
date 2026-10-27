"""Public MCP surface for the synchronous VSS workflow."""

from __future__ import annotations

import asyncio
import inspect
import json
import time

import httpx
import pytest

from terasim_agent_skill import server
from terasim_agent_skill.client import CityOptimizationClient
from terasim_agent_skill.schemas import PlanPhaseInput

_OPTIMIZE_RESPONSE = {
    "nema_plan": {"schema": "terasim.nema_timing.v1"},
    "kpis": {
        "cost_weights": {"control_delay": 0.7, "stops": 0.3},
        "baseline": {"cost": 1.0, "metrics": {}},
        "optimized": {"cost": 0.8, "metrics": {}},
    },
}
# Intersection i1 of the demo corridor (signal J1), as the TMC registry lists it.
_TARGET = {
    "intersection_id": "i1",
    "name": "Main St & 1st Ave",
    "lat": 37.335,
    "lon": -121.90339,
}
_REGISTERED_RECORD = {
    **_TARGET,
    "corridor_id": "demo_arterial",
    "sensors": ["i1-cam1", "i1-cam2", "i1-cam3", "i1-cam4"],
    "registered_at": "2026-09-07T20:23:52Z",
}
_REGISTRY_REPLY = {
    "corridor_id": "demo_arterial",
    "observation_interval": "10m",
    "available": [
        {
            "intersection_id": "i1",
            "lat": 37.335,
            "lon": -121.90339,
            "sensors": ["i1-cam1", "i1-cam2", "i1-cam3", "i1-cam4"],
            "registered": True,
        }
    ],
    "registered": [_REGISTERED_RECORD],
}
# The shape of a completed optimize: export_nema_plan() rows plus the normalized
# kpis block from the client.
_FULL_OPTIMIZE_RESULT = {
    "nema_plan": {
        "schema": "terasim.nema_timing.v1",
        "generated_at": "2026-09-07T22:29:13.221974+00:00",
        "corridor_id": "demo_arterial",
        "handoff_ready": True,
        "coordination": {
            "cycle_length_s": 100.0,
            "reference_intersection": "J1",
        },
        "intersections": [
            {
                "tls_id": "J1",
                "pattern_id": "pm_peak",
                "description": "PM PEAK",
                "valid_windows": [{"days": ["Mon"], "start": "15:00", "end": "19:00"}],
                "offset_s": 81.0,
                "cycle_s": 100.0,
                "phases": [
                    {
                        "nema_phase": 1,
                        "movements": ["NB-left"],
                        "split_s": 11.0,
                        "green_s": 7.5,
                        "yellow_s": 3.0,
                        "all_red_s": 0.5,
                        "notes": "",
                    },
                    {
                        "nema_phase": 2,
                        "movements": ["WB-through", "WB-right"],
                        "split_s": 39.0,
                        "green_s": 35.5,
                        "yellow_s": 3.0,
                        "all_red_s": 0.5,
                        "notes": "",
                    },
                    {
                        "nema_phase": None,
                        "movements": ["ped-scramble"],
                        "split_s": 12.0,
                        "green_s": 9.0,
                        "yellow_s": 3.0,
                        "all_red_s": 0.0,
                        "notes": "",
                    },
                ],
            }
        ],
    },
    "kpis": {
        "cost_weights": {"control_delay": 0.7, "stops": 0.3},
        "baseline": {
            "cost": 1.0,
            "metrics": {
                "control_delay_s_per_veh": 32.818,
                "stops_per_veh": 0.6977,
                "vehicles_completed": 399,
            },
        },
        "optimized": {
            "cost": 0.9154440075818686,
            "metrics": {
                "control_delay_s_per_veh": 29.633,
                "stops_per_veh": 0.6591,
                "vehicles_completed": 401,
            },
        },
    },
}


def _assert_optimize_result(result, expected: dict = _OPTIMIZE_RESPONSE) -> None:
    """A tool result is two text blocks: the chat answer, then the client payload as JSON."""
    blocks = [block for block in result if getattr(block, "type", None) == "text"]
    assert len(blocks) == 2, result
    assert blocks[0].text.startswith("**Signal timing optimization: ")
    assert "| metric | baseline | optimized | change |" in blocks[0].text
    # without an artifact store the JSON block is the client's payload, unchanged
    assert json.loads(blocks[1].text) == expected


def _registry_client(reply: dict, seen: list | None = None):
    from terasim_agent_skill.tmc_client import TmcRegistryClient

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=reply)

    return TmcRegistryClient("http://tmc:8200", transport=httpx.MockTransport(handler))


class _FakeFastMCP:
    def __init__(self, name: str):
        self.name = name
        self.tools = {}

    def tool(self, **_options):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


def _resolve_schema_ref(root: dict, value: dict) -> dict:
    if "$ref" in value:
        name = value["$ref"].rsplit("/", 1)[-1]
        return root["$defs"][name]
    for option in value.get("anyOf", []):
        if option.get("type") != "null":
            return _resolve_schema_ref(root, option)
    return value


def test_sync_timeout_defaults_long_and_is_mcp_configurable(monkeypatch):
    assert inspect.signature(CityOptimizationClient).parameters["timeout_s"].default == 1800.0

    captured = {}

    class _FakeClient:
        def __init__(self, base_url: str, timeout_s: float):
            captured["base_url"] = base_url
            captured["timeout_s"] = timeout_s

    monkeypatch.setattr(server, "CityOptimizationClient", _FakeClient)
    monkeypatch.setenv("TERASIM_CITY_URL", "http://city.test")
    monkeypatch.setenv("TERASIM_CITY_TIMEOUT_S", "2400")

    client = server._make_client()

    assert isinstance(client, _FakeClient)
    assert captured == {"base_url": "http://city.test", "timeout_s": 2400.0}


def test_public_mcp_surface_is_sync_and_returns_plan_with_kpis():
    responses = [
        httpx.Response(202, json={"corridor_id": "c1", "buffered": 1}),
        httpx.Response(
            201,
            json={"stored": [["c1", "i1"]], "notes": ["c1/i1: new"]},
        ),
        httpx.Response(
            200,
            json={
                "nema_plan": {"schema": "terasim.nema_timing.v1"},
                "kpis": {
                    "cost_weights": {"control_delay": 0.7, "stops": 0.3},
                    "baseline": {"cost": 1.0, "metrics": {}},
                    "optimized": {"cost": 0.8, "metrics": {}},
                },
            },
        ),
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    client = CityOptimizationClient(
        "http://testserver", transport=httpx.MockTransport(handler)
    )
    app = server.create_mcp_app(client, fast_mcp_cls=_FakeFastMCP)

    assert set(app.tools) == {
        "upload_traffic_snapshot",
        "upload_signal_plan",
        "upload_map",
        "optimize_intersection",
        "optimize_intersection_by_id",
        "optimize_corridor",
    }
    for function in app.tools.values():
        parameters = inspect.signature(function).parameters
        assert "optimizer" not in parameters
        assert "max_trials" not in parameters

    snapshot = {
        "corridor_id": "c1",
        "timestamp_start": "2026-07-22T08:00:00-07:00",
        "observation_interval": "10m",
        "intersections": [
            {
                "intersection_id": "i1",
                "name": "First & Main",
                "lat": 37.1,
                "lon": -121.1,
                "counts": {"East": {"Straight": 10}},
            }
        ],
    }
    assert app.tools["upload_traffic_snapshot"](snapshot) == {
        "corridor_id": "c1",
        "buffered": 1,
    }
    plan_result = app.tools["upload_signal_plan"](
        {
            "schema": "terasim.nema_plan.v1",
            "corridor_id": "c1",
            "intersections": [
                {
                    "intersection_id": "i1",
                    "name": "First & Main",
                    "lat": 37.1,
                    "lon": -121.1,
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
                            "cycle_s": 90,
                            "splits": {"2": 90, "6": 90},
                        }
                    ],
                    "schedule": [],
                }
            ],
            "provenance": {
                "source_files": ["plan.pdf"],
                "handoff_ready": True,
                "gaps": [],
            },
        },
        "c1",
    )
    intersection = {
        "intersection_id": "i1",
        "name": "First & Main",
        "lat": 37.1,
        "lon": -121.1,
    }
    result = asyncio.run(app.tools["optimize_intersection"]("c1", intersection))

    assert plan_result == {
        "stored": [["c1", "i1"]],
        "notes": ["c1/i1: new"],
    }
    posted_plan = json.loads(requests[1].content)
    assert {
        key: posted_plan["intersections"][0][key] for key in intersection
    } == intersection
    assert posted_plan["intersections"][0]["rings"]["coordinated_phases"] == [2, 6]
    _assert_optimize_result(result)
    assert requests[2].url.path == "/v1/city/optimize"
    assert json.loads(requests[2].content) == {
        "type": "intersection",
        "corridor_id": "c1",
        "intersection": intersection,
    }
    assert set(inspect.signature(app.tools["optimize_intersection"]).parameters) == {
        "corridor_id",
        "intersection",
        "cost",
        "traffic_model",
        "ctx",
    }
    assert set(inspect.signature(app.tools["optimize_corridor"]).parameters) == {
        "corridor_id",
        "from_intersection",
        "to_intersection",
        "cost",
        "traffic_model",
        "ctx",
    }
    client.close()


def test_public_mcp_corridor_forwards_complete_endpoint_objects():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "nema_plan": {"schema": "terasim.nema_timing.v1"},
                "kpis": {
                    "cost_weights": {"control_delay": 0.7, "stops": 0.3},
                    "baseline": {"cost": 1.0, "metrics": {}},
                    "optimized": {"cost": 0.8, "metrics": {}},
                },
            },
        )

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
    client = CityOptimizationClient(
        "http://testserver", transport=httpx.MockTransport(handler)
    )
    app = server.create_mcp_app(client, fast_mcp_cls=_FakeFastMCP)

    asyncio.run(
        app.tools["optimize_corridor"](
            "c1",
            first,
            second,
            {"weights": {"control_delay": 0.8, "stops": 0.2}},
        )
    )

    assert json.loads(requests[0].content) == {
        "type": "corridor",
        "corridor_id": "c1",
        "corridor": {"from": first, "to": second},
        "cost": {"weights": {"control_delay": 0.8, "stops": 0.2}},
    }
    client.close()


def test_real_mcp_schema_describes_snapshot_plan_targets_and_cost():
    client = CityOptimizationClient("http://example.invalid")
    app = server.create_mcp_app(client)
    schemas = {
        tool.name: tool.inputSchema for tool in asyncio.run(app.list_tools())
    }

    snapshot_schema = schemas["upload_traffic_snapshot"]
    snapshot = _resolve_schema_ref(
        snapshot_schema, snapshot_schema["properties"]["snapshot"]
    )
    assert {
        "corridor_id",
        "timestamp_start",
        "observation_interval",
        "intersections",
    }.issubset(snapshot["required"])
    snapshot_intersection = _resolve_schema_ref(
        snapshot_schema, snapshot["properties"]["intersections"]["items"]
    )
    assert {"intersection_id", "name", "lat", "lon", "counts"}.issubset(
        snapshot_intersection["required"]
    )
    counts = _resolve_schema_ref(
        snapshot_schema, snapshot_intersection["properties"]["counts"]
    )
    assert set(counts["properties"]) == {"North", "South", "East", "West"}
    assert "anyOf" not in counts["properties"]["East"]
    east_turns = _resolve_schema_ref(
        snapshot_schema, counts["properties"]["East"]
    )
    assert set(east_turns["properties"]) == {"Left", "Straight", "Right"}
    assert "anyOf" not in east_turns["properties"]["Straight"]

    plan_schema = schemas["upload_signal_plan"]
    plan = _resolve_schema_ref(plan_schema, plan_schema["properties"]["plan"])
    assert {"schema", "intersections", "provenance"}.issubset(plan["required"])
    assert "corridor_id" not in plan["required"]
    plan_intersection = _resolve_schema_ref(
        plan_schema, plan["properties"]["intersections"]["items"]
    )
    assert {
        "intersection_id",
        "name",
        "lat",
        "lon",
        "phases",
        "rings",
        "patterns",
        "schedule",
    }.issubset(plan_intersection["required"])
    phase = _resolve_schema_ref(
        plan_schema, plan_intersection["properties"]["phases"]["items"]
    )
    assert "anyOf" not in phase["properties"]["yellow_s"]

    intersection_schema = schemas["optimize_intersection"]
    intersection = _resolve_schema_ref(
        intersection_schema,
        intersection_schema["properties"]["intersection"],
    )
    assert {"intersection_id", "name", "lat", "lon"}.issubset(
        intersection["required"]
    )
    assert intersection["properties"]["lat"]["minimum"] == -90
    assert intersection["properties"]["lat"]["maximum"] == 90
    assert intersection["properties"]["lon"]["minimum"] == -180
    assert intersection["properties"]["lon"]["maximum"] == 180

    cost = _resolve_schema_ref(
        intersection_schema, intersection_schema["properties"]["cost"]
    )
    assert "weights" in cost["required"]
    weights = _resolve_schema_ref(
        intersection_schema, cost["properties"]["weights"]
    )
    assert set(weights["properties"]) == {"control_delay", "stops"}
    assert weights["additionalProperties"] is False
    assert "anyOf" not in weights["properties"]["stops"]

    corridor_schema = schemas["optimize_corridor"]
    for field in ("from_intersection", "to_intersection"):
        endpoint = _resolve_schema_ref(
            corridor_schema, corridor_schema["properties"][field]
        )
        assert {"intersection_id", "name", "lat", "lon"}.issubset(
            endpoint["required"]
        )

    client.close()


def test_mcp_models_reject_scalar_coercion_before_http():
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    client = CityOptimizationClient(
        "http://testserver", transport=httpx.MockTransport(handler)
    )
    app = server.create_mcp_app(client, fast_mcp_cls=_FakeFastMCP)
    target = {
        "intersection_id": "i1",
        "name": "First & Main",
        "lat": "37.1",
        "lon": -121.1,
    }

    with pytest.raises(ValueError):
        asyncio.run(app.tools["optimize_intersection"]("c1", target))

    target["lat"] = 37.1
    with pytest.raises(ValueError):
        asyncio.run(
            app.tools["optimize_intersection"](
                "c1", target, {"weights": {"control_delay": True}}
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            app.tools["optimize_intersection"](
                "c1", target, {"weights": {"control_delay": "1.0"}}
            )
        )
    for invalid_weights in (
        {},
        {"unknown": 1.0},
        {"stops": 0.0},
        {"stops": None},
    ):
        with pytest.raises(ValueError):
            asyncio.run(
                app.tools["optimize_intersection"](
                    "c1", target, {"weights": invalid_weights}
                )
            )
    invalid_snapshot = {
        "corridor_id": "c1",
        "timestamp_start": "2026-07-22T08:00:00-07:00",
        "observation_interval": "10m",
        "intersections": [
            {
                "intersection_id": "i1",
                "name": "First & Main",
                "lat": 37.1,
                "lon": -121.1,
                "counts": {"East": {"Straight": -1}},
            }
        ],
    }
    with pytest.raises(ValueError):
        app.tools["upload_traffic_snapshot"](invalid_snapshot)
    invalid_snapshot["intersections"][0]["counts"] = {"East": None}
    with pytest.raises(ValueError):
        app.tools["upload_traffic_snapshot"](invalid_snapshot)
    invalid_snapshot["intersections"][0]["counts"] = {
        "East": {"Straight": None}
    }
    with pytest.raises(ValueError):
        app.tools["upload_traffic_snapshot"](invalid_snapshot)
    invalid_snapshot["intersections"][0]["counts"] = {
        "Southeast": {"Straight": 1}
    }
    with pytest.raises(ValueError):
        app.tools["upload_traffic_snapshot"](invalid_snapshot)
    with pytest.raises(ValueError):
        PlanPhaseInput.model_validate(
            {
                "phase": 2,
                "movements": ["EB-through"],
                "yellow_s": None,
            }
        )

    assert called is False
    client.close()


def test_plan_may_receive_corridor_id_from_tool_argument():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={"stored": [["c1", "i1"]], "notes": ["c1/i1: new"]},
        )

    plan = {
        "schema": "terasim.nema_plan.v1",
        "intersections": [
            {
                "intersection_id": "i1",
                "name": "First & Main",
                "lat": 37.1,
                "lon": -121.1,
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
                        "cycle_s": 90,
                        "splits": {"2": 90, "6": 90},
                    }
                ],
                "schedule": [],
            }
        ],
        "provenance": {
            "source_files": ["plan.pdf"],
            "handoff_ready": True,
            "gaps": [],
        },
    }
    client = CityOptimizationClient(
        "http://testserver", transport=httpx.MockTransport(handler)
    )
    app = server.create_mcp_app(client, fast_mcp_cls=_FakeFastMCP)

    result = app.tools["upload_signal_plan"](plan, "c1")

    assert result["stored"] == [["c1", "i1"]]
    assert json.loads(requests[0].content) == plan
    assert requests[0].url.params["corridor_id"] == "c1"
    client.close()


class _CapturingFastMCP:
    """Fake FastMCP that records constructor kwargs and run() transports."""

    def __init__(self, name: str, **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.tools = {}
        self.run_calls: list[str] = []

    def tool(self, **_options):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register

    def run(self, transport: str = "stdio", mount_path=None):
        self.run_calls.append(transport)


def _dead_client() -> CityOptimizationClient:
    return CityOptimizationClient(
        "http://testserver",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )


def test_transport_settings_default_to_stdio(monkeypatch):
    for name in ("TERASIM_MCP_TRANSPORT", "TERASIM_MCP_HOST", "TERASIM_MCP_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert server._transport_settings() == ("stdio", {})


def test_transport_settings_streamable_http_reads_host_and_port(monkeypatch):
    monkeypatch.setenv("TERASIM_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("TERASIM_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("TERASIM_MCP_PORT", "9911")
    monkeypatch.delenv("TERASIM_MCP_STATELESS", raising=False)
    assert server._transport_settings() == (
        "streamable-http",
        {"host": "0.0.0.0", "port": 9911, "stateless_http": False},
    )


def test_transport_settings_defaults_for_http_and_stateless_flag(monkeypatch):
    monkeypatch.setenv("TERASIM_MCP_TRANSPORT", "streamable-http")
    monkeypatch.delenv("TERASIM_MCP_HOST", raising=False)
    monkeypatch.delenv("TERASIM_MCP_PORT", raising=False)
    monkeypatch.setenv("TERASIM_MCP_STATELESS", "1")
    assert server._transport_settings() == (
        "streamable-http",
        {"host": "0.0.0.0", "port": server.DEFAULT_MCP_PORT, "stateless_http": True},
    )
    assert server.DEFAULT_MCP_PORT == 9911


@pytest.mark.parametrize(
    "env",
    [
        {"TERASIM_MCP_TRANSPORT": "websocket"},
        {"TERASIM_MCP_TRANSPORT": "streamable-http", "TERASIM_MCP_PORT": "abc"},
        {"TERASIM_MCP_TRANSPORT": "streamable-http", "TERASIM_MCP_PORT": "70000"},
    ],
)
def test_transport_settings_reject_bad_values(monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        server._transport_settings()


def test_create_mcp_app_forwards_settings_to_fastmcp():
    client = _dead_client()
    app = server.create_mcp_app(
        client, fast_mcp_cls=_CapturingFastMCP, settings={"host": "0.0.0.0", "port": 9911}
    )
    assert app.name == "terasim-city-optimization"
    assert app.kwargs == {"host": "0.0.0.0", "port": 9911}
    client.close()


def test_create_mcp_app_without_settings_passes_no_kwargs():
    client = _dead_client()
    app = server.create_mcp_app(client, fast_mcp_cls=_CapturingFastMCP)
    assert app.kwargs == {}
    client.close()


def test_main_runs_with_transport_from_environment(monkeypatch):
    monkeypatch.setenv("TERASIM_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("TERASIM_MCP_PORT", "9911")
    created = {}

    def fake_create(client, *, settings=None, **kwargs):
        app = _CapturingFastMCP("terasim-city-optimization", **(settings or {}))
        created["app"] = app
        return app

    monkeypatch.setattr(server, "create_mcp_app", fake_create)
    server.main()
    assert created["app"].run_calls == ["streamable-http"]
    assert created["app"].kwargs["port"] == 9911


def test_real_fastmcp_accepts_transport_settings():
    pytest.importorskip("mcp.server.fastmcp")
    client = _dead_client()
    app = server.create_mcp_app(client, settings={"host": "0.0.0.0", "port": 9911})
    assert (app.settings.host, app.settings.port, app.settings.streamable_http_path) == (
        "0.0.0.0",
        9911,
        "/mcp",
    )
    client.close()


def test_upload_map_tool_posts_path_to_maps_endpoint():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"map_id": "m1", "duplicate": False})

    client = CityOptimizationClient(
        "http://testserver", transport=httpx.MockTransport(handler)
    )
    app = server.create_mcp_app(client, fast_mcp_cls=_FakeFastMCP)
    assert app.tools["upload_map"]("/data/input/map/demo.sumocfg") == {
        "map_id": "m1",
        "duplicate": False,
    }
    assert requests[0].url.path == "/v1/city/maps"
    assert json.loads(requests[0].content) == {"path": "/data/input/map/demo.sumocfg"}
    assert set(inspect.signature(app.tools["upload_map"]).parameters) == {"path", "proj"}
    client.close()


def test_upload_map_tool_forwards_the_projection_override():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={"map_id": "m2", "duplicate": False, "source": "opendrive", "summary": {}},
        )

    client = CityOptimizationClient(
        "http://testserver", transport=httpx.MockTransport(handler)
    )
    app = server.create_mcp_app(client, fast_mcp_cls=_FakeFastMCP)
    proj = "+proj=utm +zone=10 +datum=WGS84 +units=m +no_defs"
    reply = app.tools["upload_map"]("/data/input/maps/corridor.xodr", proj=proj)
    assert reply["source"] == "opendrive"
    assert json.loads(requests[0].content) == {
        "path": "/data/input/maps/corridor.xodr",
        "proj": proj,
    }
    description = " ".join((app.tools["upload_map"].__doc__ or "").split())
    assert ".xodr (OpenDRIVE)" in description and "Lanelet2" in description
    client.close()


def test_registry_tools_only_present_with_tmc_client():
    client = _dead_client()
    without = server.create_mcp_app(client, fast_mcp_cls=_FakeFastMCP)
    assert "register_intersection" not in without.tools
    assert "list_intersections" not in without.tools
    assert "optimize_registered_intersection" not in without.tools

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"intersection_id": "i1", "sensors": ["i1-cam1"]})
        return httpx.Response(200, json={"available": [], "registered": []})

    from terasim_agent_skill.tmc_client import TmcRegistryClient

    tmc = TmcRegistryClient("http://tmc:8200", transport=httpx.MockTransport(handler))
    app = server.create_mcp_app(client, fast_mcp_cls=_FakeFastMCP, tmc_client=tmc)
    assert set(app.tools) == {
        "upload_traffic_snapshot",
        "upload_signal_plan",
        "upload_map",
        "optimize_intersection",
        "optimize_corridor",
        "optimize_intersection_by_id",
        "register_intersection",
        "list_intersections",
        "optimize_registered_intersection",
    }
    assert app.tools["register_intersection"]("i1", "Main", 37.335, -121.90339) == {
        "intersection_id": "i1",
        "sensors": ["i1-cam1"],
    }
    assert json.loads(seen[0].content)["name"] == "Main"
    assert app.tools["list_intersections"]() == {"available": [], "registered": []}
    assert set(inspect.signature(app.tools["register_intersection"]).parameters) == {
        "intersection_id", "name", "lat", "lon", "corridor_id",
    }
    tmc.close()
    client.close()


def test_optimize_registered_intersection_resolves_the_record_by_id():
    posted: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        return httpx.Response(200, json=_OPTIMIZE_RESPONSE)

    seen: list[httpx.Request] = []
    client = CityOptimizationClient(
        "http://testserver", transport=httpx.MockTransport(handler)
    )
    tmc = _registry_client(_REGISTRY_REPLY, seen)
    app = server.create_mcp_app(client, fast_mcp_cls=_FakeFastMCP, tmc_client=tmc)
    tool = app.tools["optimize_registered_intersection"]

    result = asyncio.run(tool("i1"))

    _assert_optimize_result(result)
    assert (seen[0].method, seen[0].url.path) == ("GET", "/v1/tmc/intersections")
    assert posted[0].url.path == "/v1/city/optimize"
    assert json.loads(posted[0].content) == {
        "type": "intersection",
        "corridor_id": "demo_arterial",
        "intersection": _TARGET,
    }
    assert set(inspect.signature(tool).parameters) == {
        "intersection_id", "cost", "traffic_model", "ctx"
    }
    description = " ".join((tool.__doc__ or "").split())
    assert "Prefer this over optimize_intersection" in description

    asyncio.run(tool("i1", {"weights": {"control_delay": 0.8, "stops": 0.2}}))
    assert json.loads(posted[1].content)["cost"] == {
        "weights": {"control_delay": 0.8, "stops": 0.2}
    }
    tmc.close()
    client.close()


def test_optimize_registered_intersection_rejects_an_unregistered_id():
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_OPTIMIZE_RESPONSE)

    client = CityOptimizationClient(
        "http://testserver", transport=httpx.MockTransport(handler)
    )
    tmc = _registry_client(_REGISTRY_REPLY)
    app = server.create_mcp_app(client, fast_mcp_cls=_FakeFastMCP, tmc_client=tmc)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(app.tools["optimize_registered_intersection"]("first_and_main"))

    message = str(excinfo.value)
    assert "first_and_main" in message
    assert "(registered ids: i1)" in message
    assert "list_intersections" in message
    assert "register_intersection" in message
    assert called is False
    tmc.close()
    client.close()


def test_optimize_intersection_by_id_takes_flat_scalar_fields():
    posted: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        return httpx.Response(200, json=_OPTIMIZE_RESPONSE)

    client = CityOptimizationClient(
        "http://testserver", transport=httpx.MockTransport(handler)
    )
    app = server.create_mcp_app(client, fast_mcp_cls=_FakeFastMCP)

    result = asyncio.run(
        app.tools["optimize_intersection_by_id"](
            "demo_arterial", "i1", "Main St & 1st Ave", 37.335, -121.90339
        )
    )

    _assert_optimize_result(result)
    assert json.loads(posted[0].content) == {
        "type": "intersection",
        "corridor_id": "demo_arterial",
        "intersection": _TARGET,
    }
    assert set(inspect.signature(app.tools["optimize_intersection_by_id"]).parameters) == {
        "corridor_id",
        "intersection_id",
        "name",
        "lat",
        "lon",
        "cost",
        "traffic_model",
        "ctx",
    }
    client.close()


def test_summarize_optimization_renders_the_kpis_and_every_phase_row():
    # A small planner model condenses the nested tool JSON and drops the splits, so
    # the answer carries a pre-rendered block it can paste.
    summary = server.summarize_optimization(
        _FULL_OPTIMIZE_RESULT, intersections=[{"name": "Main St & 1st Ave"}]
    )
    lines = summary.splitlines()

    assert lines[0] == "**Signal timing optimization: Main St & 1st Ave**"
    assert "Plan: PM PEAK coordination pattern, cycle 100 s, offset 81 s." in summary
    assert "Hand-off ready: yes." in summary
    assert "**KPIs (baseline -> optimized)**" in summary
    assert "| control delay (s/veh) | 32.82 | 29.63 | -9.7% |" in summary
    assert "| stops per vehicle | 0.70 | 0.66 | -5.5% |" in summary
    assert "| weighted cost (delay 0.7, stops 0.3) | 1.00 | 0.92 | -8.5% |" in summary
    assert "**NEMA splits (optimized): Main St & 1st Ave**" in summary
    assert "| NEMA phase | movements | split (s) | green (s) |" in summary
    assert "| 1 | NB-left | 11.00 | 7.50 |" in summary
    assert "| 2 | WB-through, WB-right | 39.00 | 35.50 |" in summary
    # a phase the source plan could not number renders as "-", never as "None"
    assert "| - | ped-scramble | 12.00 | 9.00 |" in summary
    assert "None" not in summary
    assert len(lines) <= 40, f"one intersection rendered {len(lines)} lines"


def test_summarize_optimization_compares_the_stored_pattern_splits():
    # With the city's plan (as the artifact store reads it) the block says what
    # changed: current vs optimized split per NEMA phase and the offset move.
    # The stored plan is synthetic: its midday pattern runs a 100 s cycle at offset
    # 10 s with phase 2 at 46 s and phase 4 at 31 s.
    from pathlib import Path

    plan_path = Path(__file__).resolve().parent / "data" / "demo_arterial_i1.nema_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    result = json.loads(json.dumps(_FULL_OPTIMIZE_RESULT))
    entry = result["nema_plan"]["intersections"][0]
    entry["pattern_id"] = "midday"
    entry["offset_s"] = 12.0
    entry["phases"] = [
        {"nema_phase": 2, "movements": ["WB-through", "WB-right"], "split_s": 39.0, "green_s": 35.5},
        {"nema_phase": 4, "movements": ["NB-through"], "split_s": 27.0, "green_s": 23.0},
    ]
    target = {"intersection_id": "i1", "name": "Main St & 1st Ave"}

    summary = server.summarize_optimization(result, intersections=[target], current_plan=plan)

    assert (
        "Plan: MIDDAY coordination pattern, cycle 100 s, offset 12 s (was 10 s); "
        "the 2 NEMA phase splits were re-balanced (table below)."
    ) in summary
    assert "**KPIs (current plan -> optimized)**" in summary
    assert "| metric | current plan | optimized | change |" in summary
    assert "**NEMA splits (current -> optimized): Main St & 1st Ave**" in summary
    assert (
        "| NEMA phase | movements | current split (s) | optimized split (s) "
        "| change (s) | green (s) |"
    ) in summary
    assert "| 2 | WB-through, WB-right | 46.00 | 39.00 | -7.00 | 35.50 |" in summary
    assert "| 4 | NB-through | 31.00 | 27.00 | -4.00 | 23.00 |" in summary
    # a generated seed (no pattern id) never pretends to compare against the store
    result["nema_plan"]["intersections"][0].pop("pattern_id")
    plain = server.summarize_optimization(result, intersections=[target], current_plan=plan)
    assert "demand-timed 2-phase plan (no coordination pattern)" in plain
    assert "current split" not in plain and "baseline -> optimized" in plain


def test_summarize_optimization_says_when_the_current_plan_stays_best():
    # When no candidate beats the city's plan, the exported plan is that plan: the
    # sentence must not claim the splits were re-balanced.
    from pathlib import Path

    plan_path = Path(__file__).resolve().parent / "data" / "demo_arterial_i1.nema_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    result = json.loads(json.dumps(_FULL_OPTIMIZE_RESULT))
    entry = result["nema_plan"]["intersections"][0]
    entry["pattern_id"] = "midday"
    entry["offset_s"] = 10.0
    entry["phases"] = [
        {"nema_phase": 2, "movements": ["WB-through", "WB-right"], "split_s": 46.0, "green_s": 42.5},
        {"nema_phase": 4, "movements": ["NB-through"], "split_s": 31.0, "green_s": 27.0},
    ]
    target = {"intersection_id": "i1", "name": "Main St & 1st Ave"}

    summary = server.summarize_optimization(result, intersections=[target], current_plan=plan)

    assert (
        "Plan: MIDDAY coordination pattern, cycle 100 s, offset 10 s (unchanged); the current "
        "plan is still the best plan found, its 2 NEMA phase splits are unchanged (table below)."
    ) in summary
    assert "re-balanced" not in summary
    assert "| 2 | WB-through, WB-right | 46.00 | 46.00 | +0.00 | 42.50 |" in summary
    # same splits at another offset: the splits are unchanged, the plan is not the current one
    entry["offset_s"] = 20.0
    moved = server.summarize_optimization(result, intersections=[target], current_plan=plan)
    assert "offset 20 s (was 10 s); the 2 NEMA phase splits are unchanged (table below)." in moved
    assert "re-balanced" not in moved and "still the best plan" not in moved


def test_summarize_optimization_never_raises_on_a_missing_or_odd_result():
    for result in ({}, {"nema_plan": None, "kpis": []}, {"nema_plan": {"intersections": [{}]}}):
        summary = server.summarize_optimization(result)
        assert isinstance(summary, str)
        assert "n/a" in summary
        assert "| cost score |" in summary


def test_summary_includes_junction_delay_and_cycle_change():
    result = {
        "nema_plan": {
            "corridor_id": "c1",
            "handoff_ready": False,
            "coordination": {"cycle_length_s": 164.0},
            "intersections": [{"tls_id": "n34", "offset_s": 0.0, "phases": []}],
        },
        "kpis": {
            "cost_weights": {"control_delay": 0.7, "stops": 0.3},
            "baseline": {
                "cost": 1.0,
                "timing": {"cycle_s": {"n34": 140.0}},
                "metrics": {
                    "control_delay_s_per_veh": 267.1,
                    "stops_per_veh": 1.37,
                    "vehicles_total": 6375,
                    "corridor_junction_delay_s": 47.0,
                    "junction_delay_s": {"n34": 47.0, "n16": 36.9},
                },
            },
            "optimized": {
                "cost": 0.95,
                "timing": {"cycle_s": {"n34": 164.0}},
                "metrics": {
                    "control_delay_s_per_veh": 257.8,
                    "stops_per_veh": 1.38,
                    "vehicles_total": 6375,
                    "corridor_junction_delay_s": 36.0,
                    "junction_delay_s": {"n34": 27.4, "n16": 44.6},
                },
            },
        },
    }
    text = server.summarize_optimization(result)
    assert "per-junction delay" in text.lower()
    assert "| n34 | 47.0 | 27.4 |" in text and "| n16 | 36.9 | 44.6 |" in text
    assert "140 s -> 164 s" in text


def test_summary_tolerates_missing_junction_metrics():
    result = {
        "nema_plan": {"intersections": []},
        "kpis": {
            "baseline": {"cost": 1.0, "metrics": {}},
            "optimized": {"cost": 1.0, "metrics": {}},
        },
    }
    assert "n/a" in server.summarize_optimization(result)


def _junction_result() -> dict:
    """A corridor result whose sides carry ``timing`` and the edge-based junction delay."""
    return {
        "nema_plan": {
            "corridor_id": "c1",
            "handoff_ready": False,
            "intersections": [{"tls_id": "n34", "pattern_id": "am", "offset_s": 0.0, "phases": []}],
        },
        "kpis": {
            "cost_weights": {"control_delay": 0.7, "stops": 0.3},
            "baseline": {
                "cost": 1.0,
                "timing": {"cycle_s": {"n34": 140.0}},
                "metrics": {
                    "control_delay_s_per_veh": 267.1,
                    "stops_per_veh": 1.37,
                    "vehicles_total": 6375,
                    "corridor_junction_delay_s": 47.0,
                    "junction_delay_s": {"n34": 47.0, "n16": 36.9},
                },
            },
            "optimized": {
                "cost": 0.95,
                "timing": {"cycle_s": {"n34": 164.0}},
                "metrics": {
                    "control_delay_s_per_veh": 257.8,
                    "stops_per_veh": 1.38,
                    "vehicles_total": 6375,
                    "corridor_junction_delay_s": 36.0,
                    "junction_delay_s": {"n34": 27.4, "n16": 44.6},
                },
            },
        },
    }


def test_summary_puts_the_corridor_junction_delay_in_the_kpi_table():
    text = server.summarize_optimization(_junction_result())

    assert (
        "| stops per vehicle | 1.37 | 1.38 | +0.7% |\n"
        "| per-junction delay, edge-based (s/veh) | 47.00 | 36.00 | -23.4% |\n"
        "| weighted cost (delay 0.7, stops 0.3) | 1.00 | 0.95 | -5.0% |"
    ) in text


def test_summary_lists_the_per_signal_junction_delay_between_kpis_and_splits():
    result = _junction_result()
    text = server.summarize_optimization(result)

    assert (
        "**Per-junction delay (edge-based, s/veh)**\n"
        "\n"
        "| signal | baseline | optimized |\n"
        "| --- | --- | --- |\n"
        "| n16 | 36.9 | 44.6 |\n"
        "| n34 | 47.0 | 27.4 |"
    ) in text
    assert (
        text.index("**KPIs (baseline -> optimized)**")
        < text.index("**Per-junction delay (edge-based, s/veh)**")
        < text.index("**NEMA splits (")
    )
    # with a stored plan to compare against, the column carries the KPI table's label
    current_plan = {
        "intersections": [
            {"tls_id": "n34", "patterns": [{"pattern_id": "am", "offset_s": 0.0, "splits": {}}]}
        ]
    }
    compared = server.summarize_optimization(result, current_plan=current_plan)
    assert "| signal | current plan | optimized |" in compared


def test_summary_marks_missing_junction_values_as_not_available():
    result = _junction_result()
    metrics = result["kpis"]["baseline"]["metrics"]
    metrics["corridor_junction_delay_s"] = None
    metrics["junction_delay_s"] = {"n34": 47.0, "n16": None}
    result["kpis"]["optimized"]["metrics"]["junction_delay_s"] = {"n34": 27.4, "n69": 12.0}

    text = server.summarize_optimization(result)

    assert "| per-junction delay, edge-based (s/veh) | n/a | 36.00 | n/a |" in text
    # a signal only one side measured, or one that had no data, is n/a - never "None"
    assert "| n16 | n/a | n/a |" in text
    assert "| n34 | 47.0 | 27.4 |" in text
    assert "| n69 | n/a | 12.0 |" in text
    assert "None" not in text


def test_summary_without_the_new_fields_adds_no_table_and_no_cycle_sentence():
    text = server.summarize_optimization(
        _FULL_OPTIMIZE_RESULT, intersections=[{"name": "Main St & 1st Ave"}]
    )

    assert "| control delay (s/veh) | 32.82 | 29.63 | -9.7% |" in text
    assert "| stops per vehicle | 0.70 | 0.66 | -5.5% |" in text
    assert "| per-junction delay, edge-based (s/veh) | n/a | n/a | n/a |" in text
    assert "| weighted cost (delay 0.7, stops 0.3) | 1.00 | 0.92 | -8.5% |" in text
    assert "Per-junction delay (edge-based" not in text
    assert "Cycle unchanged" not in text and " s -> " not in text
    assert len(text.splitlines()) <= 40


_MIXED_CYCLES = {"n16": 280.0, "n34": 140.0, "n69": 60.0}
_COMMON_164 = {"n16": 164.0, "n34": 164.0, "n69": 164.0}


@pytest.mark.parametrize(
    ("before", "after", "sentence"),
    [
        ({"n34": 140.0}, {"n34": 164.0}, "Cycle 140 s -> 164 s."),
        ({"n34": 140.0}, {"n34": 140.0}, "Cycle unchanged at 140 s."),
        # under half a second apart is the same cycle
        ({"n34": 140.0}, {"n34": 140.4}, "Cycle unchanged at 140 s."),
        ({"n34": 140.0}, {"n34": 140.5}, "Cycle 140 s -> 140.5 s."),
        # unusable entries are ignored
        (
            {"n34": 140.0, "n2": "x", "n9": True, "n7": float("nan")},
            {"n34": 164.0, "n16": 164.0},
            "Cycle 140 s -> 164 s.",
        ),
        # signals within half a second of each other share one cycle (the longest shows)
        ({"n34": 140.0, "n16": 140.5}, {"n34": 164.0}, "Cycle 140.5 s -> 164 s."),
        # signals further apart run different cycles: a side shows as a range, not its longest
        pytest.param(
            _MIXED_CYCLES, _COMMON_164, "Cycle 60–280 s (per signal) -> 164 s.",
            id="mixed_baseline_common_optimized",
        ),
        (
            {"n34": 140.0, "n16": 140.6}, {"n34": 164.0},
            "Cycle 140–140.6 s (per signal) -> 164 s.",
        ),
        (_COMMON_164, _MIXED_CYCLES, "Cycle 164 s -> 60–280 s (per signal)."),
        (
            _MIXED_CYCLES, {"n16": 200.0, "n34": 150.0, "n69": 90.0},
            "Cycles 60–280 s -> 90–200 s (per signal).",
        ),
        pytest.param(
            _MIXED_CYCLES, dict(_MIXED_CYCLES), "Cycles unchanged (60–280 s per signal).",
            id="both_mixed_equal",
        ),
        # both ends of the two ranges within half a second: unchanged
        (
            _MIXED_CYCLES, {"n16": 280.3, "n34": 140.0, "n69": 59.8},
            "Cycles unchanged (60–280 s per signal).",
        ),
    ],
)
def test_summary_states_the_cycle_change_in_the_facts_line(before, after, sentence):
    result = _junction_result()
    result["kpis"]["baseline"]["timing"] = {"cycle_s": before}
    result["kpis"]["optimized"]["timing"] = {"cycle_s": after}

    text = server.summarize_optimization(result)

    head = text.split("**KPIs")[0]
    assert sentence in head
    demand, handoff = head.index("Demand: 6375 vehicles"), head.index("Hand-off ready: no.")
    assert demand < head.index(sentence) < handoff


@pytest.mark.parametrize(
    ("baseline_timing", "optimized_timing"),
    [
        (None, {"cycle_s": {"n34": 164.0}}),
        ({"cycle_s": {"n34": 140.0}}, None),
        ({"cycle_s": {}}, {"cycle_s": {"n34": 164.0}}),
        ({"cycle_s": {"n34": 140.0}}, {"offsets_s": {"n34": 0.0}}),
        ({"cycle_s": {"n34": "140"}}, {"cycle_s": {"n34": 164.0}}),
        ([], {"cycle_s": ["n34"]}),
    ],
)
def test_summary_says_nothing_about_the_cycle_unless_both_sides_report_one(
    baseline_timing, optimized_timing
):
    result = _junction_result()
    result["kpis"]["baseline"]["timing"] = baseline_timing
    result["kpis"]["optimized"]["timing"] = optimized_timing

    text = server.summarize_optimization(result)

    assert "Cycle unchanged" not in text and " s -> " not in text


def test_summary_never_raises_on_odd_timing_and_junction_shapes():
    odd_sides = [
        {"timing": []},
        {"timing": {"cycle_s": ["x"]}},
        {"timing": {"cycle_s": {"n34": float("inf")}}},
        {"metrics": {"junction_delay_s": [1, 2], "corridor_junction_delay_s": "47"}},
        {"metrics": {"junction_delay_s": {1: 5.0, "n34": float("nan")}}},
    ]
    for side in odd_sides:
        result = {
            "nema_plan": {"intersections": [{}]},
            "kpis": {"baseline": {"cost": 1.0, **side}, "optimized": {"cost": 1.0, **side}},
        }
        text = server.summarize_optimization(result)
        assert isinstance(text, str)
        assert "None" not in text


def test_optimize_tools_add_the_summary_without_mutating_the_client_payload():
    payload = json.loads(json.dumps(_FULL_OPTIMIZE_RESULT))

    class _StubClient:
        def optimize_intersection(
            self, corridor_id, intersection, *, cost=None, traffic_model=None
        ):
            return payload

        def optimize_corridor(self, corridor_id, first, second, *, cost=None, traffic_model=None):
            return payload

    app = server.create_mcp_app(_StubClient(), fast_mcp_cls=_FakeFastMCP)
    target = dict(_TARGET)

    results = [
        asyncio.run(app.tools["optimize_intersection"]("demo_arterial", target)),
        asyncio.run(
            app.tools["optimize_intersection_by_id"](
                "demo_arterial", "i1", "Main St & 1st Ave", 37.335, -121.90339
            )
        ),
        asyncio.run(app.tools["optimize_corridor"]("demo_arterial", target, target)),
    ]

    assert set(payload) == {"nema_plan", "kpis"}, "the client's dict was mutated in place"
    for result in results:
        _assert_optimize_result(result, _FULL_OPTIMIZE_RESULT)
        assert "| 2 | WB-through, WB-right | 39.00 | 35.50 |" in result[0].text


def test_optimize_tool_docstrings_point_the_agent_at_the_summary():
    client = CityOptimizationClient(
        "http://testserver", transport=httpx.MockTransport(lambda _r: httpx.Response(200))
    )
    tmc = _registry_client(_REGISTRY_REPLY)
    app = server.create_mcp_app(client, fast_mcp_cls=_FakeFastMCP, tmc_client=tmc)

    for name in (
        "optimize_intersection",
        "optimize_corridor",
        "optimize_intersection_by_id",
        "optimize_registered_intersection",
    ):
        description = " ".join((app.tools[name].__doc__ or "").split())
        assert "ready-to-show markdown block" in description, name
        assert "present that block verbatim" in description, name
    tmc.close()
    client.close()


def test_heartbeat_defaults_to_two_seconds(monkeypatch):
    # NAT's MCP client runs with httpx's 5 s default read timeout: the heartbeat
    # interval stays well under it.
    monkeypatch.delenv("TERASIM_MCP_HEARTBEAT_S", raising=False)
    assert server.HEARTBEAT_S == 2.0
    assert server._heartbeat_interval() == 2.0


def test_heartbeat_interval_reads_the_environment(monkeypatch):
    monkeypatch.delenv("TERASIM_MCP_HEARTBEAT_S", raising=False)
    assert server._heartbeat_interval() == server.HEARTBEAT_S == 2.0
    monkeypatch.setenv("TERASIM_MCP_HEARTBEAT_S", "0.25")
    assert server._heartbeat_interval() == 0.25
    for invalid in ("abc", "0", "-1"):
        monkeypatch.setenv("TERASIM_MCP_HEARTBEAT_S", invalid)
        with pytest.raises(ValueError):
            server._heartbeat_interval()


def test_heartbeat_survives_a_missing_or_broken_context():
    class _BrokenContext:
        async def report_progress(self, progress, total=None, message=None):
            raise RuntimeError("notification dropped")

        async def info(self, message):
            raise RuntimeError("log dropped")

    class _ProgressOnlyContext:
        def __init__(self):
            self.messages = []

        async def report_progress(self, progress, total=None, message=None):
            self.messages.append(message)

    def work(value: str) -> str:
        time.sleep(0.05)
        return value

    def boom() -> None:
        raise RuntimeError("upstream refused")

    for ctx in (None, object(), _BrokenContext(), _ProgressOnlyContext()):
        assert asyncio.run(server._run_with_heartbeat(ctx, work, "done")) == "done"
        with pytest.raises(RuntimeError, match="upstream refused"):
            asyncio.run(server._run_with_heartbeat(ctx, boom))


def test_heartbeat_logs_without_a_progress_token(monkeypatch):
    """A client that sends no progressToken must still receive bytes."""
    monkeypatch.setenv("TERASIM_MCP_HEARTBEAT_S", "0.05")
    logged: list[str] = []

    class _LogOnlyContext:
        async def info(self, message):
            logged.append(message)

    def work() -> str:
        time.sleep(0.3)
        return "done"

    assert asyncio.run(server._run_with_heartbeat(_LogOnlyContext(), work)) == "done"
    assert logged, "expected at least one log notification"
    assert all("optimizing" in message for message in logged)


def test_real_fastmcp_hides_ctx_and_streams_progress(monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")
    from mcp.shared.memory import create_connected_server_and_client_session

    monkeypatch.setenv("TERASIM_MCP_HEARTBEAT_S", "0.1")

    def handler(_request: httpx.Request) -> httpx.Response:
        time.sleep(0.3)
        return httpx.Response(200, json=_OPTIMIZE_RESPONSE)

    client = CityOptimizationClient(
        "http://testserver", transport=httpx.MockTransport(handler)
    )
    tmc = _registry_client(_REGISTRY_REPLY)
    app = server.create_mcp_app(client, tmc_client=tmc)
    progress: list[tuple] = []
    logs: list[tuple] = []

    async def on_progress(value, total, message):
        progress.append((value, total, message))

    async def on_log(params):
        logs.append((params.level, params.data))

    async def scenario():
        async with create_connected_server_and_client_session(
            app, logging_callback=on_log
        ) as session:
            schemas = {
                tool.name: tool.inputSchema for tool in (await session.list_tools()).tools
            }
            for name in (
                "optimize_intersection",
                "optimize_corridor",
                "optimize_intersection_by_id",
                "optimize_registered_intersection",
            ):
                assert "ctx" not in schemas[name]["properties"], name
                assert "ctx" not in schemas[name].get("required", []), name
            assert set(schemas["optimize_registered_intersection"]["required"]) == {
                "intersection_id"
            }
            flat = await session.call_tool(
                "optimize_intersection_by_id",
                {"corridor_id": "demo_arterial", **_TARGET},
            )
            _assert_optimize_result(flat.content)
            return await session.call_tool(
                "optimize_registered_intersection",
                {"intersection_id": "i1"},
                progress_callback=on_progress,
            )

    result = asyncio.run(scenario())

    assert result.isError is False
    _assert_optimize_result(result.content)
    assert result.structuredContent is None, "content blocks must not be wrapped in {'result': ...}"
    assert progress, "expected at least one MCP progress notification"
    assert all(message and "optimizing" in message for _, _, message in progress)
    assert [value for value, _, _ in progress] == sorted(
        value for value, _, _ in progress
    )
    # notifications/message needs no progressToken, so it also covers the first
    # (token-less) call above -- NAT never sends one.
    assert logs, "expected at least one MCP logging notification"
    assert all(level == "info" for level, _ in logs)
    assert all("optimizing" in str(data) for _, data in logs)
    tmc.close()
    client.close()


def test_main_builds_tmc_client_from_environment(monkeypatch):
    monkeypatch.setenv("TERASIM_TMC_URL", "http://tmc:8200")
    monkeypatch.delenv("TERASIM_MCP_TRANSPORT", raising=False)
    captured = {}

    def fake_create(client, *, settings=None, tmc_client=None, **kwargs):
        captured["tmc_client"] = tmc_client
        return _CapturingFastMCP("terasim-city-optimization")

    monkeypatch.setattr(server, "create_mcp_app", fake_create)
    server.main()
    from terasim_agent_skill.tmc_client import TmcRegistryClient

    assert isinstance(captured["tmc_client"], TmcRegistryClient)


def test_summary_names_the_movements_the_plan_never_serves():
    result = _junction_result()
    result["unserved_movements"] = {
        "n69": ["WB-left", "WB-through", "WB-right"],
        "n34": ["EB-left"],
    }

    head = server.summarize_optimization(result).split("**KPIs")[0]

    sentence = "Unserved movements: n34 EB-left; n69 WB-left, WB-through, WB-right."
    assert sentence in head
    assert head.index("Demand: 6375 vehicles") < head.index(sentence) < head.index("Hand-off ready")


@pytest.mark.parametrize("unserved", [None, {}, {"n34": []}, ["n34"], {"n34": "EB-left"}])
def test_summary_says_nothing_about_unserved_movements_without_any(unserved):
    result = _junction_result()
    if unserved is not None:
        result["unserved_movements"] = unserved

    assert "Unserved" not in server.summarize_optimization(result)


def _static_corridor_result() -> dict:
    """A static corridor plan's export (no pattern ids, no phase numbers), 3 signals."""
    result = _junction_result()
    stage = {"nema_phase": None, "movements": ["EB-through"], "split_s": 60.0, "green_s": 57.0}
    result["nema_plan"] = {
        "handoff_ready": False,
        "coordination": {"cycle_length_s": 135.0, "reference_intersection": "J1"},
        "intersections": [
            {"tls_id": tls_id, "offset_s": offset, "phases": [stage, stage]}
            for tls_id, offset in (("J1", 0.0), ("J2", 34.0), ("J3", 26.0))
        ],
    }
    return result


def test_summary_calls_a_static_corridor_plan_coordinated():
    text = server.summarize_optimization(_static_corridor_result())

    assert "no coordination pattern" not in text
    assert "Plan: coordinated 2-phase corridor plan (3 signals), cycle 135 s, offset 34 s." in text


@pytest.mark.parametrize(
    ("config", "origin"),
    [
        ({"flow_source": "scenario_routes", "counts_source": "scenario_routes"},
         "the scenario's own route files"),
        ({"flow_source": "scenario_routes", "counts_source": "tmc"},
         "the scenario's own route files"),
        ({"flow_source": "corridor_graph", "counts_source": "scenario_routes"},
         "the scenario's own route files"),
        ({"flow_source": "corridor_graph", "counts_source": "tmc"},
         "the measured traffic counts"),
        (None, "the measured traffic counts"),
        ("scenario_routes", "the measured traffic counts"),
    ],
    ids=["both", "flows", "counts", "defaults", "absent", "not_a_dict"],
)
def test_summary_says_where_the_demand_came_from(config, origin):
    result = _junction_result()
    if config is not None:
        result["config"] = config

    text = server.summarize_optimization(result)

    assert f"Demand: 6375 vehicles simulated from {origin}." in text


# -- traffic model ---------------------------------------------------------------

_OPTIMIZE_TOOLS = (
    "optimize_intersection",
    "optimize_corridor",
    "optimize_intersection_by_id",
    "optimize_registered_intersection",
)
# The phrasings that should make the agent ask for CNDE traffic, and nothing else.
_CNDE_TRIGGERS = (
    "CNDE",
    "Neural NDE",
    "neural traffic",
    "learned / naturalistic driver behavior",
)


def test_every_optimize_tool_offers_an_optional_traffic_model():
    pytest.importorskip("mcp.server.fastmcp")
    client = CityOptimizationClient("http://example.invalid")
    tmc = _registry_client(_REGISTRY_REPLY)
    app = server.create_mcp_app(client, tmc_client=tmc)
    tools = {tool.name: tool for tool in asyncio.run(app.list_tools())}

    for name in _OPTIMIZE_TOOLS:
        schema = tools[name].inputSchema
        field = schema["properties"]["traffic_model"]
        assert "traffic_model" not in schema.get("required", []), name
        assert field["default"] is None, name
        assert {"enum": ["sumo", "cnde"], "type": "string"} in field["anyOf"], name
        for text in (field["description"], " ".join(tools[name].description.split())):
            # When to set it, that ordinary requests leave it unset, and the cost.
            for trigger in _CNDE_TRIGGERS:
                assert trigger in text, (name, trigger)
            assert "unset" in text and '"cnde"' in text and "GPU" in text, name
            assert "longer" in text or "slower" in text, name
    tmc.close()
    client.close()


def test_optimize_tools_forward_the_traffic_model_only_when_set():
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json=_OPTIMIZE_RESPONSE)

    client = CityOptimizationClient(
        "http://testserver", transport=httpx.MockTransport(handler)
    )
    tmc = _registry_client(_REGISTRY_REPLY)
    app = server.create_mcp_app(client, fast_mcp_cls=_FakeFastMCP, tmc_client=tmc)
    target = {key: _REGISTERED_RECORD[key] for key in ("intersection_id", "name", "lat", "lon")}

    calls = [
        app.tools["optimize_intersection"]("demo_arterial", target, traffic_model="cnde"),
        app.tools["optimize_corridor"](
            "demo_arterial", target, {**target, "intersection_id": "i2"},
            traffic_model="cnde",
        ),
        app.tools["optimize_intersection_by_id"](
            "demo_arterial", "i1", "Main St & 1st Ave", 37.335, -121.90339,
            traffic_model="cnde",
        ),
        app.tools["optimize_registered_intersection"]("i1", traffic_model="cnde"),
        app.tools["optimize_registered_intersection"]("i1"),
    ]
    for call in calls:
        asyncio.run(call)

    assert [body.get("traffic_model") for body in posted] == ["cnde"] * 4 + [None]
    assert "traffic_model" not in posted[-1]
    tmc.close()
    client.close()


def test_summary_says_when_cnde_traffic_simulated_the_plans():
    result = _junction_result()
    result["traffic_model"] = "cnde"

    head = server.summarize_optimization(result).split("**KPIs")[0]

    sentence = (
        "Traffic model: CNDE (Neural NDE, learned driver behavior) simulated every "
        "candidate plan."
    )
    assert sentence in head
    assert head.index("Demand: 6375 vehicles") < head.index(sentence) < head.index("Cycle 140")


@pytest.mark.parametrize("traffic_model", [None, "sumo"])
def test_summary_of_a_default_run_names_no_traffic_model(traffic_model):
    result = _junction_result()
    if traffic_model is not None:
        result["traffic_model"] = traffic_model

    assert "Traffic model" not in server.summarize_optimization(result)


def test_a_cnde_tool_result_carries_the_traffic_model_in_both_blocks():
    response = dict(_OPTIMIZE_RESPONSE, traffic_model="cnde")
    client = CityOptimizationClient(
        "http://testserver",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response)),
    )
    app = server.create_mcp_app(client, fast_mcp_cls=_FakeFastMCP)
    target = {key: _REGISTERED_RECORD[key] for key in ("intersection_id", "name", "lat", "lon")}

    blocks = asyncio.run(app.tools["optimize_intersection"]("c1", target, traffic_model="cnde"))

    assert "Traffic model: CNDE" in blocks[0].text
    assert json.loads(blocks[1].text)["traffic_model"] == "cnde"
    client.close()
