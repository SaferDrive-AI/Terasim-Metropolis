"""Generator: grid-aligned windows, VSS behavior fetch, NVIDIA-identical counting."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime

import httpx
import pytest

from terasim_tmc_service import nv_generate_tmc
from terasim_tmc_service.generator import (
    DEFAULT_SETTLE_LAG_S,
    VssBehaviorSource,
    aligned_window,
    build_ftmc,
    filter_counted,
    seconds_until_next_window,
)
from terasim_tmc_service.registry import IntersectionRegistry

from .conftest import EXPECTED_COUNTS, METADATA_PATH, WINDOW_END, WINDOW_START, make_behavior


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def test_aligned_window_picks_latest_settled_grid_window():
    assert DEFAULT_SETTLE_LAG_S == 180.0
    assert aligned_window(_utc("2026-09-04T18:33:20Z"), 600) == (WINDOW_START, WINDOW_END)
    # 18:32:59 - 180 s = 18:29:59 -> the 18:20-18:30 window is not settled yet
    assert aligned_window(_utc("2026-09-04T18:32:59Z"), 600) == (
        "2026-09-04T18:10:00Z",
        "2026-09-04T18:19:59Z",
    )
    assert aligned_window(_utc("2026-09-04T18:33:20Z"), 600, settle_lag_s=0) == (
        "2026-09-04T18:20:00Z",
        "2026-09-04T18:29:59Z",
    )


def test_seconds_until_next_window():
    # next grid end after (now - 180 s) is 18:40:00; due at 18:40:00 + 180 + 5 = 18:43:05
    assert seconds_until_next_window(_utc("2026-09-04T18:33:20Z"), 600, 180) == 585.0
    assert seconds_until_next_window(_utc("2026-09-04T18:43:05Z"), 600, 180) == 600.0


def test_window_helpers_reject_naive_datetimes():
    naive = datetime(2026, 9, 4, 18, 33, 20)
    with pytest.raises(ValueError, match="timezone-aware"):
        aligned_window(naive, 600)
    with pytest.raises(ValueError, match="timezone-aware"):
        seconds_until_next_window(naive, 600, 180)


def test_vss_behavior_source_queries_like_nvidia_script():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"behaviors": [make_behavior("i1-cam1", 1, ["a"])]})

    source = VssBehaviorSource("http://vss:8081", transport=httpx.MockTransport(handler))
    behaviors = source.fetch("i1-cam1", WINDOW_START, WINDOW_END)
    source.close()

    assert len(behaviors) == 1
    request = seen[0]
    assert request.url.host == "vss" and request.url.path == "/behavior"
    assert request.url.params["sensorId"] == "i1-cam1"
    assert request.url.params["queryString"] == (
        "end:[2026-09-04T18:20:00.000Z TO 2026-09-04T18:29:59.000Z]"
    )
    assert request.url.params["maxResultSize"] == str(nv_generate_tmc.MAX_RESULT_SIZE)


def test_vss_behavior_source_surfaces_errors():
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="es down")

    source = VssBehaviorSource("http://vss:8081", transport=httpx.MockTransport(failing))
    with pytest.raises(RuntimeError, match="es down"):
        source.fetch("i1-cam1", WINDOW_START, WINDOW_END)

    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"behaviors": "nope"})

    source = VssBehaviorSource("http://vss:8081", transport=httpx.MockTransport(malformed))
    with pytest.raises(RuntimeError, match="behaviors"):
        source.fetch("i1-cam1", WINDOW_START, WINDOW_END)


def test_filter_counted_keeps_only_vehicles_of_that_sensor(sample_behaviors):
    kept = filter_counted(sample_behaviors["i1-cam1"], "i1-cam1")
    assert {item["object"]["type"] for item in kept} == {"Vehicle"}
    assert len(kept) == 7  # 5 movements + 1 without edges + 1 unmapped
    assert filter_counted(sample_behaviors["i1-cam1"], "i1-cam2") == []


def _registered(spec):
    registry = IntersectionRegistry(spec)
    # Offset slightly from the spec's own (37.335, -121.90339) but still inside the 50 m
    # registration radius, so intersection["lat"]/["lon"] in the assertions below can only
    # match if build_ftmc reads them from the spec rather than from the registration.
    return [registry.register("i1", "Main St & 1st Ave", 37.33502, -121.90344)]


def test_build_ftmc_matches_expected_counts_and_adds_name(spec, sample_behaviors):
    generated = build_ftmc(spec, _registered(spec), sample_behaviors, timestamp_start=WINDOW_START)
    payload = generated.payload
    assert payload["corridor_id"] == "demo_arterial"
    assert payload["observation_interval"] == "10m"
    assert payload["timestamp_start"] == WINDOW_START
    assert len(payload["intersections"]) == 1
    intersection = payload["intersections"][0]
    assert intersection["intersection_id"] == "i1"
    assert intersection["name"] == "Main St & 1st Ave"
    assert (intersection["lat"], intersection["lon"]) == (37.335, -121.90339)
    assert intersection["counts"] == EXPECTED_COUNTS
    assert generated.stats["i1"]["i1-cam1"] == {
        "processed_with_edges": 6,
        "skipped_without_edges": 1,
        "skipped_without_mapping": 1,
    }


def test_build_ftmc_with_missing_sensor_data_yields_zero_counts(spec):
    generated = build_ftmc(spec, _registered(spec), {}, timestamp_start=WINDOW_START)
    counts = generated.payload["intersections"][0]["counts"]
    assert counts["South"] == {"Right": 0, "Straight": 0, "Left": 0}


def test_build_ftmc_counts_equal_nvidia_script_offline_mode(spec, sample_behaviors, tmp_path):
    """Golden test: our counting == the vendored NVIDIA script run on the same objects."""
    paths = []
    for sensor, behaviors in sample_behaviors.items():
        path = tmp_path / f"{sensor}_00.json"
        path.write_text(json.dumps({"behaviors": behaviors}), encoding="utf-8")
        paths.append(str(path))
    output = tmp_path / "TMC.json"
    subprocess.run(
        [sys.executable, nv_generate_tmc.__file__, str(METADATA_PATH),
         "--behavior-path", *paths, "--tmc-path", str(output)],
        check=True,
        capture_output=True,
        cwd=tmp_path,
    )
    nvidia = json.loads(output.read_text(encoding="utf-8"))
    ours = build_ftmc(spec, _registered(spec), sample_behaviors, timestamp_start=WINDOW_START)
    assert ours.payload["intersections"][0]["counts"] == nvidia["intersections"][0]["counts"]
    assert ours.payload["corridor_id"] == nvidia["corridor_id"]
