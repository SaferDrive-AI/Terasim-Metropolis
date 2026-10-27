"""Measures-of-effectiveness (MOE) extraction and KPI scoring.

``extract_moe`` reads a SUMO ``tripinfo.xml`` produced with
``--tripinfo-output.write-unfinished true`` and
``--tripinfo-output.write-undeparted true``, so the returned means cover ALL
demand — completed, unfinished (``arrival="-1.00"``) and undeparted
(``depart="-1"``) vehicles. This is the required MOE input of the corridor
optimization pipeline: unlike the removed legacy tripinfo summary (which
silently returns empty aggregates), a missing/empty/unparseable file raises
``ValueError`` because a plan without an MOE must never score as valid.

``KpiConfig`` + ``score_moe`` turn an MOE dict into a single lower-is-better
score as a weighted sum of baseline ratios; a plan identical to the baseline
scores ~= sum of the weights.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

__all__ = ["extract_moe", "KpiConfig", "score_moe", "EPS"]

# Guard for baseline metrics that are exactly zero (e.g. a run with no stops):
# the ratio denominator is max(baseline_metric, EPS) so scores stay finite.
EPS = 1e-6

# KpiConfig weight key -> MOE dict key it applies to.
_WEIGHT_METRICS: dict[str, str] = {
    "control_delay": "control_delay_s_per_veh",
    "stops": "stops_per_veh",
}


def _float_attr(attrib: Mapping[str, str], name: str, *, path: Path, veh_id: str) -> float:
    """Parse an optional float attribute; absent -> 0.0, malformed -> ValueError."""
    raw = attrib.get(name)
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{path}: tripinfo id={veh_id!r} has non-numeric {name}={raw!r}"
        ) from exc


def extract_moe(tripinfo_path: Path) -> dict:
    """Extract corridor MOEs from a tripinfo.xml covering all demand.

    Per-vehicle delay = ``timeLoss`` + ``departDelay``. Missing ``timeLoss``
    or ``waitingCount`` counts as 0.0 on any row, but an undeparted row
    (``depart`` < 0) MISSING the ``departDelay`` attribute raises ValueError:
    real SUMO always writes ``departDelay`` on
    ``--tripinfo-output.write-undeparted`` rows, so its absence means a
    hand-rolled or truncated file, and silently zero-filling it would let a
    gamed file erase the delay penalty of held-back demand.

    Stops per vehicle = ``waitingCount``, except undeparted rows contribute
    ``max(waitingCount, 1)`` stops. Anti-gaming rationale: SUMO reports
    ``waitingCount=0`` for vehicles never inserted, yet a vehicle held at the
    insertion gate is physically standing for the whole run. Counting it as 0
    would let a plan that starves the network (gate-holding) DILUTE
    ``stops_per_veh`` below the baseline and win the stops axis; counting at
    least one stop per undeparted vehicle makes gate-holding strictly worse
    than serving the demand.

    Besides the count buckets, the returned dict carries ``departed_ratio``
    (= (total - undeparted) / total) and ``completion_ratio``
    (= completed / total) so downstream validity gates can reason about how
    much of the demand was actually served.

    Means are over ALL vehicles including unfinished and undeparted rows.

    Raises ValueError when the file is missing, empty, unparseable, or
    contains no ``<tripinfo>`` entries: this file is a required output of the
    optimization pipeline, so "no data" must fail the trial, not zero-fill it.
    """
    tripinfo_path = Path(tripinfo_path)
    if not tripinfo_path.exists():
        raise ValueError(
            f"tripinfo file not found: {tripinfo_path}. The trial run must be "
            "started with --tripinfo-output (plus write-unfinished/write-undeparted)."
        )
    try:
        root = ET.parse(tripinfo_path).getroot()
    except ET.ParseError as exc:
        raise ValueError(
            f"{tripinfo_path}: not parseable as tripinfo XML ({exc}). The run "
            "likely crashed or was truncated."
        ) from exc

    trips = root.findall("tripinfo")
    if not trips:
        raise ValueError(
            f"{tripinfo_path}: contains no <tripinfo> entries. Even undeparted "
            "vehicles produce rows under --tripinfo-output.write-undeparted, so "
            "an entry-less file means the run had no demand at all."
        )

    total = len(trips)
    completed = 0
    unfinished = 0
    undeparted = 0
    total_delay = 0.0
    total_stops = 0
    total_time_loss = 0.0
    total_depart_delay = 0.0

    for trip in trips:
        attrib = trip.attrib
        veh_id = attrib.get("id", "<no id>")
        depart = _float_attr(attrib, "depart", path=tripinfo_path, veh_id=veh_id)
        arrival = _float_attr(attrib, "arrival", path=tripinfo_path, veh_id=veh_id)
        is_undeparted = depart < 0
        if is_undeparted:
            undeparted += 1
            if "departDelay" not in attrib:
                raise ValueError(
                    f"{tripinfo_path}: undeparted tripinfo id={veh_id!r} is "
                    "missing the departDelay attribute. SUMO always writes "
                    "departDelay on write-undeparted rows, so its absence "
                    "means a hand-rolled or truncated file."
                )
        elif arrival >= 0:
            completed += 1
        else:
            unfinished += 1

        time_loss = _float_attr(attrib, "timeLoss", path=tripinfo_path, veh_id=veh_id)
        depart_delay = _float_attr(
            attrib, "departDelay", path=tripinfo_path, veh_id=veh_id
        )
        stops = int(
            _float_attr(attrib, "waitingCount", path=tripinfo_path, veh_id=veh_id)
        )
        if is_undeparted:
            # A vehicle held at the insertion gate always has waitingCount=0
            # in SUMO, yet it is standing the whole run: count at least one
            # stop so gate-holding cannot dilute stops_per_veh (see docstring).
            stops = max(stops, 1)

        total_delay += time_loss + depart_delay
        total_time_loss += time_loss
        total_depart_delay += depart_delay
        total_stops += stops

    return {
        "control_delay_s_per_veh": total_delay / total,
        "stops_per_veh": total_stops / total,
        "vehicles_total": total,
        "vehicles_completed": completed,
        "vehicles_unfinished": unfinished,
        "vehicles_undeparted": undeparted,
        "departed_ratio": (total - undeparted) / total,
        "completion_ratio": completed / total,
        "total_delay_s": total_delay,
        "total_stops": total_stops,
        "mean_time_loss_s": total_time_loss / total,
        "mean_depart_delay_s": total_depart_delay / total,
    }


@dataclass(frozen=True)
class KpiConfig:
    """Weighted-metric configuration for :func:`score_moe`.

    ``weights`` maps metric names (``"control_delay"`` ->
    ``moe["control_delay_s_per_veh"]``, ``"stops"`` -> ``moe["stops_per_veh"]``)
    to strictly positive weights. Unknown names, non-positive/non-numeric
    weights, duplicates, or an empty mapping raise ValueError.
    """

    weights: tuple[tuple[str, float], ...] = (
        ("control_delay", 0.7),
        ("stops", 0.3),
    )

    def __post_init__(self) -> None:
        normalized: list[tuple[str, float]] = []
        seen: set[str] = set()
        for entry in self.weights:
            name, weight = entry
            if name not in _WEIGHT_METRICS:
                raise ValueError(
                    f"KpiConfig: unknown weight key {name!r}; "
                    f"known keys: {sorted(_WEIGHT_METRICS)}"
                )
            if name in seen:
                raise ValueError(f"KpiConfig: duplicate weight key {name!r}")
            seen.add(name)
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise ValueError(
                    f"KpiConfig: weight for {name!r} must be a number, "
                    f"got {weight!r}"
                )
            weight = float(weight)
            if not weight > 0:
                raise ValueError(
                    f"KpiConfig: weight for {name!r} must be > 0, got {weight}"
                )
            normalized.append((name, weight))
        if not normalized:
            raise ValueError("KpiConfig: weights must not be empty")
        object.__setattr__(self, "weights", tuple(normalized))

    @classmethod
    def from_dict(cls, weights: Mapping[str, float]) -> "KpiConfig":
        return cls(weights=tuple((str(k), v) for k, v in weights.items()))

    def to_dict(self) -> dict[str, float]:
        return dict(self.weights)


def score_moe(moe: dict, baseline_moe: dict, cfg: KpiConfig) -> float:
    """Weighted baseline-ratio score; lower is better.

    score = sum over cfg.weights of w * (metric / max(baseline_metric, EPS)).
    Scoring the baseline against itself yields ~ sum of the weights. A metric
    missing from either MOE dict raises ValueError (an MOE produced by
    :func:`extract_moe` always carries both keys, so absence means a caller
    bug, not "zero").
    """
    score = 0.0
    for name, weight in cfg.weights:
        metric_key = _WEIGHT_METRICS[name]
        for label, payload in (("moe", moe), ("baseline_moe", baseline_moe)):
            if metric_key not in payload:
                raise ValueError(
                    f"score_moe: {label} is missing {metric_key!r} "
                    f"(required by weight {name!r})"
                )
        score += weight * (
            float(moe[metric_key]) / max(float(baseline_moe[metric_key]), EPS)
        )
    return score
