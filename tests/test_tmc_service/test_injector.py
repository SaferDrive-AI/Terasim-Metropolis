"""SnapshotInjector validates against the MCP snapshot schema, then pushes via the client."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from terasim_agent_skill.client import CityOptimizationClient
from terasim_tmc_service.generator import build_ftmc
from terasim_tmc_service.injector import SnapshotInjector
from terasim_tmc_service.registry import IntersectionRegistry

from .conftest import EXPECTED_COUNTS, I1, WINDOW_START


def _payload(spec, sample_behaviors) -> dict:
    registry = IntersectionRegistry(spec)
    record = registry.register(**I1)
    return build_ftmc(spec, [record], sample_behaviors, timestamp_start=WINDOW_START).payload


def _injector(handler) -> tuple[SnapshotInjector, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = CityOptimizationClient("http://terasim", transport=httpx.MockTransport(recording))
    return SnapshotInjector(client), seen


def test_inject_posts_validated_snapshot(spec, sample_behaviors):
    injector, seen = _injector(
        lambda request: httpx.Response(202, json={"corridor_id": "demo_arterial", "buffered": 1})
    )
    reply = injector.inject(_payload(spec, sample_behaviors))
    injector.close()

    assert reply == {"corridor_id": "demo_arterial", "buffered": 1}
    assert seen[0].method == "POST" and seen[0].url.path == "/v1/city/snapshots"
    body = json.loads(seen[0].content)
    assert body["timestamp_start"] == WINDOW_START
    assert body["intersections"][0]["name"] == "Main St & 1st Ave"
    assert body["intersections"][0]["counts"] == EXPECTED_COUNTS


def test_inject_rejects_payload_without_name_before_http(spec, sample_behaviors):
    injector, seen = _injector(lambda request: httpx.Response(202, json={}))
    payload = _payload(spec, sample_behaviors)
    del payload["intersections"][0]["name"]
    with pytest.raises(ValidationError):
        injector.inject(payload)
    assert seen == []


def test_inject_surfaces_server_rejection(spec, sample_behaviors):
    injector, _ = _injector(
        lambda request: httpx.Response(422, json={"detail": "timestamp too far in the future"})
    )
    with pytest.raises(RuntimeError, match="too far in the future"):
        injector.inject(_payload(spec, sample_behaviors))
