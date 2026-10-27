"""``terasim-plugin``: list, run and check Traffic Operation plug-ins.

::

    terasim-plugin list [--json]
    terasim-plugin run NAME --map MAP --tmc TMC --out DIR
                       [--plan PLAN [--plan-time ISO]] [--weights WEIGHTS]
                       [--from REF --to REF | --tls ID[,ID...]]
                       [--sim-end S] [--seed N] [--workers N] [--vehicle-behavior NAME]
                       [--data-root DIR] [--param KEY=VALUE ...] [--json] [--quiet]
    terasim-plugin check RESULT [--request REQUEST]

``--map`` is a SUMO ``.sumocfg`` file, the ``map_id`` of a stored map, or ``auto``
(the stored map covering the corridor); stored maps live under ``--data-root``
(default ``$TERASIM_CITY_DATA_ROOT``). ``--plan`` is a timing sheet or NEMA plan JSON
file, a plan-store directory, or ``scenario`` (the map's own signal programs).
``--weights`` is ``control_delay=0.7,stops=0.3`` or a JSON object.

Exit status: 0 success; 1 the run failed or its result is not conformant; 2 the
request is invalid (arguments, missing inputs, unknown plugin).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from terasim_plugin_sdk.constants import (
    DEFAULT_N_WORKERS,
    DEFAULT_SEED,
    DEFAULT_SIM_END,
    DEFAULT_VEHICLE_BEHAVIOR,
    REQUEST_FILENAME,
    RESULT_FILENAME,
    SDK_VERSION,
)
from terasim_plugin_sdk.errors import (
    ConformanceError,
    PluginError,
    PluginInputError,
    PluginNotFoundError,
)
from terasim_plugin_sdk.model import (
    CorridorSelection,
    CurrentPlan,
    KpiWeights,
    MapInput,
    OperationRequest,
    OperationResult,
    TrafficSnapshot,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terasim-plugin",
        description="List, run and check TeraSim Metropolis Traffic Operation plug-ins.",
    )
    parser.add_argument("--version", action="version", version=f"terasim-plugin-sdk {SDK_VERSION}")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log the core pipeline at INFO level"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="list the registered plugins")
    listing.add_argument("--json", action="store_true", help="print JSON")

    run = commands.add_parser("run", help="run a plugin on one request")
    run.add_argument("name", help="plugin name (see 'terasim-plugin list')")
    run.add_argument("--map", required=True, help="SUMO .sumocfg file, stored map_id, or 'auto'")
    run.add_argument("--tmc", required=True, help="TMC JSON file (turning-movement counts)")
    run.add_argument("--out", required=True, help="output directory")
    run.add_argument(
        "--plan", help="current plan: timing sheet / NEMA plan JSON, plan-store dir, or 'scenario'"
    )
    run.add_argument("--plan-time", help="ISO 8601 time selecting NEMA time-of-day patterns")
    run.add_argument(
        "--weights", help="KPI weights: 'control_delay=0.7,stops=0.3' or a JSON object"
    )
    run.add_argument(
        "--from", dest="from_ref", help="corridor start (TMC intersection id or 'lat,lon')"
    )
    run.add_argument("--to", dest="to_ref", help="corridor end (TMC intersection id or 'lat,lon')")
    run.add_argument(
        "--tls", help="explicit corridor signals, comma-separated SUMO traffic-light ids"
    )
    run.add_argument(
        "--sim-end", type=float, default=DEFAULT_SIM_END, help="simulated seconds (default 1800)"
    )
    run.add_argument("--seed", type=int, default=DEFAULT_SEED, help="random seed (default 42)")
    run.add_argument(
        "--workers", type=int, default=DEFAULT_N_WORKERS, help="parallel SUMO processes (default 2)"
    )
    run.add_argument(
        "--vehicle-behavior",
        default=DEFAULT_VEHICLE_BEHAVIOR,
        help="vehicle-behavior registry name",
    )
    run.add_argument(
        "--data-root", help="asset volume with the map store (default $TERASIM_CITY_DATA_ROOT)"
    )
    run.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="plugin-specific option; VALUE is read as JSON when it parses (repeatable)",
    )
    run.add_argument("--json", action="store_true", help="print the full result as JSON")
    run.add_argument("--quiet", action="store_true", help="no progress lines on stderr")

    check = commands.add_parser("check", help="check an operation_result.json against the contract")
    check.add_argument("result", help="operation_result.json or the output directory holding it")
    check.add_argument(
        "--request", help="the operation_request.json to check against (default: next to it)"
    )
    return parser


def _parse_params(items) -> dict:
    params = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            raise PluginInputError(f"--param {item!r} must look like KEY=VALUE")
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        params[key.strip()] = value
    return params


def _map_input(value: str, data_root) -> MapInput:
    path = Path(value)
    if path.suffix == ".sumocfg" or path.is_file():
        if data_root:
            raise PluginInputError("--data-root applies to stored maps, not to a .sumocfg file")
        return MapInput.from_sumocfg(path)
    return MapInput.from_store(None if value == "auto" else value, data_root=data_root)


def build_request(args: argparse.Namespace) -> OperationRequest:
    """The :class:`OperationRequest` for ``terasim-plugin run`` arguments."""
    if args.tls and (args.from_ref or args.to_ref):
        raise PluginInputError("use --tls or --from/--to, not both")
    if args.plan_time and not args.plan:
        raise PluginInputError("--plan-time needs --plan")
    corridor = (
        CorridorSelection.signals(*[tls.strip() for tls in args.tls.split(",") if tls.strip()])
        if args.tls
        else CorridorSelection(from_ref=args.from_ref, to_ref=args.to_ref)
    )
    return OperationRequest(
        map=_map_input(args.map, args.data_root),
        traffic=TrafficSnapshot.from_file(args.tmc),
        output_dir=Path(args.out),
        corridor=corridor,
        current_plan=CurrentPlan.detect(args.plan, plan_time=args.plan_time) if args.plan else None,
        weights=KpiWeights.parse(args.weights) if args.weights else KpiWeights(),
        sim_end=args.sim_end,
        seed=args.seed,
        n_workers=args.workers,
        vehicle_behavior=args.vehicle_behavior,
        params=_parse_params(args.param),
    )


def _cmd_list(args) -> int:
    from terasim_plugin_sdk.registry import default_registry

    registry = default_registry()
    infos = registry.infos()
    errors = registry.load_errors
    if args.json:
        print(
            json.dumps(
                {
                    "plugins": [info.to_dict() for info in infos],
                    "load_errors": [e.to_dict() for e in errors],
                },
                indent=2,
            )
        )
    else:
        rows = [("NAME", "VERSION", "API", "SOURCE", "DESCRIPTION")]
        for info in infos:
            source = (
                info.source if not info.distribution else f"{info.source} ({info.distribution})"
            )
            rows.append((info.name, info.version, info.api_version, source, info.description))
        widths = [max(len(row[i]) for row in rows) for i in range(4)]
        for row in rows:
            print(
                "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:4])) + "  " + row[4]
            )
    for error in errors:
        print(
            f"warning: skipped entry point {error.name!r} ({error.target}) from "
            f"{error.distribution or 'an unknown distribution'}: {error.error}",
            file=sys.stderr,
        )
    return EXIT_OK


def _summary(result: OperationResult) -> str:
    kpis = result.kpis
    lines = [
        f"{result.plugin} {result.plugin_version} on corridor {result.corridor_id} "
        f"({', '.join(result.tls_ids)})"
    ]
    for side in ("baseline", "optimized"):
        block = kpis.get(side, {})
        metrics = block.get("metrics", {})
        shown = "  ".join(
            f"{key} {metrics[key]:.3f}"
            for key in ("control_delay_s_per_veh", "stops_per_veh")
            if isinstance(metrics.get(key), (int, float))
        )
        lines.append(f"  {side:<9} cost {block.get('cost', float('nan')):.4f}  {shown}")
    ready = "yes" if result.nema_plan.get("handoff_ready") else "no"
    lines.append(f"  NEMA plan: {result.nema_plan_path} (handoff ready: {ready})")
    lines.append(f"  result:    {result.output_dir / RESULT_FILENAME}")
    for note in result.notes:
        lines.append(f"  note: {note}")
    if result.unserved_movements:
        lines.append(f"  unserved movements: {json.dumps(result.unserved_movements)}")
    return "\n".join(lines)


def _cmd_run(args) -> int:
    from terasim_plugin_sdk.registry import run_operation

    request = build_request(args)

    def progress(done: int, total: int, label) -> None:
        if not args.quiet:
            print(
                f"progress {done}/{total}" + (f" ({label})" if label else ""),
                file=sys.stderr,
                flush=True,
            )

    result = run_operation(args.name, request, progress=progress)
    print(json.dumps(result.to_dict(), indent=2) if args.json else _summary(result))
    return EXIT_OK


def _cmd_check(args) -> int:
    from terasim_plugin_sdk.conformance import validate_result

    result_path = Path(args.result)
    if result_path.is_dir():
        result_path = result_path / RESULT_FILENAME
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PluginInputError(f"cannot read {result_path}: {exc}") from None
    request_path = Path(args.request) if args.request else result_path.parent / REQUEST_FILENAME
    request = None
    if request_path.is_file():
        request = OperationRequest.from_dict(json.loads(request_path.read_text(encoding="utf-8")))
    elif args.request:
        raise PluginInputError(f"request file not found: {request_path}")
    problems = validate_result(payload, request)
    if problems:
        print(f"{result_path}: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  - {problem}")
        return EXIT_FAILED
    print(f"{result_path}: conformant")
    return EXIT_OK


def main(argv=None) -> int:
    """Entry point of the ``terasim-plugin`` console script."""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: --help / --version (0) or a usage error (2)
        return int(exc.code or 0)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    commands = {"list": _cmd_list, "run": _cmd_run, "check": _cmd_check}
    try:
        return commands[args.command](args)
    except (PluginInputError, PluginNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except ConformanceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except PluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
