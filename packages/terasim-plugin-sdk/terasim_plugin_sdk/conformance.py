"""Conformance kit: check that a plugin and its results honour the Plugin Interface.

Third parties call it from their own test suites::

    from terasim_plugin_sdk.conformance import demo_request, run_conformance

    def test_my_plugin_conforms(tmp_path):
        request = demo_request("path/to/examples/corridor_demo", tmp_path / "out")
        result = run_conformance(MyPlugin(), request)
        assert result.kpis["optimized"]["cost"] <= result.kpis["baseline"]["cost"]

:func:`validate_result` lists every way a result breaks the contract (an empty
list means conformant); :func:`check_result` raises
:class:`~terasim_plugin_sdk.errors.ConformanceError` instead. :func:`run_conformance`
validates the plugin object, runs it through the standard harness, checks the
progress reports and the result, and re-reads ``operation_result.json``.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from terasim_plugin_sdk import _core
from terasim_plugin_sdk.constants import (
    NEMA_TIMING_SCHEMA,
    PLUGIN_NAME_PATTERN,
    RESULT_SCHEMA,
)
from terasim_plugin_sdk.errors import ConformanceError, PluginInputError
from terasim_plugin_sdk.model import (
    CorridorSelection,
    CurrentPlan,
    KpiWeights,
    MapInput,
    OperationRequest,
    OperationResult,
    TrafficSnapshot,
)
from terasim_plugin_sdk.protocol import is_compatible_api_version, plugin_problems

__all__ = [
    "check_plugin",
    "check_result",
    "demo_request",
    "progress_problems",
    "run_conformance",
    "validate_result",
]

#: Allowed slack when comparing costs (float noise).
_COST_TOLERANCE = 1e-9


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_result(result, request: "OperationRequest | None" = None) -> "list[str]":
    """Every way ``result`` breaks the result contract; ``[]`` when it conforms.

    ``result`` is an :class:`OperationResult` or its JSON form. With ``request``, the
    result is also checked against it (weights echoed, output directory, requested
    signals).
    """
    payload, problems = _payload(result)
    if payload is None:
        return problems
    tls_ids = _tls_ids(payload.get("tls_ids"), problems)
    output_dir = None
    if _nonempty_str(payload.get("output_dir")):
        output_dir = Path(payload["output_dir"])
    else:
        problems.append("output_dir must be set")
    problems.extend(_header_problems(payload))
    problems.extend(_nema_problems(payload.get("nema_plan"), payload.get("corridor_id"), tls_ids))
    problems.extend(_kpi_problems(payload.get("kpis")))
    problems.extend(
        _artifact_problems(payload.get("artifacts"), output_dir, payload.get("nema_plan"))
    )
    problems.extend(_unserved_problems(payload.get("unserved_movements"), tls_ids))
    if request is not None:
        problems.extend(_request_problems(payload, request, output_dir, tls_ids))
    return problems


def check_result(result, request: "OperationRequest | None" = None) -> None:
    """Raise :class:`ConformanceError` unless ``result`` conforms (see :func:`validate_result`)."""
    problems = validate_result(result, request)
    if problems:
        name = result.plugin if isinstance(result, OperationResult) else "operation"
        raise ConformanceError(problems, subject=f"result of plugin {name!r}")


def _payload(result) -> "tuple[dict | None, list[str]]":
    """The result as a JSON-like dict, and whether it serializes with finite numbers."""
    if isinstance(result, OperationResult):
        try:
            payload = result.to_dict()
        except Exception as exc:  # noqa: BLE001 - reported as a problem
            return None, [f"result cannot be serialized: {exc}"]
    elif isinstance(result, Mapping):
        payload = dict(result)
    else:
        return None, [f"result must be an OperationResult, got {type(result).__name__}"]
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        return payload, [f"result is not JSON with finite numbers: {exc}"]
    return payload, []


def _tls_ids(tls_ids, problems: "list[str]") -> "list[str]":
    if (
        not isinstance(tls_ids, (list, tuple))
        or not tls_ids
        or not all(_nonempty_str(tls) for tls in tls_ids)
    ):
        problems.append("tls_ids must be a non-empty list of signal ids")
        return []
    if len(set(tls_ids)) != len(tls_ids):
        problems.append(f"tls_ids has duplicates: {list(tls_ids)}")
    return list(tls_ids)


def _header_problems(payload: Mapping) -> "list[str]":
    problems = []
    if payload.get("schema") != RESULT_SCHEMA:
        problems.append(f"schema is {payload.get('schema')!r}, expected {RESULT_SCHEMA!r}")
    if not is_compatible_api_version(payload.get("api_version")):
        problems.append(
            f"api_version {payload.get('api_version')!r} is not compatible with this SDK"
        )
    plugin = payload.get("plugin")
    if not (isinstance(plugin, str) and re.match(PLUGIN_NAME_PATTERN, plugin)):
        problems.append(f"plugin must be a plugin name, got {plugin!r}")
    if not _nonempty_str(payload.get("plugin_version")):
        problems.append("plugin_version must be a non-empty string")
    if not _nonempty_str(payload.get("corridor_id")):
        problems.append("corridor_id must be a non-empty string")
    notes = payload.get("notes")
    if not isinstance(notes, (list, tuple)) or not all(isinstance(note, str) for note in notes):
        problems.append("notes must be a list of strings")
    if not isinstance(payload.get("details"), Mapping):
        problems.append("details must be a JSON object")
    return problems


def _nema_problems(nema, corridor_id, tls_ids: Sequence[str]) -> "list[str]":
    if not isinstance(nema, Mapping):
        return ["nema_plan must be a JSON object"]
    problems = _nema_header_problems(nema, corridor_id)
    intersections = nema.get("intersections")
    if not isinstance(intersections, list) or not intersections:
        problems.append("nema_plan.intersections must be a non-empty list")
        return problems
    seen: list[str] = []
    for index, item in enumerate(intersections):
        tls_id, item_problems = _nema_intersection_problems(index, item)
        problems.extend(item_problems)
        if tls_id is not None:
            seen.append(tls_id)
    duplicates = sorted({tls for tls in seen if seen.count(tls) > 1})
    if duplicates:
        problems.append(f"nema_plan lists signals more than once: {duplicates}")
    missing = [tls for tls in tls_ids if tls not in seen]
    if missing:
        problems.append(f"nema_plan has no intersection for corridor signal(s) {missing}")
    extra = [tls for tls in seen if tls_ids and tls not in tls_ids]
    if extra:
        problems.append(f"nema_plan has intersections outside the corridor: {extra}")
    return problems


def _nema_header_problems(nema: Mapping, corridor_id) -> "list[str]":
    problems = []
    if nema.get("schema") != NEMA_TIMING_SCHEMA:
        problems.append(
            f"nema_plan.schema is {nema.get('schema')!r}, expected {NEMA_TIMING_SCHEMA!r}"
        )
    if nema.get("corridor_id") != corridor_id:
        problems.append(
            f"nema_plan.corridor_id {nema.get('corridor_id')!r} differs from "
            f"corridor_id {corridor_id!r}"
        )
    if not isinstance(nema.get("handoff_ready"), bool):
        problems.append("nema_plan.handoff_ready must be true or false")
    coordination = nema.get("coordination")
    cycle = coordination.get("cycle_length_s") if isinstance(coordination, Mapping) else None
    if not (_is_number(cycle) and cycle > 0):
        problems.append("nema_plan.coordination.cycle_length_s must be a number > 0")
    return problems


def _nema_intersection_problems(index: int, item) -> "tuple[str | None, list[str]]":
    """``(tls_id or None, problems)`` of one ``nema_plan.intersections`` entry."""
    where = f"nema_plan.intersections[{index}]"
    if not isinstance(item, Mapping):
        return None, [f"{where} must be an object"]
    problems = []
    tls_id = item.get("tls_id")
    if _nonempty_str(tls_id):
        where = f"nema_plan intersection {tls_id!r}"
    else:
        problems.append(f"{where}.tls_id must be a non-empty string")
        tls_id = None
    if not (_is_number(item.get("offset_s")) and item["offset_s"] >= 0):
        problems.append(f"{where}: offset_s must be a number >= 0")
    if "cycle_s" in item and not (_is_number(item["cycle_s"]) and item["cycle_s"] > 0):
        problems.append(f"{where}: cycle_s must be a number > 0")
    rows = item.get("phases")
    if not isinstance(rows, list) or not rows:
        problems.append(f"{where}: phases must be a non-empty list")
        return tls_id, problems
    for row_index, row in enumerate(rows):
        problems.extend(_nema_row_problems(f"{where} phases[{row_index}]", row))
    if not any(_is_number(_get(row, "split_s")) and row["split_s"] > 0 for row in rows):
        problems.append(f"{where}: no phase has a split > 0")
    return tls_id, problems


def _get(row, key):
    return row.get(key) if isinstance(row, Mapping) else None


def _nema_row_problems(where: str, row) -> "list[str]":
    if not isinstance(row, Mapping):
        return [f"{where} must be an object"]
    problems = [
        f"{where}.{key} must be a finite number"
        for key in ("split_s", "green_s", "yellow_s", "all_red_s")
        if not _is_number(row.get(key))
    ]
    problems.extend(
        f"{where}.{key} must be >= 0"
        for key in ("split_s", "yellow_s", "all_red_s")
        if _is_number(row.get(key)) and row[key] < 0
    )
    phase = row.get("nema_phase")
    valid_phase = isinstance(phase, int) and not isinstance(phase, bool) and 1 <= phase <= 8
    if phase is not None and not valid_phase:
        problems.append(f"{where}.nema_phase must be null or a NEMA phase 1..8")
    return problems


def _kpi_problems(kpis) -> "list[str]":
    if not isinstance(kpis, Mapping):
        return ["kpis must be a JSON object"]
    weighted, problems = _weight_problems(kpis.get("cost_weights"))
    costs = {}
    for side in ("baseline", "optimized"):
        cost, side_problems = _side_problems(side, kpis.get(side), weighted)
        problems.extend(side_problems)
        if cost is not None:
            costs[side] = cost
    if len(costs) == 2:
        slack = _COST_TOLERANCE * max(1.0, abs(costs["baseline"]))
        if costs["optimized"] > costs["baseline"] + slack:
            problems.append(
                f"kpis.optimized.cost {costs['optimized']:g} is worse than kpis.baseline.cost "
                f"{costs['baseline']:g}: when nothing beats the baseline, the baseline is "
                "the recommendation"
            )
    return problems


def _weight_problems(weights) -> "tuple[list[str], list[str]]":
    """``(weighted MOE keys, problems)`` of ``kpis.cost_weights``."""
    if not isinstance(weights, Mapping) or not weights:
        return [], ["kpis.cost_weights must be a non-empty object"]
    metrics_for = _core.kpi_metrics()
    problems = [
        f"kpis.cost_weights has unknown key {name!r}; known: {sorted(metrics_for)}"
        for name in weights
        if name not in metrics_for
    ]
    problems.extend(
        f"kpis.cost_weights[{name!r}] must be a number > 0"
        for name, weight in weights.items()
        if not (_is_number(weight) and weight > 0)
    )
    return [metrics_for[name] for name in weights if name in metrics_for], problems


def _side_problems(side: str, block, weighted: Sequence[str]) -> "tuple[float | None, list[str]]":
    """``(cost or None, problems)`` of ``kpis.baseline`` / ``kpis.optimized``."""
    if not isinstance(block, Mapping):
        return None, [f"kpis.{side} must be an object with cost and metrics"]
    problems = []
    cost = float(block["cost"]) if _is_number(block.get("cost")) else None
    if cost is None:
        problems.append(f"kpis.{side}.cost must be a finite number")
    metrics = block.get("metrics")
    if not isinstance(metrics, Mapping):
        problems.append(f"kpis.{side}.metrics must be an object")
    else:
        problems.extend(
            f"kpis.{side}.metrics.{key} must be a finite number >= 0"
            for key in weighted
            if not (_is_number(metrics.get(key)) and metrics[key] >= 0)
        )
    if "timing" in block and not isinstance(block["timing"], Mapping):
        problems.append(f"kpis.{side}.timing must be an object when present")
    return cost, problems


def _artifact_problems(artifacts, output_dir: "Path | None", nema) -> "list[str]":
    if not isinstance(artifacts, Mapping):
        return ["artifacts must be an object mapping names to paths relative to output_dir"]
    problems = []
    for name, relative in artifacts.items():
        if not _nonempty_str(name) or not _nonempty_str(relative):
            problems.append(f"artifact {name!r}: names and paths must be non-empty strings")
            continue
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            problems.append(f"artifact {name!r} must be a path inside output_dir, got {relative!r}")
        elif output_dir is not None and not (output_dir / path).exists():
            problems.append(f"artifact {name!r} does not exist: {output_dir / path}")
    if "nema_plan" not in artifacts:
        problems.append("artifacts must list 'nema_plan' (the nema_plan.json the plugin wrote)")
    elif output_dir is not None and isinstance(artifacts["nema_plan"], str):
        problems.extend(_nema_file_problems(output_dir / artifacts["nema_plan"], nema))
    return problems


def _nema_file_problems(nema_file: Path, nema) -> "list[str]":
    """The written ``nema_plan.json`` must be the result's ``nema_plan``."""
    if not nema_file.is_file():
        return []  # reported as a missing artifact
    try:
        on_disk = json.loads(nema_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"{nema_file} is not valid JSON: {exc}"]
    return [] if on_disk == nema else [f"{nema_file} differs from the result's nema_plan"]


