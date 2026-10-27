"""Configurable detector labels: the counted object types are a setting, not hard-coded.

Detectors differ in the labels they emit: ``Vehicle`` / ``two_wheeler`` / ``Person`` on one,
``car`` / ``bicycle`` / ``person`` on another. With a fixed set {"Vehicle", "two_wheeler"}
every vehicle of a detector with other labels would be dropped and the TMC counts would be
zero; these tests pin the configurable behaviour.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from terasim_tmc_service import nv_generate_tmc
from terasim_tmc_service.app import TmcSettings, create_app, run_tick
from terasim_tmc_service.generator import build_ftmc, filter_counted
from terasim_tmc_service.nv_generate_tmc import (
    COUNTED_OBJECT_TYPES,
    COUNTED_OBJECT_TYPES_ENV,
    counted_object_types_from_env,
    is_counted_object_type,
    parse_counted_object_types,
)
from terasim_tmc_service.registry import IntersectionRegistry

from .conftest import EXPECTED_COUNTS, I1, METADATA_PATH, WINDOW_START
from .test_app import _NOW, FakeInjector, FakeSource

_RELABEL = {"Vehicle": "car", "Person": "person", "two_wheeler": "bicycle"}
_ZERO = {
    heading: {movement: 0 for movement in movements}
    for heading, movements in EXPECTED_COUNTS.items()
}


def _relabelled(sample_behaviors):
    """The sample behaviors with a lowercase car / bicycle / person label set."""
    relabelled = copy.deepcopy(sample_behaviors)
    for items in relabelled.values():
        for item in items:
            item["object"]["type"] = _RELABEL.get(item["object"]["type"], item["object"]["type"])
    return relabelled


def _registered(spec):
    registry = IntersectionRegistry(spec)
    return [registry.register(**I1)]


def test_default_set_is_vehicle_and_two_wheeler():
    assert COUNTED_OBJECT_TYPES == frozenset({"vehicle", "two_wheeler"})
    assert parse_counted_object_types(None) == COUNTED_OBJECT_TYPES


def test_parse_is_case_insensitive_and_reads_dash_as_underscore():
    assert parse_counted_object_types(" car, Bicycle ,two-wheeler,,") == frozenset(
        {"car", "bicycle", "two_wheeler"}
    )
    assert parse_counted_object_types(["Car", "car"]) == frozenset({"car"})
    for label in ("Vehicle", "vehicle", "VEHICLE", "two_wheeler", "two-wheeler", "Two-Wheeler"):
        assert is_counted_object_type(label, COUNTED_OBJECT_TYPES)
    for label in ("Person", "person", "car", "", None, 3):
        assert not is_counted_object_type(label, COUNTED_OBJECT_TYPES)


def test_an_empty_environment_variable_means_the_default():
    assert counted_object_types_from_env({COUNTED_OBJECT_TYPES_ENV: ""}) == COUNTED_OBJECT_TYPES
    assert counted_object_types_from_env({COUNTED_OBJECT_TYPES_ENV: "  "}) == COUNTED_OBJECT_TYPES


@pytest.mark.parametrize("value", [",", " , ", []])
def test_a_list_without_labels_is_rejected(value):
    with pytest.raises(ValueError, match=COUNTED_OBJECT_TYPES_ENV):
        parse_counted_object_types(value)


def test_environment_overrides_the_default():
    assert counted_object_types_from_env({}) == COUNTED_OBJECT_TYPES
    assert counted_object_types_from_env({COUNTED_OBJECT_TYPES_ENV: "car,bicycle"}) == frozenset(
        {"car", "bicycle"}
    )


def test_lowercase_labels_count_nothing_with_the_default_set(spec, sample_behaviors):
    """What a relabelled detector gives without the setting: every vehicle is dropped."""
    relabelled = _relabelled(sample_behaviors)
    assert all(filter_counted(items, sensor) == [] for sensor, items in relabelled.items())
    generated = build_ftmc(spec, _registered(spec), relabelled, timestamp_start=WINDOW_START)
    assert generated.payload["intersections"][0]["counts"] == _ZERO


def test_lowercase_labels_count_the_same_with_the_setting(spec, sample_behaviors):
    counted = parse_counted_object_types("car,bicycle")
    relabelled = _relabelled(sample_behaviors)
    generated = build_ftmc(
        spec, _registered(spec), relabelled, timestamp_start=WINDOW_START,
        counted_object_types=counted,
    )
    original = build_ftmc(spec, _registered(spec), sample_behaviors, timestamp_start=WINDOW_START)
    assert generated.payload["intersections"][0]["counts"] == EXPECTED_COUNTS
    assert generated.payload == original.payload
    assert generated.stats == original.stats


def test_run_tick_reports_the_labels_it_saw(spec, sample_behaviors):
    registry = IntersectionRegistry(spec)
    registry.register(**I1)
    relabelled = _relabelled(sample_behaviors)
    result = run_tick(
        spec, registry, FakeSource(relabelled), FakeInjector(), now=_NOW, settle_lag_s=180.0,
        last_windows={}, counted_object_types=parse_counted_object_types("car,bicycle"),
    )
    assert result["counts"]["i1"] == EXPECTED_COUNTS
    assert result["object_types"]["i1-cam1"] == {"car": 7, "person": 1}
    assert result["object_types"]["i1-cam2"] == {"car": 2}


def test_settings_and_health_expose_the_counted_set(spec, sample_behaviors, monkeypatch, tmp_path):
    monkeypatch.setenv("TMC_SPEC_FILE", str(METADATA_PATH))
    monkeypatch.setenv("VSS_BEHAVIOR_URL", "http://vss:8081")
    monkeypatch.setenv(COUNTED_OBJECT_TYPES_ENV, "car, bicycle")
    settings = TmcSettings.from_environment()
    assert settings.counted_object_types == frozenset({"car", "bicycle"})
    monkeypatch.delenv(COUNTED_OBJECT_TYPES_ENV)
    assert TmcSettings.from_environment().counted_object_types == COUNTED_OBJECT_TYPES

    registry = IntersectionRegistry(spec, tmp_path / "registry.json")
    app = create_app(
        spec, registry, FakeSource(sample_behaviors), FakeInjector(), autostart=False,
        clock=lambda: _NOW, counted_object_types=settings.counted_object_types,
    )
    with TestClient(app) as client:
        assert client.get("/v1/tmc/health").json()["counted_object_types"] == ["bicycle", "car"]


def test_script_option_and_environment(spec, sample_behaviors, tmp_path):
    """The vendored script: --counted-object-types, TMC_COUNTED_OBJECT_TYPES, default."""
    relabelled = _relabelled(sample_behaviors)
    paths = []
    for sensor, behaviors in relabelled.items():
        path = tmp_path / f"{sensor}_00.json"
        path.write_text(json.dumps({"behaviors": behaviors}), encoding="utf-8")
        paths.append(str(path))

    def run(extra_args, env_value=None):
        output = tmp_path / f"TMC_{len(extra_args)}_{env_value}.json"
        env = {k: v for k, v in __import__("os").environ.items() if k != COUNTED_OBJECT_TYPES_ENV}
        if env_value is not None:
            env[COUNTED_OBJECT_TYPES_ENV] = env_value
        subprocess.run(
            [sys.executable, nv_generate_tmc.__file__, str(METADATA_PATH),
             "--behavior-path", *paths, "--tmc-path", str(output), *extra_args],
            check=True, capture_output=True, cwd=tmp_path, env=env,
        )
        return json.loads(output.read_text(encoding="utf-8"))["intersections"][0]["counts"]

    assert run([]) == _ZERO
    assert run(["--counted-object-types", "car,bicycle"]) == EXPECTED_COUNTS
    assert run([], env_value="car,bicycle") == EXPECTED_COUNTS
    assert run(["--counted-object-types", "Vehicle"], env_value="car") == _ZERO
