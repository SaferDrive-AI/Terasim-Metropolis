"""MCP server exposing the TeraSim city optimization API as agent tools.

Run via the `terasim-agent-skill` console script. Requires the optional `mcp`
dependency (`pip install 'terasim-agent-skill[mcp]'`); FastMCP is imported by
create_mcp_app() and Context under a guard, so the client stays usable without it.

The transport is chosen from the environment: ``TERASIM_MCP_TRANSPORT`` is
``stdio`` by default (local agents) and ``streamable-http`` for the VSS
deployment, where the NAT agent's ``mcp_client`` connects to
``http://<TERASIM_MCP_HOST>:<TERASIM_MCP_PORT>/mcp``. Optimization tools emit
both a progress and a logging notification every ``TERASIM_MCP_HEARTBEAT_S``
seconds so that a long call keeps the HTTP response stream alive.

With ``TERASIM_ARTIFACT_ROOT`` set, every optimize result is also written to
disk (JSON + the signal-controller timing-report style PDF, see
:mod:`terasim_agent_skill.artifacts`)
and served back at ``GET /files/<artifact_id>/<filename>``; the result carries
``downloads`` and the chat summary ends with those URLs, built from
``TERASIM_MCP_PUBLIC_URL`` (the reverse-proxied address the reader can open).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Callable

from terasim_agent_skill.artifacts import (
    ArtifactStore,
    PlanStoreLookup,
    SavedArtifacts,
    downloads_markdown,
    media_type_for,
)
from terasim_agent_skill.client import CityOptimizationClient
from terasim_agent_skill.schemas import (
    IntersectionInput,
    OptimizationCostInput,
    SignalPlanInput,
    TrafficModelInput,
    TrafficSnapshotInput,
    model_payload,
)
from terasim_agent_skill.tmc_client import TmcRegistryClient

try:  # pragma: no cover - depends on the optional 'mcp' extra
    # Imported at module scope on purpose: FastMCP resolves the ``ctx`` parameter
    # with typing.get_type_hints(), which only sees module globals. A
    # function-local import would silently disable context (progress) injection.
    from mcp.server.fastmcp import Context
except ImportError:  # pragma: no cover - the client stays usable without 'mcp'
    Context = Any  # type: ignore[assignment,misc]

try:  # pragma: no cover - depends on the optional 'mcp' extra
    from mcp.types import TextContent
except ImportError:  # pragma: no cover - create_mcp_app() reports the missing extra
    TextContent = None  # type: ignore[assignment,misc]

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_S = 1800.0
DEFAULT_MCP_TRANSPORT = "stdio"
DEFAULT_MCP_HOST = "0.0.0.0"
DEFAULT_MCP_PORT = 9911
_MCP_TRANSPORTS = ("stdio", "sse", "streamable-http")
# Long optimizations must keep emitting bytes on the streamable-http response:
# NAT's MCP client builds httpx.AsyncClient() with no timeout, so httpx's 5 s
# default read timeout ends any stream silent for 5 s. 2.0 s leaves margin under it.
HEARTBEAT_S = 2.0
_TARGET_FIELDS = ("intersection_id", "name", "lat", "lon")
_REGISTRY_FIELDS = _TARGET_FIELDS + ("corridor_id",)
FILES_ROUTE = "/files/{artifact_id}/{filename}"

log = logging.getLogger(__name__)


def _base_url() -> str:
    return os.environ.get("TERASIM_CITY_URL", DEFAULT_BASE_URL)


def _timeout_s() -> float:
    raw = os.environ.get("TERASIM_CITY_TIMEOUT_S")
    if raw is None:
        return DEFAULT_TIMEOUT_S
    try:
        timeout_s = float(raw)
    except ValueError as exc:
        raise ValueError("TERASIM_CITY_TIMEOUT_S must be a positive number") from exc
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("TERASIM_CITY_TIMEOUT_S must be a positive number")
    return timeout_s


def _heartbeat_interval() -> float:
    """Seconds between heartbeat notifications during a long tool call."""
    raw = os.environ.get("TERASIM_MCP_HEARTBEAT_S")
    if raw is None:
        return HEARTBEAT_S
    try:
        interval = float(raw)
    except ValueError as exc:
        raise ValueError("TERASIM_MCP_HEARTBEAT_S must be a positive number") from exc
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("TERASIM_MCP_HEARTBEAT_S must be a positive number")
    return interval


async def _run_with_heartbeat(ctx: Any, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking call in a thread, emitting MCP heartbeats while it runs.

    Every interval two notifications go out: ``report_progress``
    (``notifications/progress``, silently dropped by FastMCP unless the client
    sent a ``progressToken``) and ``info`` (``notifications/message``, which
    needs no token and so reaches clients such as NVIDIA NAT that never send
    one). Either is enough to keep bytes flowing on the request's own stream.

    ``ctx`` is FastMCP's injected Context; ``None`` (direct Python calls, tests)
    or any object missing those methods simply runs the call without them. A
    notification that fails is dropped and never allowed to fail the call.
    """
    interval = _heartbeat_interval()
    started = time.monotonic()
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    # A cancelled MCP request (NAT tears sessions down mid-call) leaves the worker
    # thread running: consume its outcome so asyncio never logs it as unretrieved.
    task.add_done_callback(lambda finished: finished.cancelled() or finished.exception())
    report = getattr(ctx, "report_progress", None)
    log = getattr(ctx, "info", None)
    while True:
        done, _pending = await asyncio.wait({task}, timeout=interval)
        if done:
            return task.result()
        if report is None and log is None:
            continue
        elapsed = time.monotonic() - started
        message = f"optimizing... {elapsed:.0f}s"
        if report is not None:
            try:
                await report(elapsed, None, message)
            except Exception:  # pragma: no cover - defensive: a lost beat is harmless
                report = None
        if log is not None:
            try:
                await log(message)
            except Exception:  # pragma: no cover - defensive: a lost beat is harmless
                log = None


