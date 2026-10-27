"""Exceptions of the Plugin Interface (the error contract).

Every exception the SDK raises on purpose derives from :class:`PluginError`:

* :class:`PluginInputError` (also a ``ValueError``): the request cannot run as
  given, e.g. invalid KPI weights, a missing map or TMC file, an unknown
  plugin parameter, a corridor the map does not contain.
* :class:`PluginRunError` (also a ``RuntimeError``): the request was valid but
  the operation produced no usable result, e.g. no plan simulated validly, or
  the recommended plan cannot be written as NEMA timing.
* :class:`PluginNotFoundError` (also a ``LookupError``): no plugin of that name
  is registered.
* :class:`PluginRegistrationError`: a plugin object is malformed, declares an
  incompatible ``api_version`` or reuses a registered name.
* :class:`ConformanceError`: a result breaks the result contract; its
  ``problems`` list says how.

An exception raised by the caller's progress callback is not part of this
contract: it propagates unchanged (it is how a caller cancels a run).
"""

from __future__ import annotations

from typing import Iterable

__all__ = [
    "ConformanceError",
    "PluginError",
    "PluginInputError",
    "PluginNotFoundError",
    "PluginRegistrationError",
    "PluginRunError",
]


class PluginError(Exception):
    """Base class of every error the Plugin Interface defines."""


class PluginInputError(PluginError, ValueError):
    """The request is invalid or refers to inputs that do not exist."""


class PluginRunError(PluginError, RuntimeError):
    """The operation ran but could not produce a result."""


class PluginNotFoundError(PluginError, LookupError):
    """No plugin is registered under the requested name."""

    def __init__(self, name: str, known: Iterable[str] = ()) -> None:
        self.name = name
        self.known = tuple(sorted(known))
        listing = ", ".join(self.known) or "(none)"
        super().__init__(f"no operation plugin named {name!r}; registered plugins: {listing}")


class PluginRegistrationError(PluginError):
    """A plugin cannot be registered (malformed, incompatible or duplicate)."""


class ConformanceError(PluginError):
    """A plugin result does not satisfy the result contract."""

    def __init__(self, problems: Iterable[str], *, subject: str = "operation result") -> None:
        self.problems = tuple(problems)
        lines = "\n".join(f"  - {problem}" for problem in self.problems)
        super().__init__(f"{subject} is not conformant ({len(self.problems)} problem(s)):\n{lines}")
