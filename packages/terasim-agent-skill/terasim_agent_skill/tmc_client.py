"""HTTP client for the TMC microservice (intersection registry / one-shot ticks)."""

from __future__ import annotations

from typing import Any

import httpx


class TmcRegistryClient:
    """Thin client for ``terasim-tmc-service``; non-2xx raises RuntimeError with the body."""

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 60.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, *, json: dict | None = None) -> dict:
        response = self._client.request(method, path, json=json)
        if not response.is_success:
            raise RuntimeError(
                f"{method} {path} failed with HTTP {response.status_code}: {response.text}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{method} {path} returned a non-JSON success response") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{method} {path} returned an invalid response; expected an object")
        return payload

    def register_intersection(
        self,
        intersection_id: str,
        name: str,
        lat: float,
        lon: float,
        *,
        corridor_id: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "intersection_id": intersection_id,
            "name": name,
            "lat": lat,
            "lon": lon,
        }
        if corridor_id is not None:
            body["corridor_id"] = corridor_id
        return self._request("POST", "/v1/tmc/intersections", json=body)

    def list_intersections(self) -> dict:
        return self._request("GET", "/v1/tmc/intersections")

    def run_once(self) -> dict:
        return self._request("POST", "/v1/tmc/run-once")


__all__ = ["TmcRegistryClient"]
