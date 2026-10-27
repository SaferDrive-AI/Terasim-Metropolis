"""Vehicle factories and the vehicle-behavior registry.

The registry maps a behavior NAME (a plain string carried through task dicts
and configs) to a zero-argument callable producing a fresh
:class:`~terasim.vehicle.factories.vehicle_factory.VehicleFactory`. Evaluation
workers resolve the name in the child process, so behavior implementations
with heavy runtime requirements (e.g. CNDE inference) are only instantiated
where they actually run.

Registered names:

* ``"sumo_passive"`` — :class:`PassiveVehicleFactory`, SUMO's built-in driver
  behavior (the default everywhere).
* ``"cnde"`` — :class:`~terasim_city.factories.cnde_behavior.CndeVehicleFactory`,
  the CNDE neural-simulator background-vehicle behavior. Its module keeps
  torch/csim imports lazy (function-local, never at module level), so
  importing THIS module never pulls in a GPU runtime; only actually resolving
  and using the factory (:func:`resolve_vehicle_factory("cnde")`) does.
"""

from __future__ import annotations

from typing import Callable

from terasim_city.factories.cnde_behavior import CndeVehicleFactory
from terasim_city.factories.passive import PassiveVehicleFactory

__all__ = [
    "CndeVehicleFactory",
    "PassiveVehicleFactory",
    "VEHICLE_BEHAVIORS",
    "register_vehicle_behavior",
    "resolve_vehicle_factory",
]


#: Behavior name -> zero-argument callable returning a VehicleFactory.
VEHICLE_BEHAVIORS: "dict[str, Callable[[], object]]" = {
    "sumo_passive": PassiveVehicleFactory,
    "cnde": CndeVehicleFactory,
}


def register_vehicle_behavior(
    name: str,
    factory_callable: Callable[[], object],
    *,
    replace: bool = False,
) -> None:
    """Register ``factory_callable`` under ``name``.

    Raises:
        ValueError: ``name`` is already registered and ``replace`` is False.
    """
    if name in VEHICLE_BEHAVIORS and not replace:
        raise ValueError(
            f"vehicle behavior {name!r} is already registered; pass "
            "replace=True to override it"
        )
    VEHICLE_BEHAVIORS[name] = factory_callable


def resolve_vehicle_factory(name: str):
    """Return a FACTORY INSTANCE for the registered behavior ``name``.

    Raises:
        ValueError: ``name`` is not registered (message lists known names).
        Whatever the registered callable itself raises: a behavior that is
            only a fail-loud placeholder (none currently registered) would
            surface its own exception here.
    """
    try:
        factory_callable = VEHICLE_BEHAVIORS[name]
    except KeyError:
        raise ValueError(
            f"unknown vehicle behavior {name!r}; registered behaviors: "
            f"{sorted(VEHICLE_BEHAVIORS)}"
        ) from None
    return factory_callable()
