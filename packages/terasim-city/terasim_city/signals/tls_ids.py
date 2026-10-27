"""TLS id discovery/resolution from a base sumocfg's additional files."""

from __future__ import annotations

import logging
from pathlib import Path
from xml.etree import ElementTree as ET


logger = logging.getLogger(__name__)

def discover_tls_ids(base_sumocfg: Path | str) -> tuple[str, ...]:
    """Return the distinct TLS ids defined in the sumocfg's additional files.

    Scans the base sumocfg's additional ``.tls.xml`` files, NOT the net.xml —
    so the result
    is exactly the set of intersections that actually have programs to evaluate.
    Order is first-seen. Missing/unparseable additional files are skipped (with
    a warning).
    """
    from terasim_city.scenario.sumocfg_io import parse_sumocfg

    info = parse_sumocfg(base_sumocfg)
    seen: set[str] = set()
    ordered: list[str] = []
    for additional in info.additional_files:
        if not additional.exists():
            logger.warning("skipping additional file (not found): %s", additional)
            continue
        try:
            root = ET.parse(additional).getroot()
        except ET.ParseError as exc:
            logger.warning(
                "skipping additional file (XML parse error): %s (%s)", additional, exc
            )
            continue
        for node in root.findall(".//tlLogic"):
            tls_id = node.attrib.get("id")
            program_id = node.attrib.get("programID")
            # Only count intersections that actually have a program to evaluate
            # (programs need an explicit programID); an id-only tlLogic must
            # not make resolution think the map is ambiguous.
            if tls_id and program_id and tls_id not in seen:
                seen.add(tls_id)
                ordered.append(tls_id)
    return tuple(ordered)


def resolve_tls_id(base_sumocfg: Path | str, requested_tls_id: str | None) -> str:
    """Resolve which TLS to evaluate, failing loud on ambiguity.

    Rules (never silently picks the first of several):

    * 0 TLS in the map            → error (nothing to evaluate)
    * 1 TLS, none requested        → auto-select the only one
    * a TLS requested              → use it if it exists, else error + list
    * many TLS, none requested     → error + list the available ids
    """
    discovered = discover_tls_ids(base_sumocfg)
    if not discovered:
        raise ValueError(
            f"{base_sumocfg}: no traffic lights defined in the additional files; "
            "nothing to evaluate"
        )
    if requested_tls_id is not None:
        if requested_tls_id in discovered:
            return requested_tls_id
        raise ValueError(
            f"requested tls_id {requested_tls_id!r} not found in the map; "
            f"available: {', '.join(discovered)}"
        )
    if len(discovered) == 1:
        return discovered[0]
    raise ValueError(
        f"map defines {len(discovered)} traffic lights; specify which to evaluate "
        f"via tls_id. Available: {', '.join(discovered)}"
    )
