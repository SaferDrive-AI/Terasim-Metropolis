"""Version numbers, schema names, file names and defaults of the Plugin Interface."""

from __future__ import annotations

#: Version of the ``terasim-plugin-sdk`` distribution.
SDK_VERSION = "0.1.0"

#: Version of the plugin contract (request, result, plugin protocol), ``"MAJOR.MINOR"``.
#: A plugin declares the version it was written against in its ``api_version``
#: attribute; the registry accepts it when the MAJOR numbers are equal and the
#: plugin's MINOR is not newer than this one.
API_VERSION = "1.0"

#: Python entry-point group that installed distributions use to publish plugins.
ENTRY_POINT_GROUP = "terasim.operation_plugins"

#: Name of the built-in plugin that wraps the core corridor signal-timing optimizer.
BUILTIN_SIGNAL_TIMING = "signal_timing"

#: Schema tags of the SDK's own JSON documents.
REQUEST_SCHEMA = "terasim.operation_request.v1"
RESULT_SCHEMA = "terasim.operation_result.v1"

#: Schema tags of the core documents a request or a result carries.
NEMA_TIMING_SCHEMA = "terasim.nema_timing.v1"
TIMING_SHEET_SCHEMA = "terasim.timing_sheet.v1"
NEMA_PLAN_SCHEMA = "terasim.nema_plan.v1"

#: Files :func:`terasim_plugin_sdk.run_operation` writes into the request's output directory.
REQUEST_FILENAME = "operation_request.json"
RESULT_FILENAME = "operation_result.json"
#: The plan to hand off; every plugin writes it into the output directory.
NEMA_FILENAME = "nema_plan.json"
#: Subdirectory holding inputs the SDK materializes (in-memory TMC payloads, plan stores).
INPUTS_DIRNAME = "inputs"

#: Default KPI weights, the same as the core's ``KpiConfig`` and the service's ``cost.weights``.
DEFAULT_KPI_WEIGHTS: tuple[tuple[str, float], ...] = (("control_delay", 0.7), ("stops", 0.3))

#: Default simulated horizon (s): the contract evaluation window of 30 minutes.
DEFAULT_SIM_END = 1800.0
DEFAULT_SEED = 42
DEFAULT_N_WORKERS = 2
DEFAULT_VEHICLE_BEHAVIOR = "sumo_passive"

#: Plugin names are lowercase identifiers: they are entry-point names, CLI arguments
#: and (in a later release) request field values.
PLUGIN_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

__all__ = [
    "API_VERSION",
    "BUILTIN_SIGNAL_TIMING",
    "DEFAULT_KPI_WEIGHTS",
    "DEFAULT_N_WORKERS",
    "DEFAULT_SEED",
    "DEFAULT_SIM_END",
    "DEFAULT_VEHICLE_BEHAVIOR",
    "ENTRY_POINT_GROUP",
    "INPUTS_DIRNAME",
    "NEMA_FILENAME",
    "NEMA_PLAN_SCHEMA",
    "NEMA_TIMING_SCHEMA",
    "PLUGIN_NAME_PATTERN",
    "REQUEST_FILENAME",
    "REQUEST_SCHEMA",
    "RESULT_FILENAME",
    "RESULT_SCHEMA",
    "SDK_VERSION",
    "TIMING_SHEET_SCHEMA",
]
