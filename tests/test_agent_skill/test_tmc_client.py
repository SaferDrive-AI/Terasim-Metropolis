"""TmcRegistryClient against a stub httpx transport."""

from __future__ import annotations

import json

import httpx
import pytest

from terasim_agent_skill.tmc_client import TmcRegistryClient


def _client(handler) -> tuple[TmcRegistryClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return TmcRegistryClient("http://tmc:8200", transport=httpx.MockTransport(recording)), seen


def test_register_posts_flat_fields():
    record = {"intersection_id": "i1", "name": "Main", "lat": 37.335,
              "lon": -121.90339, "corridor_id": "demo_arterial",
              "sensors": ["i1-cam1", "i1-cam2", "i1-cam3", "i1-cam4"],
              "registered_at": "2026-09-04T18:00:00Z"}
    client, seen = _client(lambda request: httpx.Response(201, json=record))
    reply = client.register_intersection("i1", "Main", 37.335, -121.90339,
                                         corridor_id="demo_arterial")
    client.close()
    assert reply == record
    assert seen[0].method == "POST" and seen[0].url.path == "/v1/tmc/intersections"
    assert json.loads(seen[0].content) == {"intersection_id": "i1", "name": "Main",
                                           "lat": 37.335, "lon": -121.90339,
                                           "corridor_id": "demo_arterial"}


def test_register_omits_corridor_when_not_given():
    client, seen = _client(lambda request: httpx.Response(201, json={"intersection_id": "x"}))
    client.register_intersection("i1", "Main", 37.335, -121.90339)
    assert "corridor_id" not in json.loads(seen[0].content)


def test_list_and_run_once_paths():
    client, seen = _client(lambda request: httpx.Response(200, json={"ok": True}))
    assert client.list_intersections() == {"ok": True}
    assert client.run_once() == {"ok": True}
    assert (seen[0].method, seen[0].url.path) == ("GET", "/v1/tmc/intersections")
    assert (seen[1].method, seen[1].url.path) == ("POST", "/v1/tmc/run-once")


def test_errors_include_server_text():
    client, _ = _client(
        lambda request: httpx.Response(404, json={"detail": "unknown id; known ids: ['i1']"})
    )
    with pytest.raises(RuntimeError, match="known ids: \\['i1'\\]"):
        client.register_intersection("oak", "Park Ave & Oak St", 37.335, -121.9)
