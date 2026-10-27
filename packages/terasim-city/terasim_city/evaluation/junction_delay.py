"""Per-junction delay from SUMO ``--edgedata-output``: mean over the junction's
incoming edges of ``timeLoss / left`` (seconds per vehicle that left the edge).
This is the edge-based metric many agencies use; it complements the all-demand
MOE and is never part of the score."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

import sumolib
from lxml import etree as ET


def junction_delay_from_edgedata(
    edgedata_path: Path | str, net_file: Path | str, tls_ids: Sequence[str]
) -> dict[str, "float | None"]:
    edgedata_path = Path(edgedata_path)
    if not edgedata_path.exists():
        raise FileNotFoundError(f"edge data file not found: {edgedata_path}")
    per_edge: dict[str, float] = {}
    for interval in ET.parse(str(edgedata_path)).getroot().iter("interval"):
        for edge in interval.iter("edge"):
            left = float(edge.get("left") or 0.0)
            if left > 0.0:
                per_edge[edge.get("id")] = float(edge.get("timeLoss") or 0.0) / left
    net = sumolib.net.readNet(str(net_file))
    result: dict[str, float | None] = {}
    for tls_id in tls_ids:
        incoming = sorted(
            {lane.getEdge().getID() for lane, _out, _index in net.getTLS(tls_id).getConnections()}
        )
        values = [per_edge[e] for e in incoming if e in per_edge]
        result[tls_id] = (sum(values) / len(values)) if values else None
    return result