def _finite(value: Any) -> float | None:
    """The value as a float, or None when it is not a usable finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _num(value: Any, digits: int = 2) -> str:
    number = _finite(value)
    return "n/a" if number is None else f"{number:.{digits}f}"


def _pct(baseline: Any, optimized: Any) -> str:
    before, after = _finite(baseline), _finite(optimized)
    if before is None or after is None or before == 0.0:
        return "n/a"
    return f"{(after - before) / before * 100.0:+.1f}%"


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "n/a"
    return str(value).strip() or "n/a"


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _delta(before: Any, after: Any) -> str:
    """Signed absolute change (``+3.30`` / ``-1.20``), or n/a."""
    first, second = _finite(before), _finite(after)
    if first is None or second is None:
        return "n/a"
    return f"{second - first:+.2f}"


def _cost_label(kpis: dict) -> str:
    """``cost score``, or the weights spelled out when the result carries them."""
    weights = _dict(kpis.get("cost_weights"))
    delay, stops = _finite(weights.get("control_delay")), _finite(weights.get("stops"))
    if delay is None or stops is None:
        return "cost score"
    return f"weighted cost (delay {delay:g}, stops {stops:g})"


def _kpi_rows(kpis: dict, *, before_label: str) -> list[str]:
    """The before/optimized comparison table: delay, stops, junction delay, cost.

    A metric a side did not report (key absent, or ``None`` when no junction had
    data) renders ``n/a``.
    """
    sides = {label: _dict(kpis.get(label)) for label in ("baseline", "optimized")}
    metrics = {label: _dict(side.get("metrics")) for label, side in sides.items()}
    pairs: list[tuple[str, Any, Any]] = [
        (title, metrics["baseline"].get(key), metrics["optimized"].get(key))
        for title, key in (
            ("control delay (s/veh)", "control_delay_s_per_veh"),
            ("stops per vehicle", "stops_per_veh"),
            ("per-junction delay, edge-based (s/veh)", "corridor_junction_delay_s"),
        )
    ]
    pairs.append((_cost_label(kpis), sides["baseline"].get("cost"), sides["optimized"].get("cost")))
    rows = [f"| metric | {before_label} | optimized | change |", "| --- | --- | --- | --- |"]
    rows += [
        f"| {title} | {_num(before)} | {_num(after)} | {_pct(before, after)} |"
        for title, before, after in pairs
    ]
    return rows


def _junction_rows(kpis: dict, *, before_label: str) -> list[str]:
    """The per-signal edge-based delay table; empty when neither side reports one.

    A signal one side has no value for (it had no data, or only the other side
    lists it) renders ``n/a``.
    """
    before, after = (
        _dict(_dict(_dict(kpis.get(label)).get("metrics")).get("junction_delay_s"))
        for label in ("baseline", "optimized")
    )
    if not before and not after:
        return []
    rows = [
        "",
        "**Per-junction delay (edge-based, s/veh)**",
        "",
        f"| signal | {before_label} | optimized |",
        "| --- | --- | --- |",
    ]
    for tls_id in sorted(set(before) | set(after), key=str):
        delays = " | ".join(_num(side.get(tls_id), 1) for side in (before, after))
        rows.append(f"| {tls_id} | {delays} |")
    return rows


# Cycles closer than this are the same cycle; signals further apart run different ones.
_CYCLE_TOLERANCE_S = 0.5


def _cycle_span(kpis: dict, label: str) -> tuple[float, float] | None:
    """The shortest and longest per-signal cycle one side reports, or None without one."""
    cycles = _dict(_dict(_dict(kpis.get(label)).get("timing")).get("cycle_s"))
    values = [cycle for cycle in map(_finite, cycles.values()) if cycle is not None]
    return (min(values), max(values)) if values else None


def _cycle_mixed(span: tuple[float, float]) -> bool:
    """True when one side's signals do not share a cycle."""
    return span[1] - span[0] > _CYCLE_TOLERANCE_S


