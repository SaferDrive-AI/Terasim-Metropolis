"""HTTP client for the TeraSim city signal-optimization service (/v1/city)."""

from __future__ import annotations

import math
import time
from copy import deepcopy
from typing import Any, Mapping, Sequence

import httpx

_TERMINAL_OK = "completed"
_TERMINAL_FAILED = "failed"
_KPI_WEIGHT_KEYS = frozenset({"control_delay", "stops"})
#: What the service's ``traffic_model`` accepts; omitted, the deployment's default (SUMO).
TRAFFIC_MODELS = ("sumo", "cnde")


def _traffic_model(value: str | None) -> str | None:
    """``value`` when it is None or a traffic model the service accepts, else ValueError."""
    if value is not None and value not in TRAFFIC_MODELS:
        raise ValueError(
            f"traffic_model must be one of {TRAFFIC_MODELS} or None, got {value!r}"
        )
    return value


class CityOptimizationClient:
    """Stateless client for synchronous VSS and legacy city optimization APIs.

    Contract: every non-2xx response raises RuntimeError that includes the
    response body text, so callers (and agents) see the server's actual
    validation message instead of a bare status code. The default timeout is
    intentionally long because synchronous VSS optimization can take minutes.
    """

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 1800.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        # `transport` is a test seam (httpx.MockTransport); production callers
        # never pass it.
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
        )

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CityOptimizationClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- HTTP core -----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        response = self._client.request(method, path, json=json, params=params)
        if not response.is_success:
            raise RuntimeError(
                f"{method} {path} failed with HTTP {response.status_code}: "
                f"{response.text}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"{method} {path} returned a non-JSON success response"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"{method} {path} returned an invalid success response; "
                "expected a JSON object"
            )
        return payload

    # -- API operations ------------------------------------------------------

    def request_signal_optimization(
        self,
        *,
        tmc_json: str | None = None,
        corridor_id: str | None = None,
        base_sumocfg: str | None = None,
        from_intersection: str | None = None,
        to_intersection: str | None = None,
        tls_ids: Sequence[str] | None = None,
        baseline_sheet: str | None = None,
        incident: Mapping[str, Any] | None = None,
        optimizer: str = "auto",
        max_trials: int = 20,
        sim_end: float | None = None,
        n_workers: int = 2,
        kpi_weights: Mapping[str, float] | None = None,
        traffic_model: str | None = None,
    ) -> dict:
        """Submit an optimization run; returns the server response ({run_id, status}).

        Builds the POST /v1/city/optimizations body. Demand comes from exactly
        one of two sources (the server enforces this and its validation
        message is surfaced via RuntimeError):

        * ``corridor_id`` (PRIMARY) — the server-side snapshot queue fed by
          ``push_snapshot``; the server aggregates the recently buffered TMC
          snapshots into the demand. It must match the ``corridor_id`` the
          snapshots carry. A server-side scheduled-config entry is optional:
          without one, pass ``base_sumocfg`` explicitly.
        * ``tmc_json`` (secondary/testing) — an explicit server-side demand
          file; ``base_sumocfg`` required.

        ``corridor_id`` names the demand queue; ``from_intersection`` /
        ``to_intersection`` / ``tls_ids`` name the geometry and may be
        combined with either source (an explicit geometry beats a config
        entry's). File-path parameters (``tmc_json``, ``base_sumocfg``,
        ``baseline_sheet``) are SERVER-side paths (deployment-mounted map
        packages); pure corridor_id mode needs no shared filesystem.
        ``incident`` is passed through verbatim, shaped
        ``{"stalled_vehicles": [{"latitude": .., "longitude": ..}]}``, and
        expedites the run. ``traffic_model="cnde"`` simulates every candidate
        with the CNDE learned driver behavior (much slower, GPU only). The
        corridor endpoints use the JSON aliases "from"/"to"; None fields are
        omitted so server-side defaults apply.
        """
        body: dict[str, Any] = {
            "optimizer": optimizer,
            "max_trials": max_trials,
            "n_workers": n_workers,
        }
        corridor: dict[str, Any] = {}
        if from_intersection is not None:
            corridor["from"] = from_intersection
        if to_intersection is not None:
            corridor["to"] = to_intersection
        if tls_ids:
            corridor["tls_ids"] = list(tls_ids)
        if corridor:
            body["corridor"] = corridor
        optional_fields: tuple[tuple[str, Any], ...] = (
            ("tmc_json", tmc_json),
            ("corridor_id", corridor_id),
            ("base_sumocfg", base_sumocfg),
            ("baseline_sheet", baseline_sheet),
            ("incident", incident),
            ("sim_end", sim_end),
            ("kpi_weights", dict(kpi_weights) if kpi_weights is not None else None),
            ("traffic_model", _traffic_model(traffic_model)),
        )
        for key, value in optional_fields:
            if value is not None:
                body[key] = value
        return self._request("POST", "/v1/city/optimizations", json=body)

    def upload_plan(self, plan: dict, *, corridor_id: str | None = None) -> dict:
        """POST a ``terasim.nema_plan.v1`` document to /v1/city/plans.

        VSS supplies complete intersection identifiers and coordinates. This
        client performs no TMC lookup and does not rewrite the document. The
        caller's object is not mutated.
        Returns the 201 body
        ``{"stored": [[corridor_id, intersection_id], ...], "notes": [...]}``.
        """
        body = deepcopy(plan)
        plan_corridor_id = body.get("corridor_id")
        if (
            isinstance(plan_corridor_id, str)
            and plan_corridor_id != plan_corridor_id.strip()
        ):
            raise ValueError(
                "plan corridor_id must not contain leading or trailing whitespace"
            )
        if isinstance(corridor_id, str) and corridor_id != corridor_id.strip():
            raise ValueError(
                "corridor_id argument must not contain leading or trailing whitespace"
            )
        if (
            corridor_id is not None
            and plan_corridor_id is not None
            and plan_corridor_id != corridor_id
        ):
            raise ValueError(
                "corridor_id argument does not match plan corridor_id: "
                f"{corridor_id!r} != {plan_corridor_id!r}"
            )
        effective_corridor_id = corridor_id or plan_corridor_id
        if (
            not isinstance(effective_corridor_id, str)
            or not effective_corridor_id.strip()
        ):
            raise ValueError(
                "upload_plan requires corridor_id either as an argument or in the plan"
            )

        intersections = body.get("intersections")
        if not isinstance(intersections, list) or not intersections:
            raise ValueError("plan intersections must be a non-empty list")
        for index, intersection in enumerate(intersections):
            if not isinstance(intersection, dict):
                raise ValueError(f"plan intersections[{index}] must be an object")
            intersection_id = intersection.get("intersection_id")
            if not isinstance(intersection_id, str) or not intersection_id.strip():
                raise ValueError(
                    f"plan intersections[{index}].intersection_id must be a non-empty string"
                )
            if intersection_id != intersection_id.strip():
                raise ValueError(
                    f"plan intersections[{index}].intersection_id must not "
                    "contain leading or trailing whitespace"
                )

        params = {"corridor_id": corridor_id} if corridor_id is not None else None
        return self._request("POST", "/v1/city/plans", json=body, params=params)

    def list_plans(self) -> dict:
        """GET the stored ``(corridor_id, intersection_id)`` plan keys."""
        return self._request("GET", "/v1/city/plans")

    def upload_map(self, path: str, proj: str | None = None) -> dict:
        """POST a server-side map file path to /v1/city/maps.

        ``path`` names a map file that already exists on the service's asset
        volume (``$TERASIM_CITY_INPUT_ROOT``): ``.sumocfg`` (prebuilt SUMO),
        ``.osm`` (OpenStreetMap or Lanelet2) or ``.xodr`` (OpenDRIVE); HD maps
        are converted to SUMO on upload. ``proj`` (optional) is the proj.4
        definition of an HD map's own planar coordinates, for files without a
        usable geo-reference. This client never uploads file bytes. Returns the
        201 body ``{"map_id", "duplicate", "source", "summary"}``.
        """
        if not isinstance(path, str) or not path.strip():
            raise ValueError("upload_map requires a non-empty server-side map path")
        body: dict = {"path": path}
        if proj is not None:
            if not isinstance(proj, str) or not proj.strip():
                raise ValueError("upload_map proj must be a non-empty proj.4 string when given")
            body["proj"] = proj
        return self._request("POST", "/v1/city/maps", json=body)

    def push_snapshot(self, payload: dict) -> dict:
        """POST one TMC snapshot payload to /v1/city/snapshots.

        The payload is buffered server-side under its ``corridor_id`` — no
        shared filesystem is involved. Returns the server's 202 body,
        ``{"corridor_id": .., "buffered": <count>}``. Timestamps follow the
        ingest contract: UTC (naive ISO strings are interpreted as UTC).
        """
        response = self._request("POST", "/v1/city/snapshots", json=dict(payload))
        corridor_id = payload.get("corridor_id")
        if (
            isinstance(corridor_id, str)
            and response.get("corridor_id") != corridor_id
        ):
            raise RuntimeError(
                "POST /v1/city/snapshots returned a corridor_id that does not "
                f"match the request: {response.get('corridor_id')!r} != {corridor_id!r}"
            )
        return response

    def build_intersection_request(
        self, corridor_id: str, intersection: Mapping[str, Any]
    ) -> dict:
        """Build a request from the complete VSS-resolved intersection."""
        if not isinstance(intersection, Mapping):
            raise ValueError("intersection must be an object")
        return {
            "type": "intersection",
            "corridor_id": corridor_id,
            "intersection": deepcopy(dict(intersection)),
        }

    def build_corridor_request(
        self,
        corridor_id: str,
        from_intersection: Mapping[str, Any],
        to_intersection: Mapping[str, Any],
    ) -> dict:
        """Build a request from two complete VSS-resolved endpoints."""
        if not isinstance(from_intersection, Mapping):
            raise ValueError("from_intersection must be an object")
        if not isinstance(to_intersection, Mapping):
            raise ValueError("to_intersection must be an object")
        from_payload = deepcopy(dict(from_intersection))
        to_payload = deepcopy(dict(to_intersection))
        if (
            from_payload.get("intersection_id") is not None
            and from_payload.get("intersection_id")
            == to_payload.get("intersection_id")
        ):
            raise ValueError(
                "corridor endpoints must be different; use an intersection request "
                "for a single location"
            )
        return {
            "type": "corridor",
            "corridor_id": corridor_id,
            "corridor": {
                "from": from_payload,
                "to": to_payload,
            },
        }

    def optimize_corridor(
        self,
        corridor_id: str,
        from_intersection: Mapping[str, Any],
        to_intersection: Mapping[str, Any],
        *,
        cost: Mapping[str, Any] | None = None,
        traffic_model: str | None = None,
    ) -> dict:
        """Synchronously optimize a VSS-resolved corridor.

        ``traffic_model="cnde"`` simulates every candidate with the CNDE learned
        driver behavior (much slower, GPU only); None leaves the service default.
        """
        payload = self.build_corridor_request(
            corridor_id, from_intersection, to_intersection
        )
        if cost is not None:
            payload["cost"] = dict(cost)
        if _traffic_model(traffic_model) is not None:
            payload["traffic_model"] = traffic_model
        return self._optimize(payload)

    def optimize_intersection(
        self,
        corridor_id: str,
        intersection: Mapping[str, Any],
        *,
        cost: Mapping[str, Any] | None = None,
        traffic_model: str | None = None,
    ) -> dict:
        """Synchronously optimize one VSS-resolved intersection (``traffic_model``: as above)."""
        payload = self.build_intersection_request(corridor_id, intersection)
        if cost is not None:
            payload["cost"] = dict(cost)
        if _traffic_model(traffic_model) is not None:
            payload["traffic_model"] = traffic_model
        return self._optimize(payload)

    def _optimize(self, payload: dict) -> dict:
        response = self._request("POST", "/v1/city/optimize", json=payload)
        nema_plan = response.get("nema_plan")
        kpis = response.get("kpis")
        if not isinstance(nema_plan, dict) or not isinstance(kpis, dict):
            raise RuntimeError(
                "POST /v1/city/optimize returned an invalid response; expected "
                "{'nema_plan': {...}, 'kpis': {...}}"
            )
        cost_weights = kpis.get("cost_weights")
        if (
            not isinstance(cost_weights, dict)
            or not cost_weights
            or not set(cost_weights).issubset(_KPI_WEIGHT_KEYS)
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
                for value in cost_weights.values()
            )
        ):
            raise RuntimeError(
                "POST /v1/city/optimize returned invalid kpis.cost_weights"
            )
        normalized_kpis = {"cost_weights": dict(cost_weights)}
        for label in ("baseline", "optimized"):
            comparison = kpis.get(label)
            if (
                not isinstance(comparison, dict)
                or isinstance(comparison.get("cost"), bool)
                or not isinstance(comparison.get("cost"), (int, float))
                or not math.isfinite(comparison["cost"])
                or not isinstance(comparison.get("metrics"), dict)
            ):
                raise RuntimeError(
                    "POST /v1/city/optimize returned invalid "
                    f"kpis.{label}; expected {{'cost': number, 'metrics': {{...}}}}"
                )
            normalized_kpis[label] = {
                "cost": comparison["cost"],
                "metrics": dict(comparison["metrics"]),
            }
            # The plan's per-TLS cycle / offsets / greens ride along when the service
            # sent them; it leaves the key out when a result has none, and so does
            # this. The block's contents are the service's contract, not checked here.
            timing = comparison.get("timing")
            if isinstance(timing, dict):
                normalized_kpis[label]["timing"] = dict(timing)
        normalized = {"nema_plan": nema_plan, "kpis": normalized_kpis}
        # Movements the plan never lets move, per signal: the service sends the key
        # only when there are some, and so does this.
        unserved = response.get("unserved_movements")
        if isinstance(unserved, dict) and unserved:
            normalized["unserved_movements"] = dict(unserved)
        # Where the demand came from, sent only for a scenario's own routes.
        config = response.get("config")
        if isinstance(config, dict) and config:
            normalized["config"] = dict(config)
        # The traffic model the candidates ran under, sent only when it is not the
        # default (a CNDE run).
        traffic_model = response.get("traffic_model")
        if isinstance(traffic_model, str) and traffic_model:
            normalized["traffic_model"] = traffic_model
        return normalized

    def get_status(self, run_id: str) -> dict:
        return self._request("GET", f"/v1/city/optimizations/{run_id}/status")

    def get_result(self, run_id: str) -> dict:
        return self._request("GET", f"/v1/city/optimizations/{run_id}/result")

    def get_nema(self, run_id: str) -> dict:
        return self._request("GET", f"/v1/city/optimizations/{run_id}/nema")

    def wait(self, run_id: str, *, timeout_s: float = 1800.0, poll_s: float = 3.0) -> dict:
        """Poll status until the run completes; returns the final status payload.

        Raises RuntimeError when the run fails (including the server's error
        text) and TimeoutError when timeout_s elapses first.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            status = self.get_status(run_id)
            state = status.get("status")
            if state == _TERMINAL_OK:
                return status
            if state == _TERMINAL_FAILED:
                error = status.get("error") or "unknown error"
                raise RuntimeError(f"optimization {run_id} failed: {error}")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"optimization {run_id} did not finish within {timeout_s}s "
                    f"(last status: {state})"
                )
            time.sleep(poll_s)
