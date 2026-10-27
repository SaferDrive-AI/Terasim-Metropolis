"""Downloadable artifacts for an optimization result.

An optimize tool result (``{"nema_plan": ..., "kpis": ...}``) is written to a
per-run directory as JSON, plus the signal-controller timing-report style PDF when
the intersection's current plan is in the plan store, and each file gets a
public download URL for the chat answer.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from terasim_agent_skill.artifacts import (
    ARTIFACT_FILES,
    ArtifactStore,
    PlanStoreLookup,
    SavedArtifacts,
    downloads_markdown,
)

# A synthetic three-pattern plan for intersection i1 of the demo corridor.
PLAN_JSON = Path(__file__).resolve().parent / "data" / "demo_arterial_i1.nema_plan.json"
PUBLIC_URL = "https://gw.test/terasim"
CORRIDOR_ID = "demo_arterial"
TARGET = {
    "intersection_id": "i1",
    "name": "Main St & 1st Ave",
    "lat": 37.335,
    "lon": -121.90339,
}
_HAS_REPORTLAB = importlib.util.find_spec("reportlab") is not None


@pytest.fixture(scope="module")
def plan() -> dict:
    assert PLAN_JSON.exists(), f"missing example plan at {PLAN_JSON}"
    return json.loads(PLAN_JSON.read_text(encoding="utf-8"))


def _timing_plan(plan: dict) -> dict:
    """A ``terasim.nema_timing.v1`` hand-off for the plan's midday pattern."""
    intersection = plan["intersections"][0]
    splits = {1: 12.0, 2: 44.0, 3: 11.0, 4: 33.0, 5: 11.0, 6: 45.0, 7: 12.0, 8: 32.0}
    phases = []
    for spec in intersection["phases"]:
        num = int(spec["phase"])
        phases.append(
            {
                "nema_phase": num,
                "movements": spec["movements"],
                "split_s": splits[num],
                "green_s": round(splits[num] - spec["yellow_s"] - spec["all_red_s"], 1),
                "yellow_s": spec["yellow_s"],
                "all_red_s": spec["all_red_s"],
                "notes": "",
            }
        )
    return {
        "schema": "terasim.nema_timing.v1",
        "generated_at": "2026-09-08T17:05:00+00:00",
        "handoff_ready": True,
        "intersections": [
            {
                "tls_id": "J1",
                "intersection_id": "i1",
                "pattern_id": "midday",
                "cycle_s": 100.0,
                "offset_s": 12.0,
                "phases": phases,
            }
        ],
    }


def _result(plan: dict) -> dict:
    return {
        "nema_plan": _timing_plan(plan),
        "kpis": {
            "cost_weights": {"control_delay": 0.7, "stops": 0.3},
            "baseline": {
                "cost": 1.0,
                "metrics": {
                    "control_delay_s_per_veh": 31.4,
                    "stops_per_veh": 0.712,
                    "vehicles_total": 512,
                    "vehicles_completed": 486,
                    "total_delay_s": 16076.8,
                    "mean_time_loss_s": 29.5,
                },
            },
            "optimized": {
                "cost": 0.911,
                "metrics": {
                    "control_delay_s_per_veh": 27.9,
                    "stops_per_veh": 0.684,
                    "vehicles_total": 512,
                    "vehicles_completed": 493,
                    "total_delay_s": 14284.8,
                    "mean_time_loss_s": 26.2,
                },
            },
        },
    }


def _store(tmp_path: Path, **kwargs) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts", PUBLIC_URL, **kwargs)


def test_save_writes_the_json_files_and_returns_public_links(tmp_path, plan):
    store = _store(tmp_path)

    saved = store.save_optimization(
        _result(plan), corridor_id=CORRIDOR_ID, intersections=[TARGET]
    )

    assert isinstance(saved, SavedArtifacts)
    assert saved.artifact_id.startswith("i1-")
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", saved.artifact_id)
    assert saved.directory == tmp_path / "artifacts" / saved.artifact_id
    assert set(saved.links) == {"nema_plan.json", "optimization.json"}
    for name, url in saved.links.items():
        assert url == f"{PUBLIC_URL}/files/{saved.artifact_id}/{name}"
        assert (saved.directory / name).is_file()
    written = json.loads((saved.directory / "nema_plan.json").read_text(encoding="utf-8"))
    assert written == _result(plan)["nema_plan"]


