"""Per-junction delay (mean timeLoss/left over incoming edges) from SUMO edge data."""
from __future__ import annotations

import pytest
import sumolib

from terasim_city.evaluation.junction_delay import junction_delay_from_edgedata
from tests.test_city.conftest import REPO_ROOT, requires_sumolib

DEMO_NET = REPO_ROOT / "examples" / "corridor_demo" / "demo.net.xml"


@requires_sumolib
def test_mean_over_incoming_edges(tmp_path):
    net = sumolib.net.readNet(str(DEMO_NET))
    incoming = sorted(
        {lane.getEdge().getID() for lane, _o, _i in net.getTLS("J1").getConnections()}
    )
    rows = "".join(
        f'<edge id="{edge}" timeLoss="{(i + 1) * 100.0}" left="10"/>'
        for i, edge in enumerate(incoming)
    )
    path = tmp_path / "edgedata.xml"
    path.write_text(
        '<meandata><interval begin="0" end="1800" id="ed">'
        f'{rows}<edge id="unused" timeLoss="999" left="0"/></interval></meandata>',
        encoding="utf-8",
    )
    result = junction_delay_from_edgedata(path, DEMO_NET, ["J1", "J2"])
    expected = sum((i + 1) * 10.0 for i in range(len(incoming))) / len(incoming)
    assert result["J1"] == pytest.approx(expected)
    assert result["J2"] is None  # no data for its edges


@requires_sumolib
def test_incoming_edge_without_departures_is_left_out_of_the_mean(tmp_path):
    net = sumolib.net.readNet(str(DEMO_NET))
    first, second = sorted(
        {lane.getEdge().getID() for lane, _o, _i in net.getTLS("J1").getConnections()}
    )[:2]
    path = tmp_path / "edgedata.xml"
    # A queue that has not discharged yet (left=0) has no per-vehicle value:
    # it is skipped, not divided by zero.
    path.write_text(
        '<meandata><interval begin="0" end="60" id="ed">'
        f'<edge id="{first}" timeLoss="500" left="0"/><edge id="{second}" timeLoss="30" left="3"/>'
        "</interval></meandata>",
        encoding="utf-8",
    )
    assert junction_delay_from_edgedata(path, DEMO_NET, ["J1"])["J1"] == pytest.approx(10.0)


def test_missing_file_raises(tmp_path):
    with pytest.raises(OSError):
        junction_delay_from_edgedata(tmp_path / "nope.xml", DEMO_NET, ["J1"])
