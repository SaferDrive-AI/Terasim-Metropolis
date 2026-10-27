"""Shared builders for the corridor-demo signal plans.

Hardcoded equivalents of the ``alt_fixed`` programs in
``examples/corridor_demo/demo.tls.xml`` (three identical two-stage programs,
cycle 66 s, offset 0), so tests can build seed ``SignalPlan`` objects without
a tls.xml parser in the production package.
"""

from __future__ import annotations

from terasim_city.plans import PhaseSpec, SignalPlan

_ALT_FIXED_PHASES = (
    PhaseSpec(state="GGGgrrrGGGgrrr", duration_s=30.0, kind="green"),
    PhaseSpec(state="yyyyrrryyyyrrr", duration_s=3.0, kind="yellow"),
    PhaseSpec(state="rrrrGGgrrrrGGg", duration_s=30.0, kind="green"),
    PhaseSpec(state="rrrryyyrrrryyy", duration_s=3.0, kind="yellow"),
)


def demo_alt_fixed_plan(tls_id: str) -> SignalPlan:
    """The demo map's ``alt_fixed`` program for one of J1/J2/J3."""
    return SignalPlan(
        tls_id=tls_id,
        program_id="alt_fixed",
        offset_s=0.0,
        phases=_ALT_FIXED_PHASES,
    )
