"""Plugin registry and the standard run harness.

A :class:`PluginRegistry` holds plugins by name from three sources:

* the built-in ``signal_timing`` plugin (always present unless ``builtins=False``);
* installed distributions that publish an entry point in the
  ``terasim.operation_plugins`` group (loaded once, on first use);
* explicit :meth:`PluginRegistry.register` calls.

Names are unique. An explicit registration of a taken name raises
:class:`~terasim_plugin_sdk.errors.PluginRegistrationError` unless
``replace=True``; an entry point whose name is taken, which fails to import, or
whose plugin is malformed or API-incompatible is skipped and recorded in
:attr:`PluginRegistry.load_errors` (with a logged warning), so one broken
distribution never hides the others.

:func:`run_operation` is the harness every caller uses (the CLI, the conformance
kit): it validates the request, runs the plugin, checks the result against the
contract and writes ``operation_request.json`` / ``operation_result.json``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from importlib import metadata
from typing import Iterator

from terasim_plugin_sdk.constants import (
    ENTRY_POINT_GROUP,
    PLUGIN_NAME_PATTERN,
    REQUEST_FILENAME,
)
from terasim_plugin_sdk.errors import (
    PluginError,
    PluginInputError,
    PluginNotFoundError,
    PluginRegistrationError,
    PluginRunError,
)
from terasim_plugin_sdk.model import OperationRequest, OperationResult
from terasim_plugin_sdk.protocol import ProgressCallback, ProgressRelay, plugin_problems

logger = logging.getLogger(__name__)

__all__ = [
    "LoadError",
    "PluginInfo",
    "PluginRegistry",
    "default_registry",
    "get_plugin",
    "list_plugins",
    "register",
    "reset_default_registry",
    "run_operation",
]

_NAME_RE = re.compile(PLUGIN_NAME_PATTERN)


@dataclass(frozen=True)
class PluginInfo:
    """What the registry knows about one registered plugin."""

    name: str
    version: str
    api_version: str
    description: str
    #: ``"builtin"``, ``"entry_point"`` or ``"explicit"``.
    source: str
    #: ``"<distribution> <version>"`` that provides it, when known.
    distribution: "str | None" = None
    #: The entry-point target (``"module:attribute"``) or the plugin's class path.
    target: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "api_version": self.api_version,
            "description": self.description,
            "source": self.source,
            "distribution": self.distribution,
            "target": self.target,
        }


@dataclass(frozen=True)
class LoadError:
    """An entry point the registry skipped, and why."""

    name: str
    target: str
    distribution: "str | None"
    error: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "target": self.target,
            "distribution": self.distribution,
            "error": self.error,
        }


def _class_path(plugin) -> str:
    cls = type(plugin)
    return f"{cls.__module__}:{cls.__qualname__}"


def _distribution_label(entry_point) -> "str | None":
    dist = getattr(entry_point, "dist", None)
    if dist is None:
        return None
    try:
        return f"{dist.metadata['Name']} {dist.version}"
    except Exception:  # noqa: BLE001 - a label only
        return None


def _instantiate(obj):
    """An entry point may name a plugin class, a zero-argument factory, or an instance."""
    if isinstance(obj, type):
        return obj()
    if hasattr(obj, "run"):
        return obj
    if callable(obj):
        return obj()
    return obj


def _group_entry_points():
    try:
        selected = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # very old importlib.metadata: dict API
        selected = metadata.entry_points().get(ENTRY_POINT_GROUP, ())
    return sorted(selected, key=lambda ep: (ep.name, ep.value))


class PluginRegistry:
    """Plugins by name: built-ins, entry points, explicit registrations."""

    def __init__(self, *, builtins: bool = True, entry_points: bool = True) -> None:
        self._plugins: dict[str, object] = {}
        self._info: dict[str, PluginInfo] = {}
        self._load_errors: list[LoadError] = []
        self._discovered = not entry_points
        if builtins:
            from terasim_plugin_sdk.builtin import SignalTimingPlugin

            self._add(SignalTimingPlugin(), source="builtin", distribution="terasim-plugin-sdk")

    # -- registration ---------------------------------------------------------

    def register(self, plugin, *, replace: bool = False) -> PluginInfo:
        """Register a plugin object under its ``name``.

        Raises:
            PluginRegistrationError: The object is not a valid plugin (see
                :func:`~terasim_plugin_sdk.protocol.plugin_problems`), or the name is
                taken and ``replace`` is False.
        """
        self.discover()
        return self._add(plugin, source="explicit", replace=replace)

    def unregister(self, name: str) -> None:
        self.discover()
        if name not in self._plugins:
            raise PluginNotFoundError(name, self._plugins)
        del self._plugins[name]
        del self._info[name]

    def _add(
        self,
        plugin,
        *,
        source: str,
        distribution: "str | None" = None,
        target: "str | None" = None,
        replace: bool = False,
    ) -> PluginInfo:
        problems = plugin_problems(plugin)
        if problems:
            label = getattr(plugin, "name", None) or _class_path(plugin)
            raise PluginRegistrationError(f"invalid plugin {label!r}: " + "; ".join(problems))
        name = plugin.name
        if name in self._plugins and not replace:
            existing = self._info[name]
            raise PluginRegistrationError(
                f"a plugin named {name!r} is already registered ({existing.source}"
                f"{', ' + existing.distribution if existing.distribution else ''}); "
                "pass replace=True to override it"
            )
        info = PluginInfo(
            name=name,
            version=plugin.version,
            api_version=plugin.api_version,
            description=str(getattr(plugin, "description", "") or ""),
            source=source,
            distribution=distribution,
            target=target or _class_path(plugin),
        )
        self._plugins[name] = plugin
        self._info[name] = info
        return info

    # -- entry points -----------------------------------------------------------

    def discover(self, *, force: bool = False) -> None:
        """Load the ``terasim.operation_plugins`` entry points (once; ``force`` reloads)."""
        if self._discovered and not force:
            return
        self._discovered = True
        if force:
            for name in [n for n, info in self._info.items() if info.source == "entry_point"]:
                del self._plugins[name]
                del self._info[name]
            self._load_errors = []
        for entry_point in _group_entry_points():
            self._load_entry_point(entry_point)

    def _load_entry_point(self, entry_point) -> None:
        distribution = _distribution_label(entry_point)

        def skip(reason: str) -> None:
            error = LoadError(entry_point.name, entry_point.value, distribution, reason)
            self._load_errors.append(error)
            logger.warning(
                "skipping operation plugin entry point %r (%s) from %s: %s",
                entry_point.name,
                entry_point.value,
                distribution or "an unknown distribution",
                reason,
            )

        if not _NAME_RE.match(entry_point.name):
            skip(f"entry point name must match {PLUGIN_NAME_PATTERN}")
            return
        if entry_point.name in self._plugins:
            existing = self._info[entry_point.name]
            skip(
                f"duplicate plugin name: {entry_point.name!r} is already provided by "
                f"{existing.distribution or existing.source} ({existing.target})"
            )
            return
        try:
            plugin = _instantiate(entry_point.load())
        except Exception as exc:  # noqa: BLE001 - a broken distribution must not hide the others
            skip(f"could not load it: {type(exc).__name__}: {exc}")
            return
        if getattr(plugin, "name", None) != entry_point.name:
            skip(
                f"the entry point name {entry_point.name!r} differs from the plugin's name "
                f"{getattr(plugin, 'name', None)!r}"
            )
            return
        try:
            self._add(
                plugin, source="entry_point", distribution=distribution, target=entry_point.value
            )
        except PluginRegistrationError as exc:
            skip(str(exc))

    @property
    def load_errors(self) -> "tuple[LoadError, ...]":
        self.discover()
        return tuple(self._load_errors)

    # -- lookup -------------------------------------------------------------------

    def get(self, name: str):
        """The plugin registered as ``name`` (PluginNotFoundError lists the known names)."""
        self.discover()
        try:
            return self._plugins[name]
        except KeyError:
            raise PluginNotFoundError(name, self._plugins) from None

    def info(self, name: str) -> PluginInfo:
        self.get(name)
        return self._info[name]

    def names(self) -> "list[str]":
        self.discover()
        return sorted(self._plugins)

    def infos(self) -> "list[PluginInfo]":
        self.discover()
        return [self._info[name] for name in sorted(self._info)]

    def __contains__(self, name: object) -> bool:
        self.discover()
        return name in self._plugins

    def __iter__(self) -> Iterator[str]:
        return iter(self.names())

    def __len__(self) -> int:
        self.discover()
        return len(self._plugins)

    def run(
        self,
        name: str,
        request: OperationRequest,
        *,
        progress: "ProgressCallback | None" = None,
        check: bool = True,
    ) -> OperationResult:
        """:func:`run_operation` with a plugin from this registry."""
        return run_operation(name, request, progress=progress, registry=self, check=check)


# ---------------------------------------------------------------------------
# Process-wide default registry
# ---------------------------------------------------------------------------

_DEFAULT: "PluginRegistry | None" = None


def default_registry() -> PluginRegistry:
    """The process-wide registry (built-ins + entry points), created on first use."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = PluginRegistry()
    return _DEFAULT