def test_optimization_json_carries_the_kpis_in_run_shape(tmp_path, plan):
    """The full result is stored the way the report renderer reads a run JSON."""
    store = _store(tmp_path)
    result = _result(plan)

    saved = store.save_optimization(result, corridor_id=CORRIDOR_ID, intersections=[TARGET])

    doc = json.loads((saved.directory / "optimization.json").read_text(encoding="utf-8"))
    assert doc["run_id"] == saved.artifact_id
    assert doc["corridor_id"] == CORRIDOR_ID
    assert doc["intersections"] == [TARGET]
    assert doc["optimized_nema_plan"] == result["nema_plan"]
    assert doc["kpis"] == result["kpis"]
    assert doc["result"]["baseline"] == {"score": 1.0, "moe": result["kpis"]["baseline"]["metrics"]}
    assert doc["result"]["best"] == {"score": 0.911, "moe": result["kpis"]["optimized"]["metrics"]}
    assert doc["result"]["config"] == {"kpi_weights": {"control_delay": 0.7, "stops": 0.3}}
    assert doc["generated_at"].endswith("Z")
    assert "traffic_model" not in doc  # a default run's file is as before


def test_optimization_json_names_a_cnde_traffic_model(tmp_path, plan):
    store = _store(tmp_path)
    result = {**_result(plan), "traffic_model": "cnde"}

    saved = store.save_optimization(result, corridor_id=CORRIDOR_ID, intersections=[TARGET])

    doc = json.loads((saved.directory / "optimization.json").read_text(encoding="utf-8"))
    assert doc["traffic_model"] == "cnde"


@pytest.mark.skipif(not _HAS_REPORTLAB, reason="reportlab not installed")
def test_save_renders_the_timing_report_when_the_current_plan_is_stored(tmp_path, plan):
    lookups: list[tuple[str, list[dict]]] = []

    def lookup(corridor_id: str, intersections: list[dict]) -> dict | None:
        lookups.append((corridor_id, intersections))
        return plan

    store = _store(tmp_path, plan_lookup=lookup)

    saved = store.save_optimization(
        _result(plan), corridor_id=CORRIDOR_ID, intersections=[TARGET]
    )

    assert lookups == [(CORRIDOR_ID, [TARGET])]
    assert set(saved.links) == set(ARTIFACT_FILES)
    pdf = (saved.directory / "timing_report.pdf").read_bytes()
    assert pdf.startswith(b"%PDF")
    # cover + phase timing + 3 coordination patterns + TOD + KPI comparison
    assert len(re.findall(rb"/Type\s*/Page[^s]", pdf)) == 7
    current = json.loads((saved.directory / "current_plan.json").read_text(encoding="utf-8"))
    assert current == plan
    # the chat summary compares against the same plan the report was rendered from
    assert saved.current_plan == plan


def test_no_pdf_and_no_current_plan_without_a_stored_plan(tmp_path, plan):
    store = _store(tmp_path, plan_lookup=lambda corridor_id, intersections: None)

    saved = store.save_optimization(
        _result(plan), corridor_id=CORRIDOR_ID, intersections=[TARGET]
    )

    assert saved.current_plan is None
    assert set(saved.links) == {"nema_plan.json", "optimization.json"}
    assert not (saved.directory / "timing_report.pdf").exists()
    assert not (saved.directory / "current_plan.json").exists()


def test_a_renderer_failure_keeps_the_json_links(tmp_path, plan):
    def broken_renderer(**kwargs):
        raise RuntimeError("reportlab exploded")

    store = _store(tmp_path, plan_lookup=lambda c, i: plan, renderer=broken_renderer)

    saved = store.save_optimization(
        _result(plan), corridor_id=CORRIDOR_ID, intersections=[TARGET]
    )

    assert set(saved.links) == {"nema_plan.json", "optimization.json", "current_plan.json"}
    assert not (saved.directory / "timing_report.pdf").exists()


def test_a_plan_lookup_failure_keeps_the_json_links(tmp_path, plan):
    def broken_lookup(corridor_id, intersections):
        raise OSError("store volume missing")

    store = _store(tmp_path, plan_lookup=broken_lookup)

    saved = store.save_optimization(
        _result(plan), corridor_id=CORRIDOR_ID, intersections=[TARGET]
    )

    assert set(saved.links) == {"nema_plan.json", "optimization.json"}


def test_artifact_ids_are_filesystem_safe_and_unique(tmp_path, plan):
    store = _store(tmp_path)
    odd = {**TARGET, "intersection_id": "Main/1st Ave #1"}

    first = store.save_optimization(_result(plan), corridor_id="c", intersections=[odd])
    second = store.save_optimization(_result(plan), corridor_id="c", intersections=[odd])

    for saved in (first, second):
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", saved.artifact_id)
        assert saved.artifact_id.startswith("Main_1st_Ave_1-")
    assert first.artifact_id != second.artifact_id


