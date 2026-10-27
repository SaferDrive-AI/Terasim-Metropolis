"""MCP server wiring for downloadable optimization artifacts.

With an artifact store configured, every optimize tool result carries
``downloads`` (file name -> public URL) and the chat summary ends with the
download block; the server also serves ``GET /files/{artifact_id}/{filename}``.
Without a store nothing changes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from terasim_agent_skill import server
from terasim_agent_skill.artifacts import ArtifactStore, PlanStoreLookup, SavedArtifacts
from terasim_agent_skill.client import CityOptimizationClient
from terasim_agent_skill.tmc_client import TmcRegistryClient

_OPTIMIZE_RESPONSE = {
    "nema_plan": {
        "schema": "terasim.nema_timing.v1",
        "handoff_ready": True,
        "intersections": [
            {
                "tls_id": "tls-1",
                "pattern_id": "midday_peak",
                "cycle_s": 100.0,
                "offset_s": 8.0,
                "phases": [
                    {"nema_phase": 2, "movements": ["WB-through"], "split_s": 45.7, "green_s": 40.1}
                ],
            }
        ],
    },
    "kpis": {
        "cost_weights": {"control_delay": 0.7, "stops": 0.3},
        "baseline": {"cost": 1.0, "metrics": {"control_delay_s_per_veh": 32.1}},
        "optimized": {"cost": 0.9, "metrics": {"control_delay_s_per_veh": 28.2}},
    },
}
_REGISTRY = {
    "corridor_id": "c1",
    "registered": [
        {
            "intersection_id": "i1",
            "name": "Main St & 1st Ave",
            "lat": 37.335,
            "lon": -121.90339,
            "corridor_id": "c1",
        }
    ],
}
_LINKS = {
    "timing_report.pdf": "https://gw.test/terasim/files/i1-1/timing_report.pdf",
    "nema_plan.json": "https://gw.test/terasim/files/i1-1/nema_plan.json",
}


class _FakeFastMCP:
    """Records tools and custom HTTP routes the way FastMCP would."""

    def __init__(self, name: str, **kwargs):
        self.name = name
        self.tools = {}
        self.routes: list[tuple[str, list[str], object]] = []

    def tool(self, **_options):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register

    def custom_route(self, path: str, methods: list[str], name=None, include_in_schema=True):
        def register(function):
            self.routes.append((path, methods, function))
            return function

        return register


class _RecordingStore:
    def __init__(self, links=_LINKS, error: Exception | None = None):
        self.links = links
        self.error = error
        self.calls: list[dict] = []

    def save_optimization(self, result: dict, *, corridor_id: str, intersections: list[dict]):
        self.calls.append(
            {"result": result, "corridor_id": corridor_id, "intersections": intersections}
        )
        if self.error is not None:
            raise self.error
        return SavedArtifacts("i1-1", Path("/tmp/i1-1"), dict(self.links))


def _city_client(seen: list[httpx.Request] | None = None) -> CityOptimizationClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=_OPTIMIZE_RESPONSE)

    return CityOptimizationClient("http://city.test", transport=httpx.MockTransport(handler))


def _registry_client() -> TmcRegistryClient:
    return TmcRegistryClient(
        "http://tmc:8200",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_REGISTRY)),
    )


def _app(store):
    return server.create_mcp_app(
        _city_client(),
        fast_mcp_cls=_FakeFastMCP,
        tmc_client=_registry_client(),
        artifact_store=store,
    )


def _blocks(result) -> tuple[str, dict]:
    """An optimize tool result: the chat markdown block, then the JSON payload."""
    assert [block.type for block in result] == ["text", "text"]
    return result[0].text, json.loads(result[1].text)


def test_registered_optimization_carries_download_links_and_the_download_block():
    store = _RecordingStore()
    app = _app(store)

    result = asyncio.run(app.tools["optimize_registered_intersection"]("i1"))

    summary, payload = _blocks(result)
    assert payload["downloads"] == _LINKS
    assert payload["artifact_id"] == "i1-1"
    assert {key: payload[key] for key in ("nema_plan", "kpis")} == _OPTIMIZE_RESPONSE
    assert "summary_markdown" not in payload, "the block is not repeated inside the JSON"
    assert summary.startswith("**Signal timing optimization: Main St & 1st Ave**")
    # the PDF is named right under the plan line too: a planner that trims the
    # tail of the block still hands the reader the report
    head = summary.split("**KPIs", 1)[0]
    assert (
        "Signal timing report (PDF): https://gw.test/terasim/files/i1-1/timing_report.pdf"
        in head
    )
    assert summary.rstrip().endswith(
        "**Downloads**\n"
        "- Signal timing report (PDF): https://gw.test/terasim/files/i1-1/timing_report.pdf\n"
        "- Optimized NEMA plan (JSON): https://gw.test/terasim/files/i1-1/nema_plan.json"
    )
    assert store.calls == [
        {
            "result": _OPTIMIZE_RESPONSE,
            "corridor_id": "c1",
            "intersections": [
                {
                    "intersection_id": "i1",
                    "name": "Main St & 1st Ave",
                    "lat": 37.335,
                    "lon": -121.90339,
                }
            ],
        }
    ]


def test_direct_and_corridor_optimizations_also_save_artifacts():
    store = _RecordingStore()
    app = _app(store)
    first = {"intersection_id": "a", "name": "A", "lat": 1.0, "lon": 2.0}
    second = {"intersection_id": "b", "name": "B", "lat": 3.0, "lon": 4.0}

    direct = asyncio.run(app.tools["optimize_intersection"]("c1", first))
    corridor = asyncio.run(app.tools["optimize_corridor"]("c1", first, second))

    assert _blocks(direct)[1]["downloads"] == _LINKS
    assert _blocks(corridor)[1]["downloads"] == _LINKS
    assert _blocks(corridor)[0].startswith("**Signal timing optimization: A / B**")
    assert [call["intersections"] for call in store.calls] == [[first], [first, second]]
    assert {call["corridor_id"] for call in store.calls} == {"c1"}


def test_an_artifact_store_failure_never_fails_the_tool():
    store = _RecordingStore(error=OSError("disk full"))
    app = _app(store)

    result = asyncio.run(app.tools["optimize_registered_intersection"]("i1"))

    summary, payload = _blocks(result)
    assert set(payload) == {"nema_plan", "kpis"}
    assert "Downloads" not in summary


def test_no_store_means_no_download_fields_and_no_file_route():
    app = _app(None)

    result = asyncio.run(app.tools["optimize_registered_intersection"]("i1"))

    summary, payload = _blocks(result)
    assert set(payload) == {"nema_plan", "kpis"}
    assert "Downloads" not in summary
    assert app.routes == []


def test_the_file_route_is_registered_with_a_store():
    app = _app(_RecordingStore())

    assert [(path, methods) for path, methods, _ in app.routes] == [
        ("/files/{artifact_id}/{filename}", ["GET"])
    ]


class _Request:
    def __init__(self, **path_params):
        self.path_params = path_params


def test_the_file_endpoint_serves_saved_files_as_attachments(tmp_path):
    store = ArtifactStore(tmp_path, "https://gw.test/terasim")
    saved = store.save_optimization(
        _OPTIMIZE_RESPONSE,
        corridor_id="c1",
        intersections=[{"intersection_id": "i1", "name": "M", "lat": 1.0, "lon": 2.0}],
    )
    endpoint = server.make_file_endpoint(store)

    response = asyncio.run(
        endpoint(_Request(artifact_id=saved.artifact_id, filename="nema_plan.json"))
    )

    assert response.status_code == 200
    assert response.media_type == "application/json"
    assert 'filename="nema_plan.json"' in response.headers["content-disposition"]
    assert Path(response.path) == saved.directory / "nema_plan.json"


def test_the_file_endpoint_404s_unknown_or_unsafe_paths(tmp_path):
    store = ArtifactStore(tmp_path, "https://gw.test/terasim")
    endpoint = server.make_file_endpoint(store)

    for artifact_id, filename in (
        ("missing", "nema_plan.json"),
        ("..", "nema_plan.json"),
        ("x", "../nema_plan.json"),
    ):
        response = asyncio.run(endpoint(_Request(artifact_id=artifact_id, filename=filename)))
        assert response.status_code == 404
        assert json.loads(response.body) == {"detail": "artifact not found"}


def test_artifact_settings_are_off_without_a_root(monkeypatch):
    monkeypatch.delenv("TERASIM_ARTIFACT_ROOT", raising=False)

    assert server._artifact_settings() is None


def test_artifact_settings_read_root_public_url_and_plan_store(monkeypatch, tmp_path):
    monkeypatch.setenv("TERASIM_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("TERASIM_MCP_PUBLIC_URL", "https://gw.test/terasim/")
    monkeypatch.setenv("TERASIM_CITY_DATA_ROOT", str(tmp_path / "store"))

    store = server._artifact_settings()

    assert isinstance(store, ArtifactStore)
    assert store.root == tmp_path / "artifacts"
    assert store.public_url == "https://gw.test/terasim"
    assert isinstance(store.plan_lookup, PlanStoreLookup)
    assert store.plan_lookup.data_root == tmp_path / "store"


def test_artifact_settings_default_public_url_to_the_local_mcp_port(monkeypatch, tmp_path):
    monkeypatch.setenv("TERASIM_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.delenv("TERASIM_MCP_PUBLIC_URL", raising=False)
    monkeypatch.delenv("TERASIM_CITY_DATA_ROOT", raising=False)
    monkeypatch.setenv("TERASIM_MCP_PORT", "9911")

    store = server._artifact_settings()

    assert store.public_url == "http://127.0.0.1:9911"
    assert store.plan_lookup is None


def test_main_passes_the_artifact_store_to_the_app(monkeypatch, tmp_path):
    monkeypatch.setenv("TERASIM_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("TERASIM_MCP_TRANSPORT", "stdio")
    monkeypatch.delenv("TERASIM_TMC_URL", raising=False)
    captured = {}

    def fake_create(client, *, settings=None, tmc_client=None, artifact_store=None, **kwargs):
        captured["artifact_store"] = artifact_store

        class _App:
            def run(self, transport):
                captured["transport"] = transport

        return _App()

    monkeypatch.setattr(server, "create_mcp_app", fake_create)

    server.main()

    assert isinstance(captured["artifact_store"], ArtifactStore)
    assert captured["artifact_store"].root == tmp_path
    assert captured["transport"] == "stdio"