def reset_default_registry() -> None:
    """Forget the process-wide registry; the next use rediscovers the entry points."""
    global _DEFAULT
    _DEFAULT = None


def register(plugin, *, replace: bool = False) -> PluginInfo:
    """Register ``plugin`` in the process-wide registry."""
    return default_registry().register(plugin, replace=replace)


def get_plugin(name: str):
    """The plugin named ``name`` in the process-wide registry."""
    return default_registry().get(name)


def list_plugins() -> "list[PluginInfo]":
    """Every plugin of the process-wide registry, sorted by name."""
    return default_registry().infos()


# ---------------------------------------------------------------------------
# Run harness
# ---------------------------------------------------------------------------


def run_operation(
    plugin,
    request: OperationRequest,
    *,
    progress: "ProgressCallback | None" = None,
    registry: "PluginRegistry | None" = None,
    check: bool = True,
) -> OperationResult:
    """Run one plugin on one request.

    ``plugin`` is a registered name (looked up in ``registry``, default the
    process-wide one) or a plugin object. The harness:

    1. makes the request's paths absolute and checks its files exist;
    2. creates ``output_dir`` and writes ``operation_request.json``;
    3. calls ``plugin.run(request, progress)``;
    4. checks the result against the result contract (unless ``check=False``);
    5. writes ``operation_result.json`` and returns the result.

    Raises:
        PluginNotFoundError: Unknown plugin name.
        PluginRegistrationError: ``plugin`` is not a valid plugin object.
        PluginInputError / PluginRunError: As raised by the plugin. Any other
            exception from the plugin is wrapped in PluginRunError.
        ConformanceError: The result breaks the contract.

    Exceptions raised by ``progress`` propagate unchanged.
    """
    if isinstance(plugin, str):
        plugin = (registry or default_registry()).get(plugin)
    else:
        problems = plugin_problems(plugin)
        if problems:
            raise PluginRegistrationError(
                f"invalid plugin {getattr(plugin, 'name', None)!r}: " + "; ".join(problems)
            )
    if not isinstance(request, OperationRequest):
        raise PluginInputError(f"request must be an OperationRequest, got {type(request).__name__}")
    request = request.resolved()
    request.check_inputs()
    request.output_dir.mkdir(parents=True, exist_ok=True)
    (request.output_dir / REQUEST_FILENAME).write_text(
        json.dumps(request.to_dict(), indent=2) + "\n", encoding="utf-8"
    )

    relay = ProgressRelay(progress)
    try:
        result = plugin.run(request, progress=relay if progress is not None else None)
    except PluginError:
        raise
    except Exception as exc:
        if relay.is_cancellation(exc):
            raise
        raise PluginRunError(f"plugin {plugin.name!r} failed: {type(exc).__name__}: {exc}") from exc
    if not isinstance(result, OperationResult):
        raise PluginRunError(
            f"plugin {plugin.name!r} returned {type(result).__name__}, not an OperationResult"
        )
    if check:
        from terasim_plugin_sdk.conformance import check_result

        check_result(result, request)
    result.write()
    return result