def _unserved_problems(unserved, tls_ids: Sequence[str]) -> "list[str]":
    from terasim_city.plans.model import MOVEMENTS

    if not isinstance(unserved, Mapping):
        return ["unserved_movements must be an object (empty when every movement is served)"]
    problems = []
    for tls_id, movements in unserved.items():
        if tls_ids and tls_id not in tls_ids:
            problems.append(f"unserved_movements names a signal outside the corridor: {tls_id!r}")
        if not isinstance(movements, (list, tuple)) or any(m not in MOVEMENTS for m in movements):
            problems.append(f"unserved_movements[{tls_id!r}] must list movements such as 'EB-left'")
    return problems


def _request_problems(
    payload: Mapping, request: OperationRequest, output_dir, tls_ids
) -> "list[str]":
    problems = []
    kpis = payload.get("kpis")
    weights = kpis.get("cost_weights") if isinstance(kpis, Mapping) else None
    expected = request.weights.to_dict()
    if (
        not isinstance(weights, Mapping)
        or {k: float(v) for k, v in weights.items() if _is_number(v)} != expected
    ):
        problems.append(
            f"kpis.cost_weights {weights!r} does not echo the request's weights {expected!r}"
        )
    if output_dir is not None and Path(output_dir).resolve() != Path(request.output_dir).resolve():
        problems.append(f"output_dir {output_dir} differs from the request's {request.output_dir}")
    wanted = list(request.corridor.tls_ids)
    if wanted and list(tls_ids) != wanted:
        problems.append(f"tls_ids {list(tls_ids)} differ from the requested signals {wanted}")
    return problems


