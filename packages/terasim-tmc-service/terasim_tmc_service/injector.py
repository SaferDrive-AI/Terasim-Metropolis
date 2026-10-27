"""TMC Injector: push one generated snapshot into the TeraSim city service.

The payload is validated with the same Pydantic model the MCP tool
``upload_traffic_snapshot`` uses, so the periodic path and the agent path share one
contract, then posted with ``CityOptimizationClient.push_snapshot`` (REST
``POST /v1/city/snapshots`` on the same TeraSim Service container that exposes the
MCP server).
"""

from __future__ import annotations

from typing import Any, Mapping

from terasim_agent_skill.client import CityOptimizationClient
from terasim_agent_skill.schemas import TrafficSnapshotInput, model_payload


class SnapshotInjector:
    def __init__(self, client: CityOptimizationClient) -> None:
        self._client = client

    def close(self) -> None:
        self._client.close()

    def inject(self, payload: Mapping[str, Any]) -> dict:
        """Validate ``payload`` as a canonical snapshot and buffer it server-side.

        Raises ``pydantic.ValidationError`` before any HTTP call when the payload does
        not satisfy the MCP snapshot schema, and ``RuntimeError`` (with the server's
        text) when the service rejects it.
        """
        validated = model_payload(payload, TrafficSnapshotInput)
        return self._client.push_snapshot(validated)


__all__ = ["SnapshotInjector"]
