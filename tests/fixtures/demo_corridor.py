"""Paths and small builders around the synthetic demo corridor.

``examples/corridor_demo`` is a fully synthetic three-signal arterial: signals
``J1``/``J2``/``J3`` (~300 m apart on an E-W arterial with N-S cross streets),
TMC intersections ``i1``/``i2``/``i3`` exactly on them. The builders below
derive the map variants the tests need from those assets at test time:

* :func:`write_partial_program_sumocfg` - the demo net with candidate signal
  programs for SOME of its signals only (the others are signalized in the net
  but carry no additional-file program);
* :func:`build_crossing_net` - the demo network re-built with sidewalks and
  pedestrian crossings, so every signal controls more links than it has
  vehicle connections (the crossing links take the trailing link indices).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_NET = DEMO_DIR / "demo.net.xml"
DEMO_SUMOCFG = DEMO_DIR / "demo.sumocfg"
DEMO_TLS = DEMO_DIR / "demo.tls.xml"
DEMO_ROU = DEMO_DIR / "demo.rou.xml"
DEMO_TMC_JSON = DEMO_DIR / "demo_tmc.json"
DEMO_SCENARIO_SUMOCFG = DEMO_DIR / "demo_scenario.sumocfg"

#: Corridor id of every demo TMC payload.
DEMO_CORRIDOR_ID = "demo_arterial"
#: The three demo signals, west to east.
DEMO_TLS_IDS = ("J1", "J2", "J3")
#: TMC intersection id -> the signal it sits on.
DEMO_INTERSECTION_TLS = {"i1": "J1", "i2": "J2", "i3": "J3"}
#: TMC intersection id -> (lat, lon); exactly the signal junction positions.
DEMO_COORDS = {
    "i1": (37.335, -121.90339),
    "i2": (37.335, -121.9),
    "i3": (37.335, -121.89661),
}

_PLAIN_XML = ("demo.nod.xml", "demo.edg.xml", "demo.con.xml")


def write_partial_program_sumocfg(
    directory: Path,
    tls_ids=("J1",),
    *,
    name: str = "partial_programs.sumocfg",
) -> Path:
    """A sumocfg on the demo net whose TLS additional file covers ``tls_ids`` only.

    The ``alt_fixed`` programs of the listed signals are copied from
    ``demo.tls.xml``; every other demo signal is still a traffic light in the
    NET (its own program ``0``) but has no candidate program in the
    additional files.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    wanted = set(tls_ids)
    source = ET.parse(DEMO_TLS).getroot()
    additional = ET.Element("additional")
    for tl in source.iter("tlLogic"):
        if tl.get("id") in wanted:
            additional.append(tl)
    found = {tl.get("id") for tl in additional}
    missing = wanted - found
    if missing:
        raise ValueError(f"demo.tls.xml has no program for {sorted(missing)}")
    tls_file = directory / f"{Path(name).stem}.tls.xml"
    ET.ElementTree(additional).write(tls_file, encoding="utf-8", xml_declaration=True)
    sumocfg = directory / name
    sumocfg.write_text(
        "<configuration><input>"
        f'<net-file value="{DEMO_NET}"/>'
        f'<route-files value="{DEMO_ROU}"/>'
        f'<additional-files value="{tls_file}"/>'
        "</input></configuration>",
        encoding="utf-8",
    )
    return sumocfg


def build_crossing_net(directory: Path, *, name: str = "demo_crossings.net.xml") -> Path:
    """Re-build the demo network with sidewalks and signalized pedestrian crossings.

    Runs ``netconvert`` on the demo's own plain-XML inputs with
    ``--sidewalks.guess --crossings.guess``. Every demo signal then controls
    its 14 vehicle links (indices 0..13) plus one link per crossing, which
    netconvert numbers after the vehicle links. Needs ``netconvert`` on PATH.
    """
    netconvert = shutil.which("netconvert")
    if netconvert is None:
        raise RuntimeError("netconvert is not on PATH")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for plain in _PLAIN_XML:
        shutil.copyfile(DEMO_DIR / plain, directory / plain)
    out = directory / name
    subprocess.run(
        [
            netconvert,
            "--node-files", "demo.nod.xml",
            "--edge-files", "demo.edg.xml",
            "--connection-files", "demo.con.xml",
            "--proj.utm",
            "--tls.default-type", "static",
            "--sidewalks.guess",
            "--crossings.guess",
            "--no-warnings",
            "--output-file", out.name,
        ],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    )
    return out


def write_sumocfg(directory: Path, net_file: Path, *, name: str = "net_only.sumocfg",
                  route_files=(), additional_files=()) -> Path:
    """A minimal sumocfg for ``net_file`` (absolute paths, nothing else)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    parts = [f'<net-file value="{Path(net_file)}"/>']
    if route_files:
        parts.append(f'<route-files value="{",".join(str(p) for p in route_files)}"/>')
    if additional_files:
        parts.append(
            f'<additional-files value="{",".join(str(p) for p in additional_files)}"/>'
        )
    sumocfg = directory / name
    sumocfg.write_text(
        "<configuration><input>" + "".join(parts) + "</input></configuration>",
        encoding="utf-8",
    )
    return sumocfg
