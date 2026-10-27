"""The plugin protocol and the API-version compatibility rule."""

from __future__ import annotations

import inspect
import re
from typing import Callable, Optional, Protocol, runtime_checkable

from terasim_plugin_sdk.constants import API_VERSION, PLUGIN_NAME_PATTERN
from terasim_plugin_sdk.model import OperationRequest, OperationResult

__all__ = [
    "ProgressCallback",
    "ProgressRelay",
    "TrafficOperationPlugin",
    "is_compatible_api_version",
    "parse_api_version",
    "plugin_problems",
]

#: ``progress(done, total, label)``: called once with ``(0, total, None)`` before the
#: first evaluation and after every evaluated plan or batch, ``label`` naming the last
#: plan. ``total`` may shrink when a step is skipped. An exception raised by the
#: callback propagates unchanged and ends the run (that is how a caller cancels).
#: The same contract as the core's ``run_corridor_optimization`` progress callback.
ProgressCallback = Callable[[int, int, Optional[str]], None]

_NAME_RE = re.compile(PLUGIN_NAME_PATTERN)
_API_RE = re.compile(r"^(\d+)\.(\d+)$")


@runtime_checkable
class TrafficOperationPlugin(Protocol):
    """A Traffic Operation plug-in.

    Attributes:
        name: Registry name, a lowercase identifier (``[a-z][a-z0-9_]*``); it is
            also the entry-point name the plugin is published under.
        version: The plugin's own version (its distribution version is a good choice).
        api_version: The :data:`~terasim_plugin_sdk.API_VERSION` it was written against.

    An optional ``description`` attribute (one sentence) is shown by ``terasim-plugin list``.

    ``run`` receives a validated request whose paths are absolute and must:
    write files only under ``request.output_dir``; write the recommended plan to
    ``<output_dir>/nema_plan.json``; return an :class:`OperationResult`; raise
    :class:`~terasim_plugin_sdk.errors.PluginInputError` for requests it cannot
    serve and :class:`~terasim_plugin_sdk.errors.PluginRunError` when it ran but has
    no result; let exceptions of ``progress`` propagate unchanged; give the same
    result for the same request and seed. One plugin instance serves many runs, so
    it keeps no per-run state.
    """

    name: str
    version: str
    api_version: str

    def run(
        self, request: OperationRequest, progress: "ProgressCallback | None" = None
    ) -> OperationResult: ...


class ProgressRelay:
    """Forward progress to a caller's callback and remember what it raised.

    Wrap the caller's callback before handing it to code that converts errors,
    then re-raise ``relay.raised`` unchanged when it comes back::

        relay = ProgressRelay(progress)
        try:
            run_something(progress_callback=relay)
        except Exception as exc:
            if relay.is_cancellation(exc):
                raise
            raise PluginRunError(str(exc)) from exc
    """

    def __init__(self, callback: "ProgressCallback | None") -> None:
        self.callback = callback
        self.raised: "BaseException | None" = None

    def __call__(self, done: int, total: int, label: "str | None") -> None:
        if self.callback is None:
            return
        try:
            self.callback(done, total, label)
        except BaseException as exc:
            self.raised = exc
            raise

    def is_cancellation(self, exc: BaseException) -> bool:
        """True when ``exc`` is the exception the caller's callback raised."""
        return self.raised is not None and exc is self.raised


def parse_api_version(text) -> "tuple[int, int]":
    """``"MAJOR.MINOR"`` -> ``(major, minor)``; ValueError for anything else."""
    match = _API_RE.match(str(text)) if isinstance(text, str) else None
    if match is None:
        raise ValueError(f"api_version must look like 'MAJOR.MINOR', got {text!r}")
    return int(match.group(1)), int(match.group(2))


def is_compatible_api_version(api_version, *, sdk_api_version: str = API_VERSION) -> bool:
    """True when a plugin written against ``api_version`` runs on this SDK.

    Same MAJOR, and the plugin's MINOR not newer than the SDK's (a minor release only
    adds optional fields and helpers, so older plugins keep working).
    """
    try:
        major, minor = parse_api_version(api_version)
    except ValueError:
        return False
    sdk_major, sdk_minor = parse_api_version(sdk_api_version)
    return major == sdk_major and minor <= sdk_minor


def plugin_problems(plugin) -> "list[str]":
    """Why ``plugin`` is not a valid plugin object (empty list when it is)."""
    problems: list[str] = []
    name = getattr(plugin, "name", None)
    if not isinstance(name, str) or not _NAME_RE.match(name):
        problems.append(
            f"name must be a lowercase identifier matching {PLUGIN_NAME_PATTERN}, got {name!r}"
        )
    version = getattr(plugin, "version", None)
    if not isinstance(version, str) or not version.strip():
        problems.append(f"version must be a non-empty string, got {version!r}")
    api_version = getattr(plugin, "api_version", None)
    if not is_compatible_api_version(api_version):
        problems.append(
            f"api_version {api_version!r} is not compatible with the SDK's API_VERSION "
            f"{API_VERSION!r} (same MAJOR, MINOR not newer)"
        )
    description = getattr(plugin, "description", "")
    if description is not None and not isinstance(description, str):
        problems.append(f"description must be a string, got {type(description).__name__}")
    run = getattr(plugin, "run", None)
    if not callable(run):
        problems.append("run(request, progress=None) must be a method")
    else:
        try:
            signature = inspect.signature(run)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            try:
                signature.bind(object(), progress=None)
            except TypeError:
                problems.append("run must accept (request, progress=None)")
    return problems