def test_corridor_ids_join_both_intersections(tmp_path, plan):
    store = _store(tmp_path)
    other = {**TARGET, "intersection_id": "i2"}

    saved = store.save_optimization(
        _result(plan), corridor_id=CORRIDOR_ID, intersections=[TARGET, other]
    )

    assert saved.artifact_id.startswith("i1+i2-")


def test_resolve_only_serves_files_it_wrote(tmp_path, plan):
    store = _store(tmp_path)
    saved = store.save_optimization(
        _result(plan), corridor_id=CORRIDOR_ID, intersections=[TARGET]
    )
    (tmp_path / "artifacts" / "secret.txt").write_text("nope", encoding="utf-8")

    assert store.resolve(saved.artifact_id, "nema_plan.json") == (
        saved.directory / "nema_plan.json"
    )
    assert store.resolve(saved.artifact_id, "timing_report.pdf") is None  # not written
    assert store.resolve(saved.artifact_id, "../secret.txt") is None
    assert store.resolve(saved.artifact_id, "secret.txt") is None
    assert store.resolve("..", "secret.txt") is None
    assert store.resolve("../artifacts", "nema_plan.json") is None
    assert store.resolve("missing-run", "nema_plan.json") is None
    assert store.resolve("", "nema_plan.json") is None


def test_downloads_markdown_lists_the_pdf_first_as_bare_urls():
    links = {
        "optimization.json": "https://gw.test/terasim/files/r1/optimization.json",
        "nema_plan.json": "https://gw.test/terasim/files/r1/nema_plan.json",
        "current_plan.json": "https://gw.test/terasim/files/r1/current_plan.json",
        "timing_report.pdf": "https://gw.test/terasim/files/r1/timing_report.pdf",
    }

    block = downloads_markdown(links)

    lines = block.splitlines()
    assert lines[0] == "**Downloads**"
    assert lines[1] == "- Signal timing report (PDF): https://gw.test/terasim/files/r1/timing_report.pdf"
    assert lines[2] == "- Optimized NEMA plan (JSON): https://gw.test/terasim/files/r1/nema_plan.json"
    assert lines[3] == "- Current plan as read (JSON): https://gw.test/terasim/files/r1/current_plan.json"
    assert lines[4] == "- Full optimization result (JSON): https://gw.test/terasim/files/r1/optimization.json"
    assert len(lines) == 5
    assert "[" not in block and "](" not in block


def test_downloads_markdown_skips_missing_files():
    block = downloads_markdown({"nema_plan.json": "https://gw.test/f/r1/nema_plan.json"})

    assert block.splitlines() == [
        "**Downloads**",
        "- Optimized NEMA plan (JSON): https://gw.test/f/r1/nema_plan.json",
    ]


def test_plan_store_lookup_returns_the_stored_slice(tmp_path, plan):
    from terasim_city.plans.store import PlanStore

    PlanStore(tmp_path / "store").ingest(plan)
    lookup = PlanStoreLookup(tmp_path / "store")

    doc = lookup(CORRIDOR_ID, [TARGET])

    assert doc is not None
    assert doc["schema"] == "terasim.nema_plan.v1"
    assert doc["corridor_id"] == CORRIDOR_ID
    assert [i["intersection_id"] for i in doc["intersections"]] == ["i1"]
    assert doc["intersections"][0]["patterns"] == plan["intersections"][0]["patterns"]


def test_plan_store_lookup_keeps_only_the_intersections_it_finds(tmp_path, plan):
    from terasim_city.plans.store import PlanStore

    PlanStore(tmp_path / "store").ingest(plan)
    lookup = PlanStoreLookup(tmp_path / "store")
    unknown = {"intersection_id": "nowhere", "name": "x", "lat": 0.0, "lon": 0.0}

    assert lookup(CORRIDOR_ID, [unknown]) is None
    doc = lookup(CORRIDOR_ID, [unknown, TARGET])
    assert doc is not None
    assert [i["intersection_id"] for i in doc["intersections"]] == ["i1"]


def test_plan_store_lookup_survives_a_missing_store_root(tmp_path):
    lookup = PlanStoreLookup(tmp_path / "does-not-exist")

    assert lookup(CORRIDOR_ID, [TARGET]) is None
