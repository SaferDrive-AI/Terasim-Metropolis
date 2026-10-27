#!/usr/bin/env python3
"""Generate the reference pages of the documentation site from the code.

Writes, under ``docs/reference/``:

* ``openapi.json``      - the OpenAPI document of ``terasim-service`` (city API enabled)
* ``openapi-tmc.json``  - the OpenAPI document of ``terasim-tmc-service``
* ``http-api.md``       - both APIs rendered as Markdown, grouped by tag
                          (city endpoints first, the legacy co-simulation endpoints last)
* ``mcp-tools.md``      - the tools registered by the ``terasim-agent-skill`` MCP server

Run it from an environment where the release packages are importable (for example an
editable install of ``packages/*``), from any working directory:

    python docs/_scripts/gen_reference.py

The output is deterministic: it depends only on the code, never on the clock, the host
or the working directory, so re-running it on unchanged code rewrites identical files.
No network access, Redis server or SUMO run is needed: the apps are only constructed,
never started.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # keep __pycache__ out of the source tree

import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Iterable  # noqa: E402

DOCS = Path(__file__).resolve().parents[1]
REFERENCE = DOCS / "reference"

#: Placeholder TMC collector URL: with TERASIM_TMC_URL set, the MCP server also registers
#: the intersection-registry tools. Nothing is ever sent to it.
DUMMY_TMC_URL = "http://tmc.invalid:8200"

GENERATED_NOTE = (
    "!!! note \"Generated page\"\n"
    "    This page is generated from the code by `docs/_scripts/gen_reference.py`. "
    "Do not edit it by hand; re-run the script after changing the code.\n"
)

# Environment variables that would change what the apps register; cleared so the
# output does not depend on the caller's shell.
_CLEARED_ENV = (
    "TERASIM_CITY_SCHEDULED_CONFIG",
    "TERASIM_CITY_SCHEDULER_ENABLED",
    "TERASIM_CITY_INPUT_ROOT",
    "TERASIM_ARTIFACT_ROOT",
    "TERASIM_MCP_TRANSPORT",
)

# ---------------------------------------------------------------------------
# Building the apps
# ---------------------------------------------------------------------------


def _prepare_environment(scratch: Path) -> None:
    for name in _CLEARED_ENV:
        os.environ.pop(name, None)
    os.environ["TERASIM_ENABLE_CITY_API"] = "true"
    os.environ["TERASIM_CITY_OUTPUT_ROOT"] = str(scratch / "output")
    os.environ["TERASIM_CITY_DATA_ROOT"] = str(scratch / "store")
    os.environ["TERASIM_TMC_URL"] = DUMMY_TMC_URL


def service_openapi() -> dict:
    """OpenAPI of terasim-service with the city API mounted (no Redis connection made)."""
    import terasim_service.api as service_api

    # create_app() pings Redis before mounting the routers; generating the schema
    # needs no server, the Redis client itself connects lazily.
    service_api.check_redis_connection = lambda: None
    app = service_api.create_app()
    return app.openapi()


#: A minimal synthetic corridor spec (Metadata.json shape) for constructing the TMC app.
_SYNTHETIC_SPEC = {
    "corridor_id": "demo_arterial",
    "observation_interval": "10m",
    "intersections": [
        {
            "intersection_id": "i1",
            "lat": 37.335,
            "lon": -121.90339,
            "Headings": {"in_edge": "East", "out_edge": "East"},
            "Movements": {"East - East": "Straight"},
            "Assignments": {"East": "cam1"},
        }
    ],
}


class _NoBehaviors:
    def fetch(self, sensor_id: str, timestamp_start: str, timestamp_end: str) -> list:
        return []


class _NoInjector:
    def inject(self, payload: dict) -> dict:
        return {}

    def close(self) -> None:
        pass


def tmc_openapi() -> dict:
    """OpenAPI of terasim-tmc-service, built around a synthetic in-memory spec."""
    from terasim_tmc_service.app import create_app
    from terasim_tmc_service.registry import IntersectionRegistry
    from terasim_tmc_service.spec import CorridorSpec

    spec = CorridorSpec.from_payload(_SYNTHETIC_SPEC)
    app = create_app(
        spec,
        IntersectionRegistry(spec, None),
        _NoBehaviors(),
        _NoInjector(),
        autostart=False,
        tick_on_register=False,
    )
    return app.openapi()


def mcp_tools() -> list[dict]:
    """The MCP tools exactly as the console script builds the server (registry tools on)."""
    from terasim_agent_skill import server
    from terasim_agent_skill.tmc_client import TmcRegistryClient

    client = server._make_client()
    tmc_client = TmcRegistryClient(os.environ["TERASIM_TMC_URL"])
    try:
        app = server.create_mcp_app(client, settings={}, tmc_client=tmc_client)
        tools = asyncio.run(app.list_tools())
        return [tool.model_dump(mode="json", exclude_none=True) for tool in tools]
    finally:
        client.close()
        tmc_client.close()


# ---------------------------------------------------------------------------
# JSON Schema -> Markdown helpers
# ---------------------------------------------------------------------------


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _cell(text: Any) -> str:
    """One Markdown table cell: single line, pipes escaped."""
    value = " ".join(str(text).split())
    return value.replace("|", "\\|")


def _first_sentence(text: str | None) -> str:
    """The first sentence of a description (a period, then a capital or a code span)."""
    if not text:
        return ""
    flat = " ".join(str(text).split())
    match = re.search(r"(?<=[.])\s+(?=[A-Z`])", flat)
    return flat[: match.start()] if match else flat


def _clean_doc(text: str | None) -> str:
    """A docstring as Markdown paragraphs: re-flowed lines, blank lines kept."""
    if not text:
        return ""
    lines = [line.strip() for line in str(text).strip().splitlines()]
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line:
            current.append(line)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


class TypeNamer:
    """Renders a JSON schema node as a short type string, linking named schemas."""

    def __init__(self, anchor_prefix: str, known: Iterable[str]) -> None:
        self.anchor_prefix = anchor_prefix
        self.known = set(known)

    def link(self, name: str) -> str:
        if name in self.known:
            return f"[`{name}`](#{self.anchor_prefix}{name.lower()})"
        return f"`{name}`"

    def __call__(self, node: Any) -> str:
        if not isinstance(node, dict) or not node:
            return "any"
        if "$ref" in node:
            return self.link(_ref_name(node["$ref"]))
        if "const" in node:
            return f"`{json.dumps(node['const'])}`"
        if "enum" in node:
            return " \\| ".join(f"`{json.dumps(item)}`" for item in node["enum"])
        for key in ("anyOf", "oneOf"):
            if key in node:
                parts: list[str] = []
                for option in node[key]:
                    rendered = self(option)
                    if rendered not in parts:
                        parts.append(rendered)
                return " \\| ".join(parts)
        if "allOf" in node and len(node["allOf"]) == 1:
            return self(node["allOf"][0])
        kind = node.get("type")
        if isinstance(kind, list):
            return " \\| ".join(str(item) for item in kind)
        if kind == "array":
            return f"array of {self(node.get('items', {}))}"
        if kind == "object":
            extra = node.get("additionalProperties")
            if isinstance(extra, dict) and extra:
                return f"object (string → {self(extra)})"
            return "object"
        if kind == "string" and node.get("format"):
            return f"string ({node['format']})"
        return str(kind) if kind else "any"


def _constraints(node: dict) -> list[str]:
    notes: list[str] = []
    for key, label in (
        ("minimum", ">="),
        ("exclusiveMinimum", ">"),
        ("maximum", "<="),
        ("exclusiveMaximum", "<"),
    ):
        if key in node:
            notes.append(f"{label} {node[key]:g}" if isinstance(node[key], (int, float)) else "")
    if "minLength" in node:
        notes.append(f"min length {node['minLength']}")
    if "maxLength" in node:
        notes.append(f"max length {node['maxLength']}")
    if "minItems" in node:
        notes.append(f"min items {node['minItems']}")
    if "pattern" in node:
        notes.append(f"pattern `{node['pattern']}`")
    for option in node.get("anyOf", []):
        if isinstance(option, dict) and option.get("type") != "null":
            notes.extend(_constraints(option))
    return [note for note in notes if note]


def _default_text(node: dict) -> str:
    if "default" not in node:
        return ""
    return f"`{json.dumps(node['default'])}`"


def _properties_table(schema: dict, namer: TypeNamer) -> list[str]:
    properties = schema.get("properties") or {}
    if not properties:
        return []
    required = set(schema.get("required") or [])
    rows = [
        "| Field | Type | Required | Default | Description |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, node in properties.items():
        node = node if isinstance(node, dict) else {}
        description = node.get("description") or ""
        constraints = _constraints(node)
        if constraints:
            description = (description + " " if description else "") + f"({', '.join(constraints)})"
        rows.append(
            f"| `{name}` | {namer(node)} | {'yes' if name in required else 'no'} | "
            f"{_default_text(node)} | {_cell(description)} |"
        )
    return rows


def _schema_section(name: str, schema: dict, namer: TypeNamer, level: str) -> list[str]:
    lines = [f"{level} `{name}` {{ #{namer.anchor_prefix}{name.lower()} }}", ""]
    description = _clean_doc(schema.get("description"))
    if description:
        lines += [description, ""]
    table = _properties_table(schema, namer)
    if table:
        lines += table + [""]
        extra = schema.get("additionalProperties")
        if extra is False:
            lines += ["Unknown fields are rejected.", ""]
        elif extra is True:
            lines += ["Additional fields are accepted.", ""]
    else:
        lines += [f"Type: {namer(schema)}.", ""]
    return lines


# ---------------------------------------------------------------------------
# HTTP API page
# ---------------------------------------------------------------------------

CITY_TAG_ORDER = ("city-optimization", "city-ingest", "city-assets", "city-scheduled")
LEGACY_TAGS = ("simulations", "agents")
TAG_TITLES = {
    "city-optimization": "Optimization runs",
    "city-ingest": "Traffic snapshot ingestion",
    "city-assets": "Maps and signal plans",
    "city-scheduled": "Scheduled runs",
    "city-audit": "Audit log",
    "general": "Service health",
    "simulations": "Co-simulation: simulations",
    "agents": "Co-simulation: agents",
    "tmc": "Intersection registry and TMC generation",
}
_METHOD_ORDER = ("get", "post", "put", "patch", "delete")

#: Usage notes for operations whose code carries no description of its own (rendered
#: only while the operation has none, so a docstring added in the code takes over).
OPERATION_NOTES: dict[tuple[str, str], str] = {
    ("post", "/v1/city/optimize"): (
        "Synchronous optimization, the call behind the MCP optimization tools. `type` is "
        "`intersection` (with `intersection`) or `corridor` (with `corridor.from` and "
        "`corridor.to`); `cost.weights` defaults to `{\"control_delay\": 0.7, \"stops\": 0.3}`. "
        "The demand is the TMC snapshots buffered for `corridor_id` within the last "
        "`TERASIM_CITY_WINDOW_S` seconds, aggregated into one demand file. A scheduled-config "
        "entry for `corridor_id`, when present, supplies request defaults (map, timing sheet, "
        "trial budget, workers). Without a map from the entry, the service selects a stored "
        "map covering the intersections, or downloads and converts an OpenStreetMap extract "
        "when `TERASIM_CITY_OSM_DOWNLOAD` allows it.\n\n"
        "The request stays open until the run has finished and answers `{nema_plan, kpis}`, "
        "plus `unserved_movements` and `config` when they apply. Errors: 400 or 422 for an "
        "invalid request or configuration, 409 when no recent traffic data is buffered for "
        "the corridor or when the best plan has no NEMA export (the detail gives the reason), "
        "429 when not enough worker slots are free, 503 when the job store is unavailable. "
        "The run is recorded with origin `direct` and is listed by "
        "`GET /v1/city/optimizations`."
    ),
    ("post", "/v1/city/optimizations"): (
        "Asynchronous submission, used by the built-in scheduler and by scripts. Name the "
        "demand with exactly one of `corridor_id` (aggregates the buffered snapshots, as the "
        "synchronous call does) or `tmc_json` (a demand file under `TERASIM_CITY_INPUT_ROOT`). "
        "`corridor` selects the geometry: `from`/`to` (TMC intersection ids or `\"lat,lon\"` "
        "strings) or explicit `tls_ids`. An inline `incident` (stalled vehicles) is simulated "
        "in every trial and the run uses the reserved emergency worker slots.\n\n"
        "Answers 202 `{\"run_id\": ..., \"status\": \"submitted\"}`; poll `/status`, then read "
        "`/result` and `/nema`. Errors: 400 for an input file that is missing or outside "
        "`TERASIM_CITY_INPUT_ROOT`, 409 when no snapshots are buffered for `corridor_id`, 422 "
        "for an invalid request, `n_workers` above `TERASIM_CITY_MAX_CONCURRENCY` or no stored "
        "map while OpenStreetMap downloads are off, 429 when the worker slots are busy, 503 "
        "when the job store is unavailable."
    ),
    ("get", "/v1/city/optimizations/{run_id}/status"): (
        "`{run_id, status, progress: {done, total}, best_score, updated_at}`; `status` is "
        "`submitted`, `running`, `completed` or `failed`. 404 for an unknown run."
    ),
    ("get", "/v1/city/optimizations/{run_id}/result"): (
        "The run's full result document (`corridor_optimization_result.json`, schema "
        "`terasim.corridor_optimization.v1`): the resolved corridor, the seed plans, every "
        "trial, the baseline and best plans with their measures of effectiveness and timing, "
        "the search summary and the NEMA export status. 409 until the run has completed (or "
        "when it failed), 410 when the file is no longer available."
    ),
    ("get", "/v1/city/optimizations/{run_id}/nema"): (
        "The exported NEMA timing (`nema_plan.json`, schema `terasim.nema_timing.v1`, see "
        "[NEMA plan formats](nema-plan-format.md)). 409 until the run has completed, or with "
        "the reason when the run exported no NEMA plan."
    ),
    ("post", "/v1/city/snapshots"): (
        "Buffers one TMC snapshot (see [TMC snapshot format](tmc-snapshot-format.md)) under "
        "its `corridor_id`. Answers 202 `{corridor_id, buffered}` with the number of snapshots "
        "now buffered for the corridor. 422 when a required field is missing or malformed, or "
        "when `timestamp_start` lies more than 300 s in the future. Snapshots are kept for a "
        "rolling two hours."
    ),
    ("get", "/v1/city/snapshots/{corridor_id}"): (
        "`{corridor_id, count, window_s}`: the number of buffered snapshots and the span "
        "between the oldest and the newest `timestamp_start` (0 with fewer than two)."
    ),
    ("get", "/v1/city/plans"): (
        "`{plans: [[corridor_id, intersection_id], ...], count}`: the stored plan keys."
    ),
    ("post", "/v1/city/plans"): (
        "Stores a `terasim.nema_plan.v1` document, one record per intersection (see "
        "[NEMA plan formats](nema-plan-format.md)); the optional `corridor_id` query parameter "
        "labels a document that carries none. A later upload for the same intersection "
        "replaces the stored plan. Answers 201 `{stored: [[corridor_id, intersection_id], "
        "...], notes: [...]}`; 400 with the reason for an invalid plan."
    ),
    ("get", "/v1/city/maps"): "`{maps: [map_id, ...], count}`: the stored map ids.",
    ("post", "/v1/city/maps"): (
        "Body `{\"path\": \"<map file on the service volume>\", \"proj\": \"<optional proj.4 "
        "string>\"}`. Detects the format (OpenStreetMap, Lanelet2, OpenDRIVE or a prebuilt "
        "SUMO scenario), converts HD maps to a SUMO network and stores the map package (see "
        "[Map formats](map-formats.md)). Answers 201 `{map_id, duplicate, source, summary}`; "
        "400 for an unknown format or an unusable geo-reference, 422 when no loadable network "
        "can be built."
    ),
    ("get", "/health"): "`{\"status\": \"healthy\"}`.",
    ("get", "/v1/tmc/intersections"): (
        "`{corridor_id, observation_interval, available, registered}`: every intersection of "
        "the corridor spec (`TMC_SPEC_FILE`) with its location, cameras (`sensors`) and "
        "registration state, and the registration records."
    ),
    ("post", "/v1/tmc/intersections"): (
        "Registers an intersection of the corridor spec for periodic counting. `lat`/`lon` "
        "must lie within 50 m of the spec location; `corridor_id`, when given, must match "
        "the spec. Answers 201 with the record (`intersection_id`, `name`, `lat`, `lon`, "
        "`corridor_id`, `sensors`, `registered_at`); 404 for an id the spec does not contain, "
        "422 for coordinates too far from it or another corridor. A successful registration "
        "also starts one counting pass in the background."
    ),
    ("post", "/v1/tmc/run-once"): (
        "Runs one counting pass now: pulls the behavior objects of the latest settled "
        "observation window for every camera of the registered intersections, counts the "
        "turning movements and pushes the snapshot to `terasim-service`. Answers the pass "
        "summary: `status` (`injected`; `skipped` when that window was already injected; "
        "`idle` without registered intersections), the window, the behavior count and the "
        "object labels per camera, and the counts. 502 when the behavior API or the service "
        "rejects a request."
    ),
    ("get", "/v1/tmc/health"): (
        "`{status, corridor_id, observation_interval, registered, settle_lag_s, "
        "counted_object_types, autostart, last_tick}`."
    ),
}


def _operations(spec: dict) -> list[tuple[str, str, dict]]:
    ops = []
    for path, item in spec.get("paths", {}).items():
        for method in _METHOD_ORDER:
            if method in item:
                ops.append((path, method, item[method]))
    return ops


def _group_of(op: dict, default: str) -> str:
    tags = op.get("tags") or [default]
    # An operation listed under several tags goes to its most specific one.
    return tags[-1]


def _op_anchor(prefix: str, method: str, path: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in f"{method}-{path}".lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    return f"{prefix}{slug.strip('-')}"


def _parameters_table(op: dict, namer: TypeNamer) -> list[str]:
    params = op.get("parameters") or []
    if not params:
        return []
    rows = [
        "| Parameter | In | Type | Required | Default | Description |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for param in params:
        schema = param.get("schema") or {}
        description = param.get("description") or schema.get("description") or ""
        constraints = _constraints(schema)
        if constraints:
            description = (description + " " if description else "") + f"({', '.join(constraints)})"
        rows.append(
            f"| `{param.get('name')}` | {param.get('in')} | {namer(schema)} | "
            f"{'yes' if param.get('required') else 'no'} | {_default_text(schema)} | "
            f"{_cell(description)} |"
        )
    return ["**Parameters**", ""] + rows + [""]


def _body_line(op: dict, namer: TypeNamer) -> list[str]:
    body = op.get("requestBody")
    if not body:
        return []
    lines = []
    for media, content in (body.get("content") or {}).items():
        schema = content.get("schema") or {}
        rendered = namer(schema)
        if rendered == "object" and schema.get("additionalProperties") is True:
            rendered = "JSON object"
        required = "required" if body.get("required") else "optional"
        lines.append(f"**Request body** ({required}): `{media}`, {rendered}")
    return lines + [""]


def _responses_table(op: dict, namer: TypeNamer) -> list[str]:
    responses = op.get("responses") or {}
    if not responses:
        return []
    rows = ["**Responses**", "", "| Status | Description | Body |", "| --- | --- | --- |"]
    for status, response in responses.items():
        bodies = []
        for media, content in (response.get("content") or {}).items():
            schema = content.get("schema") or {}
            rendered = namer(schema) if schema else "any"
            bodies.append(f"`{media}`: {rendered}")
        rows.append(
            f"| {status} | {_cell(response.get('description', ''))} | "
            f"{_cell('; '.join(bodies)) if bodies else ''} |"
        )
    return rows + [""]


def _render_operations(
    groups: dict[str, list[tuple[str, str, dict]]],
    order: list[str],
    namer: TypeNamer,
    anchor_prefix: str,
) -> list[str]:
    lines: list[str] = []
    for group in order:
        ops = groups.get(group) or []
        if not ops:
            continue
        lines += [f"### {TAG_TITLES.get(group, group)}", ""]
        lines += ["| Method | Path | Summary |", "| --- | --- | --- |"]
        for path, method, op in ops:
            anchor = _op_anchor(anchor_prefix, method, path)
            lines.append(
                f"| `{method.upper()}` | [`{path}`](#{anchor}) | {_cell(op.get('summary', ''))} |"
            )
        lines.append("")
        for path, method, op in ops:
            anchor = _op_anchor(anchor_prefix, method, path)
            lines += [f"#### `{method.upper()} {path}` {{ #{anchor} }}", ""]
            if op.get("summary"):
                lines += [f"*{op['summary']}*", ""]
            description = _clean_doc(op.get("description"))
            if not description:
                description = OPERATION_NOTES.get((method, path), "")
            if description:
                lines += [description, ""]
            lines += _parameters_table(op, namer)
            lines += _body_line(op, namer)
            lines += _responses_table(op, namer)
    return lines


def _schemas_used(spec: dict, ops: Iterable[tuple[str, str, dict]]) -> list[str]:
    """Component schema names reachable from the given operations, in first-use order."""
    schemas = spec.get("components", {}).get("schemas", {})
    seen: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = _ref_name(ref)
                if name not in seen:
                    seen.append(name)
                    walk(schemas.get(name, {}))
            for key, value in node.items():
                if key != "$ref":
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for _path, _method, op in ops:
        walk(op)
    return seen


def render_http_api(service: dict, tmc: dict) -> str:
    service_schemas = service.get("components", {}).get("schemas", {})
    tmc_schemas = tmc.get("components", {}).get("schemas", {})
    service_namer = TypeNamer("schema-", service_schemas)
    tmc_namer = TypeNamer("tmc-schema-", tmc_schemas)

    groups: dict[str, list[tuple[str, str, dict]]] = {}
    for path, method, op in _operations(service):
        groups.setdefault(_group_of(op, "general"), []).append((path, method, op))
    city = [tag for tag in CITY_TAG_ORDER if tag in groups]
    city += sorted(tag for tag in groups if tag.startswith("city") and tag not in city)
    legacy = [tag for tag in LEGACY_TAGS if tag in groups]
    other = sorted(tag for tag in groups if tag not in city and tag not in legacy)

    city_ops = [op for tag in city + other for op in groups[tag]]
    legacy_ops = [op for tag in legacy for op in groups[tag]]
    city_schema_names = _schemas_used(service, city_ops)
    legacy_schema_names = [
        name for name in _schemas_used(service, legacy_ops) if name not in city_schema_names
    ]

    tmc_ops = _operations(tmc)
    tmc_groups = {"tmc": tmc_ops}
    tmc_schema_names = _schemas_used(tmc, tmc_ops)

    info = service.get("info", {})
    lines = [
        "# HTTP API",
        "",
        GENERATED_NOTE,
        "Two HTTP services ship in the image. `terasim-service` (port 8000) serves the city "
        "signal-optimization API under `/v1/city/*`; `terasim-tmc-service` (port 8200) is the "
        "TMC collector with its intersection registry. The raw OpenAPI documents are "
        "[openapi.json](openapi.json) (`terasim-service`) and "
        "[openapi-tmc.json](openapi-tmc.json) (`terasim-tmc-service`); a running "
        "`terasim-service` also serves its own at `/openapi.json` and an interactive view at "
        "`/docs`.",
        "",
        "| Service | Default port | OpenAPI title | Endpoints |",
        "| --- | --- | --- | --- |",
        f"| `terasim-service` | 8000 | {_cell(info.get('title', ''))} {_cell(info.get('version', ''))} "
        f"| {len(_operations(service))} |",
        f"| `terasim-tmc-service` | 8200 | {_cell(tmc.get('info', {}).get('title', ''))} "
        f"{_cell(tmc.get('info', {}).get('version', ''))} | {len(tmc_ops)} |",
        "",
        "The city API is mounted when `TERASIM_ENABLE_CITY_API` is true (the container image "
        "sets it); the asset endpoints (`/v1/city/maps`, `/v1/city/plans`) additionally need "
        "`TERASIM_CITY_DATA_ROOT`. Errors are JSON objects with a `detail` field.",
        "",
        "## terasim-service: city API",
        "",
    ]
    lines += _render_operations(groups, city + other, service_namer, "op-")
    lines += ["## terasim-tmc-service", ""]
    lines += _render_operations(tmc_groups, ["tmc"], tmc_namer, "tmc-op-")
    lines += [
        "## terasim-service: legacy co-simulation endpoints",
        "",
        "These endpoints drive single TeraSim co-simulation runs from an external simulator "
        "(start, tick, read state, send agent commands). They are part of the engine's "
        "service and independent of the city signal-optimization API.",
        "",
    ]
    lines += _render_operations(groups, legacy, service_namer, "op-")
    lines += ["## Schemas", "", "### terasim-service: city API", ""]
    for name in city_schema_names:
        lines += _schema_section(name, service_schemas.get(name, {}), service_namer, "####")
    lines += ["### terasim-tmc-service", ""]
    for name in tmc_schema_names:
        lines += _schema_section(name, tmc_schemas.get(name, {}), tmc_namer, "####")
    if legacy_schema_names:
        lines += ["### terasim-service: legacy co-simulation endpoints", ""]
        for name in legacy_schema_names:
            lines += _schema_section(name, service_schemas.get(name, {}), service_namer, "####")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# MCP tools page
# ---------------------------------------------------------------------------

TOOL_GROUPS = (
    (
        "Intersection registry",
        "Registered only when `TERASIM_TMC_URL` points at a running `terasim-tmc-service`. "
        "A registered intersection is counted by the TMC collector every observation window.",
        ("list_intersections", "register_intersection"),
    ),
    (
        "Asset and data upload",
        "Map and timing-plan upload, and a manual TMC snapshot push (the TMC collector pushes "
        "snapshots on its own).",
        ("upload_map", "upload_signal_plan", "upload_traffic_snapshot"),
    ),
    (
        "Optimization",
        "Synchronous: each call runs the full optimization and answers when the NEMA plan and "
        "KPIs are ready. The result is two text content blocks: a ready-to-show Markdown summary "
        "(plan, KPI table, NEMA splits, download links when an artifact store is configured), "
        "then the JSON payload (`nema_plan`, `kpis`, and `artifact_id` / `downloads` when "
        "artifacts were saved).",
        (
            "optimize_registered_intersection",
            "optimize_intersection",
            "optimize_intersection_by_id",
            "optimize_corridor",
        ),
    ),
)


def _tool_anchor(name: str) -> str:
    return f"tool-{name.replace('_', '-')}"


def render_mcp_tools(tools: list[dict]) -> str:
    by_name = {tool["name"]: tool for tool in tools}
    defs: dict[str, dict] = {}
    for tool in tools:
        for name, schema in (tool.get("inputSchema", {}).get("$defs") or {}).items():
            defs.setdefault(name, schema)
    namer = TypeNamer("model-", defs)

    placed = [name for _title, _intro, names in TOOL_GROUPS for name in names]
    groups = [
        (title, intro, [name for name in names if name in by_name])
        for title, intro, names in TOOL_GROUPS
    ]
    extra = [tool["name"] for tool in tools if tool["name"] not in placed]
    if extra:
        groups.append(("Other tools", "", extra))

    lines = [
        "# MCP tools",
        "",
        GENERATED_NOTE,
        "`terasim-agent-skill` is an MCP server (server name `terasim-city-optimization`). "
        "With `TERASIM_MCP_TRANSPORT=streamable-http` it serves the MCP endpoint at "
        "`http://<host>:<TERASIM_MCP_PORT>/mcp` (port 9911 by default) and, with "
        "`TERASIM_ARTIFACT_ROOT` set, the result files at `/files/<artifact_id>/<file>`. "
        "Every tool forwards to the `terasim-service` REST API at `TERASIM_CITY_URL`; the MCP "
        "layer itself keeps no state. Input arguments are validated against the schemas below "
        "before any request is made.",
        "",
        "| Tool | Group | Summary |",
        "| --- | --- | --- |",
    ]
    for title, _intro, names in groups:
        for name in names:
            description = by_name[name].get("description", "")
            lines.append(
                f"| [`{name}`](#{_tool_anchor(name)}) | {title} | {_cell(_first_sentence(description))} |"
            )
    lines.append("")

    for title, intro, names in groups:
        if not names:
            continue
        lines += [f"## {title}", ""]
        if intro:
            lines += [intro, ""]
        for name in names:
            tool = by_name[name]
            schema = tool.get("inputSchema", {})
            lines += [f"### `{name}` {{ #{_tool_anchor(name)} }}", ""]
            description = _clean_doc(tool.get("description"))
            if description:
                lines += [description, ""]
            table = _properties_table(schema, namer)
            if table:
                lines += ["**Arguments**", ""] + table + [""]
            else:
                lines += ["**Arguments**: none.", ""]
            if tool.get("outputSchema"):
                lines += ["**Result**: a structured JSON object.", ""]
            elif title == "Optimization":
                lines += ["**Result**: a Markdown text block, then a JSON text block.", ""]
            else:
                lines += ["**Result**: the JSON answer of the service call.", ""]

    if defs:
        lines += [
            "## Input models",
            "",
            "Nested argument types, as published in the tools' input schemas.",
            "",
        ]
        for name in sorted(defs):
            lines += _schema_section(name, defs[name], namer, "###")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {path.relative_to(DOCS.parent)}")


def _dump_json(document: dict) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="terasim-docs-gen-") as scratch:
        _prepare_environment(Path(scratch))
        service = service_openapi()
        tmc = tmc_openapi()
        tools = mcp_tools()
    _write(REFERENCE / "openapi.json", _dump_json(service))
    _write(REFERENCE / "openapi-tmc.json", _dump_json(tmc))
    _write(REFERENCE / "http-api.md", render_http_api(service, tmc))
    _write(REFERENCE / "mcp-tools.md", render_mcp_tools(tools))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
