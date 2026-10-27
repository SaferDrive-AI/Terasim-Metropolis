"""Read and write SUMO configuration (``.sumocfg``) files.

The TMC pipeline does not generate ``net.xml`` or ``tls.xml`` — those are
inputs supplied by the user. Instead, we take an existing "base" sumocfg
(usually the one that ships with the SUMO project) and emit a new sumocfg
that:

* References the same net, tls, view, and other additional files,
* Replaces the route file with the freshly generated one,
* Inherits the tuned processing/routing parameters from the base file,
* Pins random behaviour to a known seed for reproducibility.

Using the base sumocfg as a template lets us pick up city-specific tuning
(e.g. ``tls.actuated.jam-threshold``, ``lateral-resolution``) without having
to re-derive those values from the road network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: SUMO simulation step length in seconds for the city pipeline. Base sumocfgs
#: usually omit <step-length>, so this default governs every generated scenario
#: and trial; at run time SUMO's deltaT (this value) is what
#: ``terasim.utils.get_step_size`` returns, so all controllers follow it.
DEFAULT_STEP_LENGTH_S = 1.0

from lxml import etree as ET


@dataclass(frozen=True)
class SumocfgInfo:
    """Structured view of the fields we care about in a base sumocfg.

    All paths are resolved to absolute paths so downstream code does not have
    to know about the base sumocfg's location anymore.
    """

    sumocfg_path: Path
    net_file: Path
    route_files: tuple[Path, ...]
    additional_files: tuple[Path, ...]
    gui_settings_file: Path | None
    processing_params: dict[str, str] = field(default_factory=dict)
    routing_params: dict[str, str] = field(default_factory=dict)
    report_params: dict[str, str] = field(default_factory=dict)
    step_length: float = DEFAULT_STEP_LENGTH_S


def _resolve(path: str, anchor: Path) -> Path:
    """Resolve ``path`` relative to ``anchor`` (the sumocfg's directory).

    SUMO interprets relative paths inside a sumocfg relative to the sumocfg
    file's own directory, not the process's cwd. We mirror that behaviour
    here so the resulting absolute paths can be safely written into the new
    sumocfg regardless of where the user invokes the pipeline from.
    """
    p = Path(path)
    if p.is_absolute():
        return p
    return (anchor / p).resolve()


def parse_sumocfg(sumocfg_path: Path | str) -> SumocfgInfo:
    """Parse a SUMO configuration file into a :class:`SumocfgInfo` snapshot.

    The function deliberately collects only the fields the pipeline cares
    about. Unknown SUMO sections (e.g. ``random_number``) are not preserved
    because the new sumocfg writer explicitly controls those.
    """
    sumocfg_path = Path(sumocfg_path).resolve()
    anchor = sumocfg_path.parent
    tree = ET.parse(str(sumocfg_path))
    root = tree.getroot()

    net_file_node = root.find(".//input/net-file")
    if net_file_node is None or "value" not in net_file_node.attrib:
        raise ValueError(
            f"sumocfg {sumocfg_path} does not declare an <input><net-file/></input>"
        )
    net_file = _resolve(net_file_node.attrib["value"], anchor)

    route_files: list[Path] = []
    route_node = root.find(".//input/route-files")
    if route_node is not None and route_node.attrib.get("value"):
        for token in route_node.attrib["value"].split(","):
            token = token.strip()
            if token:
                route_files.append(_resolve(token, anchor))

    additional_files: list[Path] = []
    addl_node = root.find(".//input/additional-files")
    if addl_node is not None and addl_node.attrib.get("value"):
        for token in addl_node.attrib["value"].split(","):
            token = token.strip()
            if token:
                additional_files.append(_resolve(token, anchor))

    gui_node = root.find(".//gui_only/gui-settings-file")
    gui_settings_file: Path | None = None
    if gui_node is not None and gui_node.attrib.get("value"):
        gui_settings_file = _resolve(gui_node.attrib["value"], anchor)

    processing_params = _collect_section(root, "processing")
    routing_params = _collect_section(root, "routing")
    report_params = _collect_section(root, "report")

    step_length = DEFAULT_STEP_LENGTH_S
    step_node = root.find(".//time/step-length")
    if step_node is not None and step_node.attrib.get("value"):
        try:
            step_length = float(step_node.attrib["value"])
        except ValueError:
            pass

    return SumocfgInfo(
        sumocfg_path=sumocfg_path,
        net_file=net_file,
        route_files=tuple(route_files),
        additional_files=tuple(additional_files),
        gui_settings_file=gui_settings_file,
        processing_params=processing_params,
        routing_params=routing_params,
        report_params=report_params,
        step_length=step_length,
    )


def _collect_section(root, section: str) -> dict[str, str]:
    """Collect ``<value>`` attributes from every child of a top-level section.

    Returns a flat ``{element_name: value}`` dict. Repeated elements overwrite
    earlier values (SUMO itself takes the last occurrence).
    """
    out: dict[str, str] = {}
    section_node = root.find(section)
    if section_node is None:
        return out
    for child in section_node:
        if "value" in child.attrib:
            out[child.tag] = child.attrib["value"]
    return out


def write_sumocfg(
    output_path: Path | str,
    *,
    net_file: Path,
    route_file: Path,
    additional_files: tuple[Path, ...] = (),
    gui_settings_file: Path | None = None,
    processing_params: dict[str, str] | None = None,
    routing_params: dict[str, str] | None = None,
    report_params: dict[str, str] | None = None,
    sim_end: float = 1800.0,
    seed: int = 42,
    step_length: float = DEFAULT_STEP_LENGTH_S,
) -> Path:
    """Write a new sumocfg referencing ``route_file`` and absolute net/tls paths.

    Absolute paths are used throughout so the resulting sumocfg can be
    relocated without breaking. The ``random_number`` section pins the seed
    for reproducibility.

    Args:
        output_path: Destination sumocfg path. Parent directories are created.
        net_file: Absolute path to the SUMO net.xml.
        route_file: Absolute path to the newly generated rou.xml.
        additional_files: Optional sequence of absolute paths to additional
            files (e.g. ``osm.tls.xml``).
        gui_settings_file: Optional absolute path to a GUI view file.
        processing_params: ``{tag: value}`` dict; each entry becomes a child
            element under ``<processing>``.
        routing_params: Same shape, written under ``<routing>``.
        report_params: Same shape, written under ``<report>``.
        sim_end: Simulation end time in seconds.
        seed: Deterministic random seed for SUMO.
        step_length: Simulation step length in seconds (default
            ``DEFAULT_STEP_LENGTH_S`` = 1.0).

    Returns:
        The absolute path of the written sumocfg.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    root = ET.Element(
        "sumoConfiguration",
        nsmap={"xsi": "http://www.w3.org/2001/XMLSchema-instance"},
        attrib={
            "{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation":
                "http://sumo.dlr.de/xsd/sumoConfiguration.xsd",
        },
    )

    # ---- <input> --------------------------------------------------------
    inp = ET.SubElement(root, "input")
    ET.SubElement(inp, "net-file", {"value": str(net_file)})
    ET.SubElement(inp, "route-files", {"value": str(route_file)})
    if additional_files:
        ET.SubElement(
            inp,
            "additional-files",
            {"value": ",".join(str(p) for p in additional_files)},
        )

    # ---- <time> ---------------------------------------------------------
    time_node = ET.SubElement(root, "time")
    ET.SubElement(time_node, "begin", {"value": "0"})
    ET.SubElement(time_node, "end", {"value": f"{sim_end:g}"})
    ET.SubElement(time_node, "step-length", {"value": f"{step_length:g}"})

    # ---- <random_number> -----------------------------------------------
    rnd = ET.SubElement(root, "random_number")
    ET.SubElement(rnd, "random", {"value": "false"})
    ET.SubElement(rnd, "seed", {"value": str(seed)})

    # ---- <processing> --------------------------------------------------
    _emit_section(root, "processing", processing_params)
    _emit_section(root, "routing", routing_params)
    _emit_section(root, "report", report_params)

    # ---- <gui_only> ----------------------------------------------------
    if gui_settings_file is not None:
        gui_only = ET.SubElement(root, "gui_only")
        ET.SubElement(gui_only, "gui-settings-file", {"value": str(gui_settings_file)})

    tree = ET.ElementTree(root)
    tree.write(
        str(output_path),
        encoding="utf-8",
        xml_declaration=True,
        pretty_print=True,
    )

    return output_path.resolve()


def _emit_section(root, tag: str, params: dict[str, str] | None) -> None:
    """Write ``<tag><child value="..."/></tag>`` for every key/value pair."""
    if not params:
        return
    section = ET.SubElement(root, tag)
    for child_tag, value in params.items():
        ET.SubElement(section, child_tag, {"value": value})
