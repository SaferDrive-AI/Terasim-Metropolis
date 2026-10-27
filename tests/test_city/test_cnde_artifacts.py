"""ensure_cnde_artifacts: fail loud without a net, reuse existing artifacts as they are."""

import pickle
from pathlib import Path

import pytest

from terasim_city.maps.cnde_artifacts import CndeArtifacts, ensure_cnde_artifacts

def test_missing_net_fails_loud(tmp_path):
    with pytest.raises(FileNotFoundError):
        ensure_cnde_artifacts(tmp_path)   # an empty directory has no net.net.xml

def test_idempotent_returns_existing(tmp_path):
    (tmp_path / "net.net.xml").write_text("<net/>")
    (tmp_path / "cnde_map.pkl").write_bytes(pickle.dumps({"map_center": [0, 0]}))
    (tmp_path / "cnde_id_mapping.pkl").write_bytes(pickle.dumps({}))
    art = ensure_cnde_artifacts(tmp_path)   # both exist -> returned as is, x2cnde is never imported
    assert art.map_pkl.name == "cnde_map.pkl"
    assert art.id_mapping_pkl.name == "cnde_id_mapping.pkl"


# --- whether a "cnde" run on a map can get its artifacts -------------------------------

from terasim_city.maps.cnde_artifacts import missing_cnde_map_artifacts  # noqa: E402


def _map(directory: Path, *, package: bool, artifacts: bool = False) -> Path:
    """A base sumocfg + net in ``directory``, optionally a stored package with artifacts."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "net.net.xml").write_text("<net/>")
    sumocfg = directory / "base.sumocfg"
    sumocfg.write_text(
        '<configuration><input><net-file value="net.net.xml"/></input></configuration>'
    )
    if package:
        (directory / "map_package.json").write_text("{}")
    if artifacts:
        (directory / "cnde_map.pkl").write_bytes(b"x")
        (directory / "cnde_id_mapping.pkl").write_bytes(b"x")
    return sumocfg


def _no_converter(_name):
    return None


def test_a_stored_package_with_its_artifacts_is_ready(tmp_path):
    sumocfg = _map(tmp_path / "pkg", package=True, artifacts=True)

    assert missing_cnde_map_artifacts(sumocfg, find_spec=_no_converter) == []


def test_a_package_found_through_the_net_file_counts(tmp_path):
    _map(tmp_path / "pkg", package=True, artifacts=True)
    scenario = tmp_path / "scenario"
    scenario.mkdir()
    sumocfg = scenario / "scenario.sumocfg"
    sumocfg.write_text(
        '<configuration><input><net-file value="../pkg/net.net.xml"/></input></configuration>'
    )

    assert missing_cnde_map_artifacts(sumocfg, find_spec=_no_converter) == []


def test_a_map_outside_the_store_is_named(tmp_path):
    sumocfg = _map(tmp_path / "plain", package=False)

    (problem,) = missing_cnde_map_artifacts(sumocfg, find_spec=_no_converter)

    assert f"the corridor map {sumocfg} is not a stored map package" in problem


def test_missing_artifacts_need_the_converter(tmp_path):
    sumocfg = _map(tmp_path / "pkg", package=True)

    (problem,) = missing_cnde_map_artifacts(sumocfg, find_spec=_no_converter)
    converter = missing_cnde_map_artifacts(sumocfg, find_spec=lambda name: object())

    assert "has no CNDE map artifacts" in problem
    assert "tensorflow, waymo_open_dataset not installed" in problem
    assert converter == []  # it would generate them on first use


def test_an_unreadable_sumocfg_is_left_to_the_run(tmp_path):
    assert missing_cnde_map_artifacts(tmp_path / "absent.sumocfg", find_spec=_no_converter) == []
