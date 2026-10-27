"""Tests for TLS id discovery/resolution from sumocfg additional files."""

from __future__ import annotations

from pathlib import Path

import pytest

from terasim_city.signals import discover_tls_ids, resolve_tls_id


def _sumocfg_with_tls(tmp_path: Path, tls_ids: list[str]) -> Path:
    """Write a minimal sumocfg + a tls additional file defining the given TLS ids."""
    blocks = "".join(
        f'<tlLogic id="{tid}" type="static" programID="p0">'
        f'<phase duration="5" state="GG"/></tlLogic>'
        for tid in tls_ids
    )
    (tmp_path / "tls.add.xml").write_text(
        f"<additional>{blocks}</additional>", encoding="utf-8"
    )
    sumocfg = tmp_path / "sim.sumocfg"
    sumocfg.write_text(
        "<configuration><input>"
        '<net-file value="dummy.net.xml"/>'
        '<additional-files value="tls.add.xml"/>'
        "</input></configuration>",
        encoding="utf-8",
    )
    return sumocfg

def test_discover_tls_ids_lists_distinct_in_order(tmp_path: Path) -> None:
    sumocfg = _sumocfg_with_tls(tmp_path, ["a", "b"])
    assert discover_tls_ids(sumocfg) == ("a", "b")


def test_resolve_tls_id_auto_selects_the_only_one(tmp_path: Path) -> None:
    sumocfg = _sumocfg_with_tls(tmp_path, ["only_one"])
    assert resolve_tls_id(sumocfg, None) == "only_one"


def test_resolve_tls_id_errors_when_ambiguous(tmp_path: Path) -> None:
    sumocfg = _sumocfg_with_tls(tmp_path, ["a", "b"])
    with pytest.raises(ValueError, match="specify which"):
        resolve_tls_id(sumocfg, None)


def test_discover_tls_ids_ignores_tllogic_without_program(tmp_path: Path) -> None:
    # An id-only tlLogic has nothing to evaluate, so it must not count toward
    # ambiguity; only the one with a programID should be discovered.
    (tmp_path / "tls.add.xml").write_text(
        "<additional>"
        '<tlLogic id="not_evaluable" type="static"/>'
        '<tlLogic id="usable" type="static" programID="p0">'
        '<phase duration="5" state="GG"/></tlLogic>'
        "</additional>",
        encoding="utf-8",
    )
    sumocfg = tmp_path / "sim.sumocfg"
    sumocfg.write_text(
        "<configuration><input>"
        '<net-file value="dummy.net.xml"/>'
        '<additional-files value="tls.add.xml"/>'
        "</input></configuration>",
        encoding="utf-8",
    )

    assert discover_tls_ids(sumocfg) == ("usable",)
    assert resolve_tls_id(sumocfg, None) == "usable"
