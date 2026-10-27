"""Map SUMO TLS link indices to NEMA-style movement labels (``EB-through`` etc.).

This is the bridge between the movement vocabulary of traffic engineering
(timing sheets, TMC counts, NEMA phase tables) and SUMO's positional state
strings: every controlled connection of a TLS gets classified as one of
:data:`terasim_city.plans.model.MOVEMENTS`, and :func:`build_state` turns a set
of green movements back into a SUMO state string over the link indices.

Classification contract:

* The approach cardinal is derived from the travel heading of the incoming
  edge as it arrives at the junction (an edge whose travel direction points
  east carries EB traffic).
* The turn comes from the SUMO connection ``dir`` attribute (``s`` through,
  ``l``/``L`` left, ``r``/``R`` right, ``t`` U-turn folded into left); when the
  attribute is absent or unrecognised the signed in/out turn angle decides.
* Explicit per-link overrides always win, so geometry oddities (skewed
  approaches, channelised turns) can be corrected via a small JSON file
  instead of code changes.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as _ET
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Mapping, Sequence

import sumolib

from terasim_city.flow.network.matcher import (
    classify_turn_by_angle,
    edge_heading_into_node,
    edge_heading_out_of_node,
    nearest_cardinal,
    signed_turn,
)
from terasim_city.plans.model import MOVEMENTS

#: SUMO connection ``dir`` attribute -> turn label. U-turns (``t``) are folded
#: into left because the movement vocabulary does not model them separately.
_DIRECTION_TO_TURN: dict[str, str] = {
    "s": "through",
    "l": "left",
    "L": "left",
    "r": "right",
    "R": "right",
    "t": "left",
}

_OVERRIDE_HINT = (
    "supply a signal_groups.json override "
    '({"<tls_id>": {"<link_index>": "<MOVEMENT>"}}) to disambiguate'
)


@dataclass(frozen=True)
class LinkMovement:
    """One controlled TLS link index and the movement it serves."""

    link_index: int
    movement: str

    def __post_init__(self) -> None:
        index = int(self.link_index)
        if index < 0:
            raise ValueError(f"LinkMovement.link_index must be >= 0, got {self.link_index!r}")
        object.__setattr__(self, "link_index", index)
        if self.movement not in MOVEMENTS:
            raise ValueError(
                f"LinkMovement.movement must be one of {list(MOVEMENTS)}, "
                f"got {self.movement!r}"
            )


def derive_link_movements(
    net_file: Path,
    tls_id: str,
    *,
    overrides: Mapping[int, str] | None = None,
) -> tuple[LinkMovement, ...]:
    """Classify every VEHICLE-controlled link of ``tls_id`` into a movement label.

    Returns one :class:`LinkMovement` per distinct link index, sorted by link
    index. Two connections sharing a link index must agree on the movement
    (they are signalled together, so a disagreement means the automatic
    classification is unreliable) — otherwise a ValueError asks for an
    override. Overrides referencing link indices the TLS does not control are
    rejected rather than silently ignored.

    IMPORTANT: on real (cluster) junctions the TLS's controlled-link set is a
    SUPERSET of the vehicle connections returned here — pedestrian crossings
    also occupy link indices but sumolib's ``tls.getConnections()`` does not
    report them, so the returned indices are neither guaranteed dense nor
    complete. Callers building full SUMO state strings must size them from
    :func:`controlled_link_count` (the net's own ground truth) and pass that
    as ``n_links`` to :func:`build_state`; unclassifiable indices are padded
    red.
    """
    net_file = Path(net_file)
    if not net_file.exists():
        raise FileNotFoundError(f"net file not found: {net_file}")
    net = sumolib.net.readNet(str(net_file))
    try:
        tls = net.getTLS(tls_id)
    except Exception:
        available = sorted(t.getID() for t in net.getTrafficLights())
        raise ValueError(
            f"{net_file}: TLS {tls_id!r} not found; available TLS ids: {available}"
        ) from None

    normalized_overrides = _normalize_overrides(overrides, tls_id=tls_id)

    connections = sorted(tls.getConnections(), key=lambda item: int(item[2]))
    if not connections:
        raise ValueError(f"{net_file}: TLS {tls_id!r} controls no connections")

    by_index: dict[int, str] = {}
    for in_lane, out_lane, raw_index in connections:
        index = int(raw_index)
        if index in normalized_overrides:
            movement = normalized_overrides[index]
        else:
            movement = _classify_connection(
                in_lane, out_lane, index, net_file=net_file, tls_id=tls_id
            )
        previous = by_index.get(index)
        if previous is not None and previous != movement:
            raise ValueError(
                f"{net_file}: TLS {tls_id!r} link {index} classifies ambiguously "
                f"({previous!r} vs {movement!r}); {_OVERRIDE_HINT}"
            )
        by_index[index] = movement

    unknown_override_links = sorted(set(normalized_overrides) - set(by_index))
    if unknown_override_links:
        raise ValueError(
            f"overrides for TLS {tls_id!r} reference link index(es) "
            f"{unknown_override_links} not controlled by that TLS "
            f"(controlled: {sorted(by_index)})"
        )

    return tuple(LinkMovement(index, by_index[index]) for index in sorted(by_index))


def approach_lane_counts(
    net_file: Path,
    tls_id: str,
    *,
    overrides: Mapping[int, str] | None = None,
) -> dict[str, int]:
    """Number of distinct incoming lanes per approach travel direction.

    Same classification as :func:`derive_link_movements`; the lane count feeds
    the per-approach saturation flow (lanes x 1800 veh/h). A lane that serves
    connections of two classified directions (e.g. after an override) counts
    for both.
    """
    net_file = Path(net_file)
    net = sumolib.net.readNet(str(net_file))
    try:
        tls = net.getTLS(tls_id)
    except Exception:
        available = sorted(t.getID() for t in net.getTrafficLights())
        raise ValueError(
            f"{net_file}: TLS {tls_id!r} not found; available TLS ids: {available}"
        ) from None
    normalized = _normalize_overrides(overrides, tls_id=tls_id)
    lanes_by_direction: dict[str, set[str]] = {}
    for in_lane, out_lane, raw_index in tls.getConnections():
        index = int(raw_index)
        movement = normalized.get(index) or _classify_connection(
            in_lane, out_lane, index, net_file=net_file, tls_id=tls_id
        )
        direction = movement.split("-", 1)[0]
        lanes_by_direction.setdefault(direction, set()).add(in_lane.getID())
    return {direction: len(ids) for direction, ids in lanes_by_direction.items()}


def controlled_link_count(net_file: Path, tls_id: str) -> int:
    """Number of controlled links of ``tls_id`` per the NET's own tlLogic.

    This is the ground truth SUMO validates state strings against ('Mismatching
    phase size'): the length of any ``<phase state=...>`` of the net's
    ``<tlLogic id=tls_id>``. The first program found is used — all programs of
    one TLS share the state length. Every net TLS carries at least one program,
    so a missing tlLogic means ``tls_id`` is simply not a TLS of this net ->
    ValueError naming the net and the tls.

    Note this can EXCEED the number of vehicle connections returned by
    :func:`derive_link_movements`: pedestrian crossings occupy link indices
    too but are not vehicle movements.
    """
    net_file = Path(net_file)
    if not net_file.exists():
        raise FileNotFoundError(f"net file not found: {net_file}")
    for _event, element in _ET.iterparse(str(net_file)):
        if element.tag != "tlLogic" or element.attrib.get("id") != tls_id:
            continue
        phase = element.find("phase")
        state = phase.attrib.get("state") if phase is not None else None
        if not state:
            raise ValueError(
                f"{net_file}: tlLogic {tls_id!r} (program "
                f"{element.attrib.get('programID')!r}) has no phase state"
            )
        return len(state)
    raise ValueError(
        f"{net_file}: no <tlLogic> found for TLS {tls_id!r}; the net does not "
        "control this id (controllability comes from the net's own tlLogic)"
    )


def build_state(
    link_movements: Sequence[LinkMovement],
    green_movements: Collection[str],
    *,
    protected: bool = True,
    n_links: int | None = None,
) -> str:
    """Build a SUMO state string giving green to ``green_movements``.

    With ``n_links`` given the string has EXACTLY that length — the net's
    controlled-link count (see :func:`controlled_link_count`); indices beyond
    the vehicle links in ``link_movements`` (pedestrian crossings etc.) are
    padded ``'r'``, which is safe for generated demand without pedestrians. A
    ``link_movements`` index ``>= n_links`` means the inputs are inconsistent
    (link movements from a different net than the count) -> ValueError.

    With ``n_links=None`` (default) the string covers link indices
    ``0 .. max(link_index)`` — correct only when the TLS controls no links
    beyond the classifiable vehicle connections. Indices absent from
    ``link_movements`` render as ``'r'``. Green links render ``'G'``
    (protected) or ``'g'`` when ``protected=False``.
    """
    movements = tuple(link_movements)
    if not movements:
        raise ValueError("build_state requires at least one LinkMovement")
    greens = {str(m) for m in green_movements}
    unknown = sorted(m for m in greens if m not in MOVEMENTS)
    if unknown:
        raise ValueError(
            f"green_movements contains unknown movement(s) {unknown}; "
            f"allowed: {list(MOVEMENTS)}"
        )
    max_index = max(lm.link_index for lm in movements)
    if n_links is None:
        length = max_index + 1
    else:
        length = int(n_links)
        if max_index >= length:
            raise ValueError(
                f"build_state: link_movements reference link index {max_index} "
                f"but n_links is {length}; link movements and the controlled-"
                "link count disagree (inconsistent inputs)"
            )
    green_char = "G" if protected else "g"
    chars = ["r"] * length
    for lm in movements:
        if lm.movement in greens:
            chars[lm.link_index] = green_char
    return "".join(chars)


#: Opposing approach per cardinal, for the permissive-left rule.
_OPPOSITE: dict[str, str] = {"EB": "WB", "WB": "EB", "NB": "SB", "SB": "NB"}


def yield_permissive_lefts(
    state: str, link_movements: Sequence[LinkMovement]
) -> str:
    """Demote lefts that face a concurrent opposing through: ``'G'`` -> ``'g'``.

    A left turn that is green WHILE its opposing through is also green is a
    permissive left and must yield (SUMO ``'g'``); a protected left (opposing
    through red) keeps ``'G'``. This is exactly the NEMA permissive/protected
    distinction — shared by every state-string producer so a flattened
    two-stage plan and the dual-ring expansion give lefts the same semantics.
    Links not covered by ``link_movements`` (pedestrian padding etc.) are left
    untouched.
    """
    chars = list(state)
    green_movements = {
        lm.movement
        for lm in link_movements
        if lm.link_index < len(chars) and chars[lm.link_index] == "G"
    }
    for lm in link_movements:
        direction, _, turn = lm.movement.partition("-")
        if (
            turn == "left"
            and lm.link_index < len(chars)
            and chars[lm.link_index] == "G"
            and f"{_OPPOSITE[direction]}-through" in green_movements
        ):
            chars[lm.link_index] = "g"
    return "".join(chars)


def load_overrides(path: Path) -> dict[str, dict[int, str]]:
    """Load per-TLS link-movement overrides from JSON.

    File shape: ``{"<tls_id>": {"<link_index>": "NB-left", ...}, ...}``. Link
    indices must parse as non-negative integers and movements must come from
    :data:`MOVEMENTS`; anything else raises with the offending file/field.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"signal-group overrides file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(
            f"{path}: expected a JSON object mapping tls_id -> overrides, "
            f"got {type(payload).__name__}"
        )
    result: dict[str, dict[int, str]] = {}
    for tls_id, raw_overrides in payload.items():
        if not isinstance(raw_overrides, dict):
            raise ValueError(
                f"{path}: overrides for tls {tls_id!r} must be an object mapping "
                f"link_index -> movement, got {type(raw_overrides).__name__}"
            )
        per_tls: dict[int, str] = {}
        for raw_index, movement in raw_overrides.items():
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                raise ValueError(
                    f"{path}: tls {tls_id!r} has non-integer link index {raw_index!r}"
                ) from None
            if index < 0:
                raise ValueError(
                    f"{path}: tls {tls_id!r} has negative link index {index}"
                )
            if movement not in MOVEMENTS:
                raise ValueError(
                    f"{path}: tls {tls_id!r} link {raw_index!r} has unknown movement "
                    f"{movement!r}; allowed: {list(MOVEMENTS)}"
                )
            per_tls[index] = str(movement)
        result[str(tls_id)] = per_tls
    return result


def _normalize_overrides(
    overrides: Mapping[int, str] | None, *, tls_id: str
) -> dict[int, str]:
    """Validate an in-memory overrides mapping (same rules as the JSON file)."""
    if not overrides:
        return {}
    normalized: dict[int, str] = {}
    for raw_index, movement in overrides.items():
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            raise ValueError(
                f"overrides for TLS {tls_id!r} have non-integer link index {raw_index!r}"
            ) from None
        if index < 0:
            raise ValueError(
                f"overrides for TLS {tls_id!r} have negative link index {index}"
            )
        if movement not in MOVEMENTS:
            raise ValueError(
                f"overrides for TLS {tls_id!r} link {raw_index!r} have unknown "
                f"movement {movement!r}; allowed: {list(MOVEMENTS)}"
            )
        normalized[index] = str(movement)
    return normalized


def _classify_connection(
    in_lane, out_lane, link_index: int, *, net_file: Path, tls_id: str
) -> str:
    """Classify one controlled connection into a MOVEMENTS label."""
    in_edge = in_lane.getEdge()
    out_edge = out_lane.getEdge()
    in_heading = edge_heading_into_node(in_edge)
    cardinal = nearest_cardinal(in_heading)

    direction = _connection_direction(in_lane, out_lane, link_index)
    turn = _DIRECTION_TO_TURN.get(direction) if direction is not None else None
    if turn is None:
        out_heading = edge_heading_out_of_node(out_edge)
        turn = classify_turn_by_angle(signed_turn(in_heading, out_heading))

    movement = f"{cardinal}-{turn}"
    if movement not in MOVEMENTS:
        raise ValueError(
            f"{net_file}: TLS {tls_id!r} link {link_index} "
            f"({in_edge.getID()} -> {out_edge.getID()}) could not be classified; "
            f"{_OVERRIDE_HINT}"
        )
    return movement


def _connection_direction(in_lane, out_lane, link_index: int) -> str | None:
    """Find the SUMO ``dir`` attribute for the (in_lane, out_lane, link) triple.

    ``tls.getConnections()`` yields lane pairs without the connection metadata,
    so we locate the matching edge-level Connection object. Returns None when
    no match is found (caller falls back to turn-angle classification).
    """
    in_edge = in_lane.getEdge()
    out_edge = out_lane.getEdge()
    try:
        candidates = in_edge.getConnections(out_edge)
    except Exception:
        return None
    for conn in candidates:
        if (
            conn.getFromLane() == in_lane
            and conn.getToLane() == out_lane
            and int(conn.getTLLinkIndex()) == link_index
        ):
            return conn.getDirection()
    return None
