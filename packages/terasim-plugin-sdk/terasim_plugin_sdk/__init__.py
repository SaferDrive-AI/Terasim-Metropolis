"""TeraSim Metropolis Plugin Interface / SDK.

Traffic Operation plug-ins are installable Python modules that turn a map, a TMC
snapshot, the current signal plan and KPI weights into a recommended NEMA timing
plan with a KPI comparison, without changes to the core. This package defines:

* the typed request and result (:mod:`terasim_plugin_sdk.model`);
* the plugin protocol and the API version rule (:mod:`terasim_plugin_sdk.protocol`);
* the registry -- built-ins, ``terasim.operation_plugins`` entry points, explicit
  registration -- and the :func:`run_operation` harness
  (:mod:`terasim_plugin_sdk.registry`);
* reusable building blocks on top of the core pipeline (:mod:`terasim_plugin_sdk.toolkit`);
* the built-in ``signal_timing`` plugin (:mod:`terasim_plugin_sdk.builtin`);
* the conformance kit (:mod:`terasim_plugin_sdk.conformance`);
* the ``terasim-plugin`` command line (:mod:`terasim_plugin_sdk.cli`).
"""

from terasim_plugin_sdk.constants import (
    API_VERSION,
    BUILTIN_SIGNAL_TIMING,
    ENTRY_POINT_GROUP,
    REQUEST_SCHEMA,
    RESULT_SCHEMA,
    SDK_VERSION,
)
from terasim_plugin_sdk.errors import (
    ConformanceError,
    PluginError,
    PluginInputError,
    PluginNotFoundError,
    PluginRegistrationError,
    PluginRunError,
)
from terasim_plugin_sdk.model import (
    PLAN_KINDS,
    CorridorSelection,
    CurrentPlan,
    KpiWeights,
    MapInput,
    OperationRequest,
    OperationResult,
    TrafficSnapshot,
)
from terasim_plugin_sdk.protocol import (
    ProgressCallback,
    ProgressRelay,
    TrafficOperationPlugin,
    is_compatible_api_version,
)
from terasim_plugin_sdk.registry import (
    LoadError,
    PluginInfo,
    PluginRegistry,
    default_registry,
    get_plugin,
    list_plugins,
    register,
    reset_default_registry,
    run_operation,
)

__version__ = SDK_VERSION

__all__ = [
    "API_VERSION",
    "BUILTIN_SIGNAL_TIMING",
    "ConformanceError",
    "CorridorSelection",
    "CurrentPlan",
    "ENTRY_POINT_GROUP",
    "KpiWeights",
    "LoadError",
    "MapInput",
    "OperationRequest",
    "OperationResult",
    "PLAN_KINDS",
    "PluginError",
    "PluginInfo",
    "PluginInputError",
    "PluginNotFoundError",
    "PluginRegistrationError",
    "PluginRegistry",
    "PluginRunError",
    "ProgressCallback",
    "ProgressRelay",
    "REQUEST_SCHEMA",
    "RESULT_SCHEMA",
    "SDK_VERSION",
    "TrafficOperationPlugin",
    "TrafficSnapshot",
    "__version__",
    "default_registry",
    "get_plugin",
    "is_compatible_api_version",
    "list_plugins",
    "register",
    "reset_default_registry",
    "run_operation",
]
