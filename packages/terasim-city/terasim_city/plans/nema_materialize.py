"""Materialize a NEMA plan (``terasim.nema_plan.v1``) into a SUMO NEMA tlLogic.

One coordination pattern becomes one ``<tlLogic type="NEMA">`` program
(``programID = pattern_id``). SUMO's NEMA controller runs the real dual-ring /
barrier structure, so protected lefts stay protected — no flattening.

Split → green: in coordinated mode SUMO reads ``maxDur`` as the phase's green,
so ``maxDur = split − yellow − red`` per phase; ``minDur`` = the phase's min
green. Per-phase ``yellow`` / ``red`` come straight from the plan (already
resolved per phase when the plan document is produced). The ``state`` string binds the
phase's movements to the net's controlled links via
:func:`terasim_city.plans.signal_groups.build_state`.

The core (:func:`expand_dual_ring_static`) takes pre-computed link movements and is
net-free (unit-testable); :func:`materialize_nema_tls` wraps it against a net.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lxml import etree as ET

logger = logging.getLogger(__name__)

from terasim_city.plans.signal_groups import (
    LinkMovement,
    build_state,
    controlled_link_count,
    derive_link_movements,
    yield_permissive_lefts,
)

DEFAULT_YELLOW_S = 3.0
DEFAULT_ALL_RED_S = 2.0
DEFAULT_MIN_GREEN_S = 5.0
DEFAULT_VEH_EXT_S = 2.0


def check_pattern_timing(intersection: dict, pattern: dict, *, tls_id: str = "") -> None:
    """Fail loud when a pattern's timing cannot be materialized.

    The SAME four rules gate both the plan-store ingest (so a broken upload is
    rejected at the door with a 400, not days later in a trial) and the two
    materialization paths:

    * the cycle must be positive,
    * every ring phase must have a definition in ``phases``,
    * every ring phase must have a split in the pattern,
    * every split must cover its minimum green plus the clearance tail.
    """
    where = tls_id or intersection.get("intersection_id") or "<intersection>"
    pattern_id = pattern.get("pattern_id", "<pattern>")
    cycle = float(pattern.get("cycle_s") or 0.0)
    if not cycle > 0.0:
        raise ValueError(f"{where}: pattern {pattern_id!r} cycle_s must be > 0")
    phase_meta = {p["phase"]: p for p in intersection.get("phases", [])}
    splits = pattern.get("splits", {})
    for pno in _phase_order(intersection):
        if pno not in phase_meta:
            raise ValueError(
                f"{where}: ring lists phase {pno} but the intersection defines "
                f"no such phase (phases: {sorted(phase_meta)})"
            )
        if str(pno) not in splits:
            raise ValueError(
                f"{where}: pattern {pattern_id!r} has no split for ring phase "
                f"{pno} — the plan's timing is incomplete"
            )
        meta = phase_meta[pno]
        floor = (
            float(meta.get("min_green_s", DEFAULT_MIN_GREEN_S))
            + float(meta.get("yellow_s", DEFAULT_YELLOW_S))
            + float(meta.get("all_red_s", DEFAULT_ALL_RED_S))
        )
        split = float(splits[str(pno)])
        # Half a 0.1s-grid step of tolerance: splits produced by grid-rounding
        # allocators (and float noise like 5+3+1.6 = 9.600000000000001) must
        # not be rejected, while any REAL violation (>= 0.1s short) still is.
        if split < floor - 0.05:
            raise ValueError(
                f"{where}: pattern {pattern_id!r} phase {pno} split {split:g}s "
                f"is below its minimum green + clearance floor ({floor:g}s)"
            )


def _phase_order(intersection: dict) -> list[int]:
    """Phases in ring order (ring1 then any ring2-only), skipping 0-padding.

    Only ring members are emitted — SUMO NEMA schedules by ring, so a phase not
    in either ring has no place in the program.
    """
    rings = intersection.get("rings", {})
    seen: list[int] = []
    for p in list(rings.get("ring1", [])) + list(rings.get("ring2", [])):
        if p and p not in seen:
            seen.append(p)
    return seen




def _require_full_coverage(
    intersection: dict, emitted: list[int], link_movements, tls_id: str
) -> None:
    """Every classified vehicle link must get green in SOME phase.

    A link that never turns green starves that approach (its traffic can never
    move, so any evaluation is meaningless) — and SUMO/libsumo are known to
    misbehave on such programs. The plan's phase movements must cover the
    junction.
    """
    served: set[str] = set()
    for ph in intersection["phases"]:
        if ph["phase"] in set(emitted):
            served.update(ph.get("movements", []))
    starving = sorted({lm.movement for lm in link_movements} - served)
    if starving:
        raise ValueError(
            f"{tls_id}: the plan gives NO green to movement(s) {starving} that "
            "exist at this junction — the plan does not fit the junction "
            "geometry. Typical fix: right turns ride with their approach's "
            "through phase (add e.g. 'EB-right' to the phase serving "
            "'EB-through')."
        )


def materialize_nema_tls(
    intersection: dict,
    pattern: dict,
    net_file: Path,
    tls_id: str,
    *,
    overrides: dict | None = None,
) -> ET._Element:
    """Bind a pattern to a net and build its tlLogic.

    Emits the exact fixed-time expansion of the dual-ring diagram
    (:func:`expand_dual_ring_static`) — timing-identical to the max-recall
    NEMA controller and safe on every junction, including the joined
    clusters SUMO's native controller crashes on.
    """
    link_movements = derive_link_movements(Path(net_file), tls_id, overrides=overrides)
    n_links = controlled_link_count(Path(net_file), tls_id)
    _require_full_coverage(
        intersection, _phase_order(intersection), link_movements, tls_id
    )
    return expand_dual_ring_static(
        intersection, pattern, link_movements, n_links, tls_id=tls_id
    )


def _ring_timeline(
    ring: list[int], splits: dict, phase_meta: dict
) -> list[tuple[float, float, int, str]]:
    """One ring's (t0, t1, phase, sub) segments; sub in 'G'/'y'/'r'.

    Each phase's split tiles into green -> yellow -> all-red. The ring may be
    shorter than the cycle (a side street with no cross-side phases rests red
    until the cycle wraps).
    """
    t = 0.0
    segments: list[tuple[float, float, int, str]] = []
    for pno in ring:
        meta = phase_meta[pno]
        split = float(splits[str(pno)])
        yellow = float(meta.get("yellow_s", DEFAULT_YELLOW_S))
        red = float(meta.get("all_red_s", DEFAULT_ALL_RED_S))
        green = max(split - yellow - red, 0.0)
        for sub, dur in (("G", green), ("y", yellow), ("r", red)):
            if dur > 0:
                segments.append((t, t + dur, pno, sub))
                t += dur
    return segments


def _active(segments, t: float):
    for a, b, pno, sub in segments:
        if a <= t < b:
            return pno, sub
    return None


def expand_dual_ring_static(
    intersection: dict,
    pattern: dict,
    link_movements: tuple[LinkMovement, ...],
    n_links: int,
    *,
    tls_id: str,
) -> ET._Element:
    """Exact fixed-time expansion of the dual-ring diagram as a static tlLogic.

    Under max-recall (no detectors) a coordinated NEMA plan is fully
    deterministic: both rings run their phase sequences with barrier-aligned
    durations. This replays that timeline second-for-second — every concurrent
    ring1/ring2 phase pair (including lead-lag protected lefts) keeps exactly
    its NEMA concurrency — and emits it as ``type="static"`` phases.

    Exists because SUMO's native NEMA controller (``NEMALogic::init``)
    segfaults on joined/cluster junctions (observed on SUMO 1.23.1 and 1.27);
    the expansion is timing-identical and runs on any junction.
    """
    phase_meta = {p["phase"]: p for p in intersection["phases"]}
    rings = intersection["rings"]
    splits = pattern.get("splits", {})
    cycle = float(pattern["cycle_s"])

    check_pattern_timing(intersection, pattern, tls_id=tls_id)

    ring1 = [p for p in rings.get("ring1", []) if p]
    ring2 = [p for p in rings.get("ring2", []) if p]
    segs1 = _ring_timeline(ring1, splits, phase_meta)
    segs2 = _ring_timeline(ring2, splits, phase_meta)

    vehicle_indices = {lm.link_index for lm in link_movements}
    breakpoints = sorted(
        {0.0, cycle}
        | {t for a, b, _, _ in segs1 + segs2 for t in (a, b) if 0.0 < t < cycle}
    )

    # A movement can ride phases in BOTH rings (dual-entry / overlap
    # movements). Its head is served while ANY active phase serves it, so ring
    # contributions merge by permissiveness — green wins over clearance wins
    # over red — instead of ring 2 simply overwriting ring 1 (which stamped a
    # served-green link yellow/red mid-window: G→y→r→G flicker).
    _SUB_PRIORITY = {"r": 0, "y": 1, "G": 2}

    def state_at(t: float) -> str:
        chars = ["r"] * n_links
        for segments in (segs1, segs2):
            hit = _active(segments, t)
            if hit is None:
                continue
            pno, sub = hit
            movements = set(phase_meta[pno].get("movements", []))
            for lm in link_movements:
                if lm.movement in movements:
                    cand = sub if sub in ("G", "y") else "r"
                    if _SUB_PRIORITY[cand] > _SUB_PRIORITY[chars[lm.link_index]]:
                        chars[lm.link_index] = cand
        # Permissive lefts yield ('G' -> 'g' while the opposing through is
        # green) — the shared NEMA permissive/protected rule.
        chars = list(yield_permissive_lefts("".join(chars), link_movements))
        for i in range(n_links):
            if i not in vehicle_indices:
                chars[i] = "G"  # pedestrian links stay permissive-green
        return "".join(chars)

    tl = ET.Element(
        "tlLogic",
        {
            "id": tls_id,
            "type": "static",
            "programID": pattern["pattern_id"],
            "offset": f"{float(pattern.get('offset_s', 0.0)):g}",
        },
    )
    # Collect state-change points, then round durations, DROP any that round to
    # zero, and fold the rounding residue into the last phase so the program
    # tiles the cycle exactly and never carries a zero-duration <phase> (which
    # SUMO rejects).
    changes: list[tuple[str, float]] = []  # (state, start)
    prev_state = None
    for a in breakpoints:
        state = state_at(a)
        if state != prev_state:
            changes.append((state, a))
            prev_state = state
    segments: list[list] = []  # [state, duration]
    for i, (state, start) in enumerate(changes):
        end = changes[i + 1][1] if i + 1 < len(changes) else cycle
        dur = round(end - start, 1)
        if dur <= 0:
            continue
        if segments and segments[-1][0] == state:
            segments[-1][1] = round(segments[-1][1] + dur, 1)
        else:
            segments.append([state, dur])
    if segments:
        residue = round(cycle - sum(d for _, d in segments), 1)
        segments[-1][1] = round(segments[-1][1] + residue, 1)
    for state, dur in segments:
        ET.SubElement(tl, "phase", {"duration": f"{dur:g}", "state": state})
    return tl


def write_nema_additional(tls_elements: list[ET._Element], out_path: Path) -> Path:
    """Write an ``<additional>`` file holding one NEMA tlLogic per intersection."""
    root = ET.Element("additional")
    for el in tls_elements:
        root.append(el)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(
        str(out_path), encoding="utf-8", xml_declaration=True, pretty_print=True
    )
    return out_path


__all__ = [
    "check_pattern_timing",
    "expand_dual_ring_static",
    "materialize_nema_tls",
    "write_nema_additional",
]