def check_plugin(plugin) -> None:
    """Raise :class:`ConformanceError` unless ``plugin`` is a valid plugin object."""
    problems = plugin_problems(plugin)
    if problems:
        raise ConformanceError(problems, subject=f"plugin {getattr(plugin, 'name', None)!r}")


def progress_problems(calls: Sequence[tuple]) -> "list[str]":
    """Check recorded ``(done, total, label)`` progress calls against the contract.

    At least one call; the first is ``(0, total, None)``; ``done`` never decreases and
    never exceeds ``total``; labels are strings or None. ``done`` may stop short of
    ``total`` when a search ends early.
    """
    if not calls:
        return ["the plugin never called progress"]
    problems = []
    first = calls[0]
    if first[0] != 0 or first[2] is not None:
        problems.append(f"the first progress call must be (0, total, None), got {first!r}")
    previous = 0
    for done, total, label in calls:
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (done, total)):
            problems.append(f"progress ({done!r}, {total!r}) must report integers")
            continue
        if done < previous:
            problems.append(f"progress went backwards: {previous} -> {done}")
        if not 0 <= done <= total:
            problems.append(f"progress done={done} is outside 0..total={total}")
        if label is not None and not isinstance(label, str):
            problems.append(f"progress label {label!r} must be a string or None")
        previous = done
    return problems