def _cycle_text(span: tuple[float, float]) -> str:
    """One side's cycle: ``140 s``, or ``60–280 s (per signal)`` when its signals differ."""
    if _cycle_mixed(span):
        return f"{span[0]:g}–{span[1]:g} s (per signal)"
    return f"{span[1]:g} s"


def _cycle_sentence(kpis: dict) -> str | None:
    """What happened to the cycle length, or None unless both sides report one.

    Signals within half a second of each other share one cycle (the longest shows).
    A side whose signals run different cycles shows as a range, never as its longest
    cycle alone. Cycles, or both ends of two ranges, under half a second apart are
    unchanged.
    """
    before, after = _cycle_span(kpis, "baseline"), _cycle_span(kpis, "optimized")
    if before is None or after is None:
        return None
    if _cycle_mixed(before) and _cycle_mixed(after):
        was, now = (f"{span[0]:g}–{span[1]:g}" for span in (before, after))
        if all(abs(x - y) < _CYCLE_TOLERANCE_S for x, y in zip(before, after)):
            return f"Cycles unchanged ({was} s per signal)."
        return f"Cycles {was} s -> {now} s (per signal)."
    if (
        not _cycle_mixed(before)
        and not _cycle_mixed(after)
        and abs(before[1] - after[1]) < _CYCLE_TOLERANCE_S
    ):
        return f"Cycle unchanged at {_cycle_text(before)}."
    return f"Cycle {_cycle_text(before)} -> {_cycle_text(after)}."


def _stored_pattern(
    entry: dict, current_plan: dict | None, index: int, count: int
) -> dict | None:
    """The stored coordination pattern an optimized entry was seeded from, or None.

    The city's plan (``terasim.nema_plan.v1``, as the artifact store reads it
    from the plan store) is matched to the entry by ``intersection_id``, then
    ``tls_id``, then by position when both sides list the same number of
    intersections; the pattern is the one whose ``pattern_id`` the optimized
    entry carries. A run that fell back to a generated seed has no pattern id,
    so nothing matches and the splits render without a "current" column.
    """
    stored = _dicts(_dict(current_plan).get("intersections"))
    if not stored or not entry.get("pattern_id"):
        return None
    match = None
    for key in ("intersection_id", "tls_id"):
        wanted = entry.get(key)
        if wanted is not None:
            match = next((item for item in stored if item.get(key) == wanted), None)
        if match is not None:
            break
    if match is None and len(stored) == count:
        match = stored[index]
    if match is None:
        return None
    return next(
        (
            pattern
            for pattern in _dicts(match.get("patterns"))
            if pattern.get("pattern_id") == entry.get("pattern_id")
        ),
        None,
    )


def _plan_sentence(entry: dict, plan: dict, pattern: dict | None) -> str:
    """One plain-language line on what the optimized plan is.

    A plan without a city pattern id is a static plan: across two or more
    signals it runs one common cycle with offsets, so it is called a coordinated
    corridor plan; for one signal it has no coordination pattern.
    """
    phases = _dicts(entry.get("phases"))
    numbered = sum(1 for phase in phases if phase.get("nema_phase") is not None)
    pattern_id = entry.get("pattern_id")
    signals = len(_dicts(plan.get("intersections")))
    if pattern_id:
        head = f"{str(pattern_id).replace('_', ' ').upper()} coordination pattern"
    elif signals >= 2:
        head = f"coordinated {len(phases)}-phase corridor plan ({signals} signals)"
    else:
        head = f"demand-timed {len(phases)}-phase plan (no coordination pattern)"
    bits = [head]
    cycle = _finite(entry.get("cycle_s"))
    if cycle is None:
        cycle = _finite(_dict(plan.get("coordination")).get("cycle_length_s"))
    if cycle is not None:
        bits.append(f"cycle {cycle:g} s")
    offset = _finite(entry.get("offset_s"))
    offset_moved = False
    if offset is not None:
        text = f"offset {offset:g} s"
        stored_offset = _finite(_dict(pattern).get("offset_s"))
        if stored_offset is not None:
            offset_moved = abs(stored_offset - offset) >= 0.05
            text += f" (was {stored_offset:g} s)" if offset_moved else " (unchanged)"
        bits.append(text)
    sentence = "Plan: " + ", ".join(bits)
    if pattern is not None and numbered:
        if not _splits_unchanged(phases, pattern):
            sentence += f"; the {numbered} NEMA phase splits were re-balanced (table below)"
        elif offset_moved:
            sentence += f"; the {numbered} NEMA phase splits are unchanged (table below)"
        else:
            sentence += (
                f"; the current plan is still the best plan found, its {numbered} NEMA phase "
                "splits are unchanged (table below)"
            )
    return sentence + "."


