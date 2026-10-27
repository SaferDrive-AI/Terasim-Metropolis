"""Unit tests for the MapPackage manifest save/load contract (no SUMO)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from terasim_city.maps import MANIFEST_NAME, MapPackage


def _make_map_dir(root: Path) -> MapPackage:
    root.mkdir(parents=True, exist_ok=True)
    (root / "net.net.xml").write_text("<net/>", encoding="utf-8")
    (root / "base.sumocfg").write_text("<configuration/>", encoding="utf-8")
    (root / "plans.tls.xml").write_text("<additional/>", encoding="utf-8")
    return MapPackage(
        root=root,
        net_file=root / "net.net.xml",
        base_sumocfg=root / "base.sumocfg",
        tls_files=(root / "plans.tls.xml",),
        geo_referenced=True,
        source="prebuilt",
        metadata={"note": "test fixture"},
    )


def test_rejects_unknown_source(tmp_path):
    with pytest.raises(ValueError, match="source"):
        MapPackage(
            root=tmp_path,
            net_file=tmp_path / "net.net.xml",
            base_sumocfg=tmp_path / "base.sumocfg",
            tls_files=(),
            geo_referenced=False,
            source="handmade",
        )


def test_save_load_round_trip_default_location(tmp_path):
    pkg = _make_map_dir(tmp_path / "map")
    manifest = pkg.save()
    assert manifest == (tmp_path / "map" / MANIFEST_NAME).resolve()

    loaded = MapPackage.load(tmp_path / "map")
    assert loaded.root == (tmp_path / "map").resolve()
    assert loaded.net_file == (tmp_path / "map" / "net.net.xml").resolve()
    assert loaded.base_sumocfg == (tmp_path / "map" / "base.sumocfg").resolve()
    assert loaded.tls_files == ((tmp_path / "map" / "plans.tls.xml").resolve(),)
    assert loaded.geo_referenced is True
    assert loaded.source == "prebuilt"
    assert loaded.metadata == {"note": "test fixture"}


def test_save_outside_root_prebuilt_pattern(tmp_path):
    """The prebuilt provider writes the manifest into out_dir; root stays put."""
    pkg = _make_map_dir(tmp_path / "original")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    pkg.save(out_dir / MANIFEST_NAME)

    loaded = MapPackage.load(out_dir)
    assert loaded.root == (tmp_path / "original").resolve()
    assert loaded.net_file == (tmp_path / "original" / "net.net.xml").resolve()


def test_relocating_package_directory_keeps_working(tmp_path):
    pkg = _make_map_dir(tmp_path / "map")
    pkg.save()
    moved = tmp_path / "elsewhere" / "map"
    moved.parent.mkdir()
    (tmp_path / "map").rename(moved)

    loaded = MapPackage.load(moved)
    assert loaded.net_file == (moved / "net.net.xml").resolve()


def test_load_missing_manifest_fails_loud(tmp_path):
    with pytest.raises(FileNotFoundError, match=MANIFEST_NAME):
        MapPackage.load(tmp_path)


def test_load_rejects_wrong_schema(tmp_path):
    pkg = _make_map_dir(tmp_path / "map")
    manifest = pkg.save()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["schema"] = "terasim.other.v9"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        MapPackage.load(tmp_path / "map")


def test_load_rejects_missing_keys(tmp_path):
    pkg = _make_map_dir(tmp_path / "map")
    manifest = pkg.save()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    del payload["net_file"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="net_file"):
        MapPackage.load(tmp_path / "map")


def test_load_rejects_dangling_net_file(tmp_path):
    pkg = _make_map_dir(tmp_path / "map")
    pkg.save()
    (tmp_path / "map" / "net.net.xml").unlink()
    with pytest.raises(ValueError, match="net_file"):
        MapPackage.load(tmp_path / "map")


