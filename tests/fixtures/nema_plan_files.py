"""Synthetic ``terasim.nema_plan.v1`` documents for the test suite.

The JSON files live next to this module in ``nema_plans/``; every id, name,
timing value and schedule in them is invented for the tests:

* ``main_1st.nema_plan.json`` - one 8-phase dual-ring intersection
  (``MAIN ST @ 1ST AVE``) with protected lefts and full per-phase timing
  (min green / yellow / all-red), three coordinated 100 s patterns
  (``am_peak`` / ``midday_peak`` / ``pm_peak``, each with its own offset) and a
  weekday + weekend time-of-day schedule with Free evenings.
* ``four_phase.nema_plan.json`` - one 4-phase intersection (``PARK AVE @ OAK
  ST``): one phase per approach, lefts permissive, a single 80 s pattern.
* ``demo_arterial.nema_plan.json`` - the coordinated plan of the synthetic
  demo corridor (``examples/corridor_demo``): intersections ``i1``/``i2``/``i3``
  exactly on signals ``J1``/``J2``/``J3``. Three ring structures - ``i1`` full
  8-phase, ``i2`` 5-phase with a short ring 2 (one shared side-street phase),
  ``i3`` 6-phase with lead-lag arterial lefts and split side-street phases -
  each with ``am_peak`` (100 s) / ``midday`` (90 s) / ``pm_peak`` (110 s)
  coordinated patterns and a Free evening; ``i1`` adds two weekend patterns.

Weekday schedule of the demo corridor (all three intersections): 06:30
``am_peak``, 09:30 ``midday``, 15:30 ``pm_peak``, 19:00 Free. Weekend: ``i1``
08:00 ``weekend_day``, 16:00 ``weekend_evening``, 21:00 Free; ``i2``/``i3``
09:00 ``midday``, 20:00 Free.
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path

NEMA_PLANS_DIR = Path(__file__).resolve().parent / "nema_plans"
MAIN_1ST_PLAN = NEMA_PLANS_DIR / "main_1st.nema_plan.json"
FOUR_PHASE_PLAN = NEMA_PLANS_DIR / "four_phase.nema_plan.json"
DEMO_ARTERIAL_PLAN = NEMA_PLANS_DIR / "demo_arterial.nema_plan.json"


@lru_cache(maxsize=None)
def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_plan(path: Path) -> dict:
    """A fresh (deep) copy of one fixture document; callers may mutate it."""
    return copy.deepcopy(_read(Path(path)))


def plan_intersection(path: Path, intersection_id: str | None = None) -> dict:
    """One intersection of a fixture document (the first when no id is given)."""
    doc = load_plan(path)
    if intersection_id is None:
        return doc["intersections"][0]
    for inter in doc["intersections"]:
        if inter["intersection_id"] == intersection_id:
            return inter
    raise KeyError(f"{Path(path).name} has no intersection {intersection_id!r}")