def _splits_unchanged(phases: list[dict], pattern: dict) -> bool:
    """Whether every numbered phase keeps the split of the stored pattern (within 0.05 s)."""
    splits = _dict(_dict(pattern).get("splits"))
    numbered = [phase for phase in phases if phase.get("nema_phase") is not None]
    for phase in numbered:
        number = phase.get("nema_phase")
        before = _finite(splits.get(str(number), splits.get(number)))
        after = _finite(phase.get("split_s"))
        if before is None or after is None or abs(before - after) >= 0.05:
            return False
    return bool(numbered)


def _split_rows(entry: dict, pattern: dict | None) -> list[str]:
    """The per-phase split table; with a stored pattern, current vs optimized."""
    phases = _dicts(entry.get("phases"))
    splits = _dict(_dict(pattern).get("splits"))
    compare = pattern is not None and any(phase.get("nema_phase") is not None for phase in phases)
    if compare:
        rows = [
            "| NEMA phase | movements | current split (s) | optimized split (s) "
            "| change (s) | green (s) |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    else:
        rows = ["| NEMA phase | movements | split (s) | green (s) |", "| --- | --- | --- | --- |"]
    for phase in phases:
        number = phase.get("nema_phase")
        movements = phase.get("movements")
        moves = ", ".join(str(item) for item in movements) if isinstance(movements, list) else ""
        cells = ["-" if number is None else _text(number), moves or "n/a"]
        if compare:
            before = None if number is None else splits.get(str(number), splits.get(number))
            cells += [_num(before), _num(phase.get("split_s")), _delta(before, phase.get("split_s"))]
        else:
            cells.append(_num(phase.get("split_s")))
        cells.append(_num(phase.get("green_s")))
        rows.append("| " + " | ".join(cells) + " |")
    if not phases:
        rows.append("| n/a | n/a | n/a | n/a | n/a | n/a |" if compare else "| n/a | n/a | n/a | n/a |")
    return rows


def summarize_optimization(
    result: dict,
    *,
    intersections: list[dict] | None = None,
    current_plan: dict | None = None,
    report_url: str | None = None,
) -> str:
    """Render an optimize result as a compact markdown block for a chat answer.

    Takes the ``{"nema_plan": ..., "kpis": ...}`` payload and returns a titled
    block: one line on what the plan is (pattern, cycle, offset), a facts line
    (demand, the CNDE traffic model when the payload's ``traffic_model`` names it,
    cycle change, the movements the plan never serves when the payload's
    ``unserved_movements`` names some, hand-off), the KPI comparison table (with the
    corridor's edge-based per-junction delay and, when reported, that delay per
    signal), and per intersection the per-phase splits. With the
    city's ``current_plan`` (``terasim.nema_plan.v1``) the split table shows
    current vs optimized and the KPI column is labelled "current plan";
    ``intersections`` (the optimize targets) supply the human-readable names;
    ``report_url`` (the timing report PDF) is named right under the plan line
    as well as in the caller's download list, because a small planner that
    trims the tail of a long block still hands the reader the report.
    Small planner models condense a large nested tool result and drop the
    splits; this block is short enough to be pasted whole.

    Deliberately total: any key may be missing or the wrong type, and the cell
    renders ``n/a`` instead of raising. Never mutates ``result``.
    """
    payload = _dict(result)
    plan = _dict(payload.get("nema_plan"))
    kpis = _dict(payload.get("kpis"))
    targets = _dicts(intersections)
    names = [str(target["name"]).strip() for target in targets if target.get("name")]
    title = " / ".join(names) or "registered intersection"
    entries = _dicts(plan.get("intersections"))
    patterns = [
        _stored_pattern(entry, current_plan, index, len(entries))
        for index, entry in enumerate(entries)
    ]
    compare = any(pattern is not None for pattern in patterns)

    lines = [f"**Signal timing optimization: {title}**", ""]
    for entry, pattern in zip(entries, patterns):
        lines.append(_plan_sentence(entry, plan, pattern))
    vehicles = _finite(_dict(_dict(kpis.get("baseline")).get("metrics")).get("vehicles_total"))
    facts = []
    if vehicles is not None:
        facts.append(
            f"Demand: {vehicles:g} vehicles simulated from {_demand_origin(payload.get('config'))}."
        )
    facts += [
        sentence
        for sentence in (
            _traffic_model_sentence(payload.get("traffic_model")),
            _cycle_sentence(kpis),
            _unserved_sentence(payload.get("unserved_movements")),
        )
        if sentence is not None
    ]
    if "handoff_ready" in plan:
        facts.append(f"Hand-off ready: {_text(plan.get('handoff_ready'))}.")
    if facts:
        lines.append(" ".join(facts))
    if report_url:
        lines.append(f"Signal timing report (PDF): {report_url}")
    before_label = "current plan" if compare else "baseline"
    lines += ["", f"**KPIs ({before_label} -> optimized)**", ""]
    lines += _kpi_rows(kpis, before_label=before_label)
    lines += _junction_rows(kpis, before_label=before_label)
    if not entries:
        return "\n".join(lines + ["", "n/a - the result carries no intersection timings"])
    for index, (entry, pattern) in enumerate(zip(entries, patterns)):
        label = names[index] if index < len(names) else _text(entry.get("tls_id"))
        heading = "current -> optimized" if pattern is not None else "optimized"
        lines += ["", f"**NEMA splits ({heading}): {label}**", ""]
        lines += _split_rows(entry, pattern)
    return "\n".join(lines)


def _demand_origin(config: Any) -> str:
    """Where the simulated demand came from, per the run's ``config`` sources.

    The scenario's own route files when either ``flow_source`` (the vehicles) or
    ``counts_source`` (the turning counts the plan is sized on) is
    ``scenario_routes``; otherwise the measured traffic counts.
    """
    sources = _dict(config)
    if "scenario_routes" in (sources.get("flow_source"), sources.get("counts_source")):
        return "the scenario's own route files"
    return "the measured traffic counts"


def _traffic_model_sentence(traffic_model: Any) -> str | None:
    """The sentence saying CNDE traffic simulated the plans, or None for the default traffic.

    The service names a ``traffic_model`` only for a run that was not simulated with
    the deployment's default (SUMO) traffic, so a default answer reads as before.
    """
    if traffic_model != "cnde":
        return None
    return (
        "Traffic model: CNDE (Neural NDE, learned driver behavior) simulated every "
        "candidate plan."
    )


def _unserved_sentence(unserved: Any) -> str | None:
    """``Unserved movements: n34 EB-left; n69 WB-left, WB-through.``, or None.

    ``unserved`` maps each signal to the movements the plan never lets move;
    signals are listed in id order, and anything that is not a list of movement
    names is ignored.
    """
    if not isinstance(unserved, dict):
        return None
    parts = []
    for tls_id in sorted(unserved, key=str):
        movements = unserved[tls_id]
        if not isinstance(movements, list):
            continue
        names = [str(movement) for movement in movements if isinstance(movement, str)]
        if names:
            parts.append(f"{tls_id} {', '.join(names)}")
    return f"Unserved movements: {'; '.join(parts)}." if parts else None


def _with_summary(
    result: Any,
    saved: SavedArtifacts | None = None,
    *,
    intersections: list[dict] | None = None,
) -> dict:
    """Copy the client's payload behind the ready-to-show markdown block.

    ``summary_markdown`` comes first so a planner reading the document top-down
    meets the answer before the nested plan. With saved artifacts the payload
    also carries ``artifact_id`` and ``downloads`` (file name -> URL), the
    block ends with those URLs (PDF first) and its split table compares the
    city's current plan when the store had it.
    """
    payload = _dict(result)
    current_plan = saved.current_plan if saved is not None else None
    report_url = saved.links.get("timing_report.pdf") if saved is not None else None
    summary = summarize_optimization(
        payload,
        intersections=intersections,
        current_plan=current_plan,
        report_url=report_url,
    )
    if saved is None:
        return {"summary_markdown": summary, **payload}
    return {
        "summary_markdown": f"{summary}\n\n{downloads_markdown(saved.links)}",
        "artifact_id": saved.artifact_id,
        "downloads": dict(saved.links),
        **payload,
    }


def chat_content(document: dict) -> list:
    """The optimize result as MCP content: the markdown answer, then the JSON.

    NAT joins a tool's text blocks with newlines, so the planner sees the
    ready-to-show block as plain text at the very top and the structured
    payload (``nema_plan``/``kpis``/``artifact_id``/``downloads``) after it;
    a small planner model pastes raw markdown far more reliably than it un-escapes
    the same block out of a JSON string. Scripts keep parsing the last text block.
    """
    if TextContent is None:  # pragma: no cover - guarded by create_mcp_app()
        raise RuntimeError("The 'mcp' package is not installed")
    summary = str(document.get("summary_markdown") or "")
    payload = {key: value for key, value in document.items() if key != "summary_markdown"}
    return [
        TextContent(type="text", text=summary),
        TextContent(type="text", text=json.dumps(payload, indent=2)),
    ]


def _artifact_settings() -> ArtifactStore | None:
    """The artifact store from the environment, or None when it is not enabled.

    ``TERASIM_ARTIFACT_ROOT`` switches the feature on. ``TERASIM_MCP_PUBLIC_URL``
    is the address the *reader* can open (a reverse proxy in front of this
    server's ``/files`` path); it defaults to the local MCP port, which only
    works on the same machine. ``TERASIM_CITY_DATA_ROOT`` (the service's asset
    volume, mounted read-only) lets the PDF show the current plan.
    """
    root = os.environ.get("TERASIM_ARTIFACT_ROOT")
    if not root:
        return None
    port = os.environ.get("TERASIM_MCP_PORT", str(DEFAULT_MCP_PORT))
    public_url = os.environ.get("TERASIM_MCP_PUBLIC_URL") or f"http://127.0.0.1:{port}"
    data_root = os.environ.get("TERASIM_CITY_DATA_ROOT")
    lookup = PlanStoreLookup(data_root) if data_root else None
    return ArtifactStore(Path(root), public_url, plan_lookup=lookup)


def make_file_endpoint(store: ArtifactStore) -> Callable[[Any], Any]:
    """The ``GET /files/{artifact_id}/{filename}`` handler over one store.

    Only files the store wrote are served (``ArtifactStore.resolve`` rejects
    everything else), as attachments so a browser downloads rather than
    renders them. Registered with FastMCP's ``custom_route``, which puts the
    route on the same Starlette app as ``/mcp``.
    """
    from starlette.responses import FileResponse, JSONResponse

    async def download(request: Any) -> Any:
        params = getattr(request, "path_params", {}) or {}
        path = store.resolve(str(params.get("artifact_id", "")), str(params.get("filename", "")))
        if path is None:
            return JSONResponse({"detail": "artifact not found"}, status_code=404)
        return FileResponse(path, media_type=media_type_for(path), filename=path.name)

    return download


def _cost_payload(cost: OptimizationCostInput | None) -> dict | None:
    return model_payload(cost, OptimizationCostInput) if cost is not None else None


def _registered_target(
    tmc_client: TmcRegistryClient, intersection_id: str
) -> tuple[str, dict[str, Any]]:
    """Resolve a registered intersection into ``(corridor_id, intersection)``.

    Raises RuntimeError naming the registered ids when the id is unknown, so the
    agent can recover without guessing the nested object's shape.
    """
    raw = tmc_client.list_intersections().get("registered")
    records = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    for record in records:
        if record.get("intersection_id") != intersection_id:
            continue
        missing = [field for field in _REGISTRY_FIELDS if record.get(field) is None]
        if missing:
            raise RuntimeError(
                f"the registry entry for {intersection_id!r} is missing {missing}; "
                "re-register it with register_intersection or call optimize_intersection "
                "with the full intersection object"
            )
        return str(record["corridor_id"]), {
            field: record[field] for field in _TARGET_FIELDS
        }
    known = ", ".join(sorted(str(record.get("intersection_id")) for record in records))
    raise RuntimeError(
        f"intersection {intersection_id!r} is not registered (registered ids: "
        f"{known or 'none'}); call list_intersections to see what exists and "
        "register_intersection to register it first"
    )


def _transport_settings() -> tuple[str, dict[str, Any]]:
    """Return ``(transport, FastMCP constructor kwargs)`` from the environment.

    ``stdio`` needs no network settings. HTTP transports read the bind host and
    port; ``TERASIM_MCP_STATELESS=1`` turns on FastMCP's stateless HTTP mode.
    """
    transport = os.environ.get("TERASIM_MCP_TRANSPORT", DEFAULT_MCP_TRANSPORT)
    if transport not in _MCP_TRANSPORTS:
        raise ValueError(
            f"TERASIM_MCP_TRANSPORT must be one of {_MCP_TRANSPORTS}, got {transport!r}"
        )
    if transport == "stdio":
        return transport, {}
    raw_port = os.environ.get("TERASIM_MCP_PORT", str(DEFAULT_MCP_PORT))
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("TERASIM_MCP_PORT must be an integer port number") from exc
    if not 1 <= port <= 65535:
        raise ValueError("TERASIM_MCP_PORT must be within 1..65535")
    settings: dict[str, Any] = {
        "host": os.environ.get("TERASIM_MCP_HOST", DEFAULT_MCP_HOST),
        "port": port,
        "stateless_http": os.environ.get("TERASIM_MCP_STATELESS", "0") == "1",
    }
    return transport, settings


def _make_client() -> CityOptimizationClient:
    return CityOptimizationClient(_base_url(), timeout_s=_timeout_s())


def create_mcp_app(
    client: CityOptimizationClient,
    *,
    fast_mcp_cls: Any | None = None,
    settings: dict[str, Any] | None = None,
    tmc_client: TmcRegistryClient | None = None,
    artifact_store: ArtifactStore | None = None,
) -> Any:
    """Build the stateless synchronous public MCP surface.

    ``settings`` are forwarded verbatim to the FastMCP constructor (bind host,
    port, stateless flag); ``None`` keeps FastMCP's defaults. With an
    ``artifact_store`` every optimize result is saved and linked (see
    :func:`_with_summary`) and ``/files/...`` serves the saved files.
    """
    if fast_mcp_cls is None:
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError as exc:
            raise RuntimeError(
                "The 'mcp' package is not installed. Install it with "
                "pip install 'terasim-agent-skill[mcp]' (or pip install 'mcp>=1.0,<2')."
            ) from exc
        fast_mcp_cls = FastMCP

    app = fast_mcp_cls("terasim-city-optimization", **dict(settings or {}))

    if artifact_store is not None:
        app.custom_route(FILES_ROUTE, methods=["GET"])(make_file_endpoint(artifact_store))

    async def finish(result: Any, corridor_id: str, intersections: list[dict]) -> list:
        """Save the artifacts (never fatal) and shape the tool result."""
        saved = None
        if artifact_store is not None:
            try:
                saved = await asyncio.to_thread(
                    artifact_store.save_optimization,
                    result,
                    corridor_id=corridor_id,
                    intersections=intersections,
                )
            except Exception:
                log.warning("optimization artifacts not saved", exc_info=True)
        return chat_content(_with_summary(result, saved, intersections=intersections))

    # The optimize tools answer with content blocks (markdown first, JSON second),
    # so FastMCP must not wrap them into a structured {"result": [...]} envelope:
    # structured_output=False keeps the blocks as the tool's only content.

    @app.tool()
    def upload_traffic_snapshot(snapshot: TrafficSnapshotInput) -> dict:
        """Upload a canonical TMC snapshot without retaining client-side state."""
        return client.push_snapshot(model_payload(snapshot, TrafficSnapshotInput))

    @app.tool()
    def upload_signal_plan(
        plan: SignalPlanInput, corridor_id: str | None = None
    ) -> dict:
        """Upload the complete NEMA plan supplied by VSS without rewriting it."""
        return client.upload_plan(
            model_payload(plan, SignalPlanInput), corridor_id=corridor_id
        )

    @app.tool()
    def upload_map(path: str, proj: str | None = None) -> dict:
        """Register a map file that already sits on the TeraSim asset volume: .sumocfg (prebuilt
        SUMO), .osm (OpenStreetMap or Lanelet2) or .xodr (OpenDRIVE); HD maps are converted to SUMO.
        `proj` is optional: a proj.4 string for the map's own x/y when the file has no usable
        geo-reference (the error says when it is needed). Returns map_id, duplicate, source and a
        summary (edges, junctions, traffic_lights, geo_referenced, warnings)."""
        return client.upload_map(path, proj=proj)

    @app.tool(structured_output=False)
    async def optimize_intersection(
        corridor_id: str,
        intersection: IntersectionInput,
        cost: OptimizationCostInput | None = None,
        traffic_model: TrafficModelInput = None,
        ctx: Context | None = None,
    ) -> list[TextContent]:
        """Synchronously return the NEMA plan and KPIs for one intersection. The result
        starts with a ready-to-show markdown block (plan, KPI table, NEMA splits, download
        links); present that block verbatim. Leave traffic_model unset unless the user
        explicitly asks for CNDE, Neural NDE, neural traffic or learned / naturalistic driver
        behavior; then pass "cnde" (needs a GPU, much slower)."""
        target = model_payload(intersection, IntersectionInput)
        result = await _run_with_heartbeat(
            ctx,
            client.optimize_intersection,
            corridor_id,
            target,
            cost=_cost_payload(cost),
            traffic_model=traffic_model,
        )
        return await finish(result, corridor_id, [target])

    @app.tool(structured_output=False)
    async def optimize_intersection_by_id(
        corridor_id: str,
        intersection_id: str,
        name: str,
        lat: float,
        lon: float,
        cost: OptimizationCostInput | None = None,
        traffic_model: TrafficModelInput = None,
        ctx: Context | None = None,
    ) -> list[TextContent]:
        """Optimize one intersection from flat fields instead of a nested object. The result
        starts with a ready-to-show markdown block (plan, KPI table, NEMA splits, download
        links); present that block verbatim. Leave traffic_model unset unless the user
        explicitly asks for CNDE, Neural NDE, neural traffic or learned / naturalistic driver
        behavior; then pass "cnde" (needs a GPU, much slower)."""
        target = {"intersection_id": intersection_id, "name": name, "lat": lat, "lon": lon}
        return await optimize_intersection(corridor_id, target, cost, traffic_model, ctx)

    @app.tool(structured_output=False)
    async def optimize_corridor(
        corridor_id: str,
        from_intersection: IntersectionInput,
        to_intersection: IntersectionInput,
        cost: OptimizationCostInput | None = None,
        traffic_model: TrafficModelInput = None,
        ctx: Context | None = None,
    ) -> list[TextContent]:
        """Synchronously return the NEMA plan and KPIs for a corridor. The result starts
        with a ready-to-show markdown block (plan, KPI table, NEMA splits, download links);
        present that block verbatim. Leave traffic_model unset unless the user explicitly
        asks for CNDE, Neural NDE, neural traffic or learned / naturalistic driver behavior;
        then pass "cnde" (needs a GPU, much slower)."""
        start = model_payload(from_intersection, IntersectionInput)
        end = model_payload(to_intersection, IntersectionInput)
        result = await _run_with_heartbeat(
            ctx,
            client.optimize_corridor,
            corridor_id,
            start,
            end,
            cost=_cost_payload(cost),
            traffic_model=traffic_model,
        )
        return await finish(result, corridor_id, [start, end])

    if tmc_client is not None:

        @app.tool()
        def register_intersection(
            intersection_id: str,
            name: str,
            lat: float,
            lon: float,
            corridor_id: str | None = None,
        ) -> dict:
            """Activate periodic TMC collection for a spec intersection (Skill 1).

            ``intersection_id`` must be one of the ids returned by ``list_intersections``;
            ``lat``/``lon`` must lie within 50 m of that intersection's spec location.
            """
            return tmc_client.register_intersection(
                intersection_id, name, lat, lon, corridor_id=corridor_id
            )

        @app.tool()
        def list_intersections() -> dict:
            """List the corridor's known intersections and which ones are registered."""
            return tmc_client.list_intersections()

        @app.tool(structured_output=False)
        async def optimize_registered_intersection(
            intersection_id: str,
            cost: OptimizationCostInput | None = None,
            traffic_model: TrafficModelInput = None,
            ctx: Context | None = None,
        ) -> list[TextContent]:
            # One physical line per 100-col limit, one logical line on the wire: FastMCP
            # serves the raw ``__doc__`` (mcp 1.29.1 does not run ``inspect.cleandoc``).
            (
                """Optimize signal timing for an already-registered intersection by id """
                """only (the registry supplies name/lat/lon/corridor). Prefer this over """
                """optimize_intersection. Synchronous; may take a few minutes. The result """
                """starts with a ready-to-show markdown block (plan, KPI table, NEMA """
                """splits, download links); present that block verbatim. Leave """
                """traffic_model unset unless the user explicitly asks for CNDE, Neural """
                """NDE, neural traffic or learned / naturalistic driver behavior; then """
                """pass "cnde" (needs a GPU, much slower)."""
            )
            corridor_id, intersection = _registered_target(tmc_client, intersection_id)
            result = await _run_with_heartbeat(
                ctx,
                client.optimize_intersection,
                corridor_id,
                intersection,
                cost=_cost_payload(cost),
                traffic_model=traffic_model,
            )
            return await finish(result, corridor_id, [intersection])

    return app


def main() -> None:
    transport, settings = _transport_settings()
    client = _make_client()
    tmc_url = os.environ.get("TERASIM_TMC_URL")
    tmc_client = TmcRegistryClient(tmc_url) if tmc_url else None
    try:
        app = create_mcp_app(
            client,
            settings=settings,
            tmc_client=tmc_client,
            artifact_store=_artifact_settings(),
        )
        app.run(transport=transport)
    except RuntimeError as exc:
        if "The 'mcp' package is not installed" not in str(exc):
            raise
        raise SystemExit(str(exc)) from exc
    finally:
        client.close()
        if tmc_client is not None:
            tmc_client.close()


if __name__ == "__main__":
    main()
