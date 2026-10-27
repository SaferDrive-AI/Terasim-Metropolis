"""Corridor resolution: user refs -> ordered SUMO traffic lights."""

from terasim_city.corridor.resolve import (
    ResolvedCorridor,
    ResolvedIntersection,
    resolve_corridor,
)

__all__ = [
    "ResolvedCorridor",
    "ResolvedIntersection",
    "resolve_corridor",
]