def run_conformance(plugin, request: OperationRequest, *, registry=None) -> OperationResult:
    """Run ``plugin`` on ``request`` through the standard harness and check everything.

    Checks the plugin object, the progress reports, the result (against the request)
    and that ``operation_result.json`` reads back to the same result. ``plugin`` may be
    a registered name (looked up in ``registry``, default the process-wide one).
    Returns the result for further, plugin-specific assertions.
    """
    from terasim_plugin_sdk.registry import default_registry, run_operation

    if isinstance(plugin, str):
        plugin = (registry or default_registry()).get(plugin)
    check_plugin(plugin)
    calls: list[tuple] = []
    result = run_operation(
        plugin, request, progress=lambda done, total, label: calls.append((done, total, label))
    )
    problems = progress_problems(calls)
    reread = OperationResult.read(result.output_dir)
    if reread.to_dict() != json.loads(json.dumps(result.to_dict())):
        problems.append("operation_result.json does not read back to the returned result")
    if problems:
        raise ConformanceError(problems, subject=f"plugin {plugin.name!r}")
    return result


def demo_request(
    corridor_dir,
    output_dir,
    *,
    current_plan: "str | None" = "timing_sheet",
    sim_end: float = 300.0,
    seed: int = 42,
    n_workers: int = 2,
    weights: "KpiWeights | None" = None,
    params: "Mapping[str, Any] | None" = None,
) -> OperationRequest:
    """A small request on the synthetic three-signal demo corridor.

    ``corridor_dir`` is ``examples/corridor_demo`` of a TeraSim Metropolis checkout
    (``demo.sumocfg``, ``demo_tmc.json``, ``baseline_sheet.json``); the corridor runs
    from ``i1`` to ``i3`` (signals J1, J2, J3). ``current_plan`` is
    ``"timing_sheet"`` (the demo's 90 s sheet), ``"scenario_programs"`` or ``None``.
    The short ``sim_end`` keeps a conformance run around a minute.
    """
    corridor_dir = Path(corridor_dir)
    files = {
        name: corridor_dir / name
        for name in ("demo.sumocfg", "demo_tmc.json", "baseline_sheet.json")
    }
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        raise PluginInputError(f"demo corridor files not found: {missing}")
    plans = {
        "timing_sheet": CurrentPlan.timing_sheet(files["baseline_sheet.json"]),
        "scenario_programs": CurrentPlan.scenario_programs(),
        None: None,
    }
    if current_plan not in plans:
        raise PluginInputError(f"current_plan must be one of {list(plans)}, got {current_plan!r}")
    return OperationRequest(
        map=MapInput.from_sumocfg(files["demo.sumocfg"]),
        traffic=TrafficSnapshot.from_file(files["demo_tmc.json"]),
        output_dir=Path(output_dir),
        corridor=CorridorSelection.between("i1", "i3"),
        current_plan=plans[current_plan],
        weights=weights or KpiWeights(),
        sim_end=sim_end,
        seed=seed,
        n_workers=n_workers,
        params=dict(params or {}),
    )
