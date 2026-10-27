"""Tests for the timing-report renderer, ``terasim_agent_skill.timing_report``.

The renderer turns a ``terasim.nema_plan.v1`` JSON (plus, optionally, an
optimization result) back into a controller-style timing report PDF, so the
agency gets its answer in the same document layout as its own timing report.
The plan under test is the synthetic 8-phase fixture ``main_1st``
(``MAIN ST @ 1ST AVE``: three coordinated 100 s patterns, offsets 12 / 8 / 64).

The whole module is skipped when ``reportlab`` is not installed.
"""

from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.nema_plan_files import MAIN_1ST_PLAN

PLAN_JSON = MAIN_1ST_PLAN
INTERSECTION_NAME = "MAIN ST @ 1ST AVE"
RENDERER_MODULE = "terasim_agent_skill.timing_report"

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("reportlab") is None,
    reason="reportlab not installed (pip install reportlab)",
)


@pytest.fixture(scope="module")
def renderer():
    return importlib.import_module(RENDERER_MODULE)


@pytest.fixture(scope="module")
def plan() -> dict:
    return json.loads(PLAN_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def optimized_doc(plan) -> dict:
    """A minimal run-shaped document: the midday pattern, re-timed.

    Mirrors the shape of the run JSON the optimization service writes
    (``result`` + ``optimized_nema_plan``) without reading any run file.
    """
    intersection = plan["intersections"][0]
    # Ring 1: 15.1 + 36.4 | 17.2 + 31.3; ring 2: 13.6 + 37.9 | 15.9 + 32.6
    # (both barrier sides 51.5 | 48.5, cycle 100).
    splits = {1: 15.1, 2: 36.4, 3: 17.2, 4: 31.3,
              5: 13.6, 6: 37.9, 7: 15.9, 8: 32.6}
    phases = []
    for spec in intersection["phases"]:
        num = int(spec["phase"])
        split = splits[num]
        phases.append({
            "nema_phase": num,
            "movements": spec["movements"],
            "split_s": split,
            "green_s": round(split - spec["yellow_s"] - spec["all_red_s"], 1),
            "yellow_s": spec["yellow_s"],
            "all_red_s": spec["all_red_s"],
            "notes": "",
        })
    return {
        "generated_at": "2026-09-07T22:15:13+00:00",
        "run_id": "test_run",
        "result": {
            "schema": "terasim.corridor_optimization.v1",
            "baseline": {"plan_id": "stored_nema", "score": 1.0, "moe": {
                "control_delay_s_per_veh": 42.5,
                "stops_per_veh": 0.72,
                "vehicles_total": 480, "vehicles_completed": 430,
                "total_delay_s": 20400.0, "mean_time_loss_s": 36.1}},
            "best": {"plan_id": "bo_0023", "score": 0.7864, "moe": {
                "control_delay_s_per_veh": 31.75,
                "stops_per_veh": 0.64,
                "vehicles_total": 480, "vehicles_completed": 446,
                "total_delay_s": 15240.0, "mean_time_loss_s": 28.4}},
            "config": {"max_trials": 24, "sim_end": 600.0,
                       "kpi_weights": {"control_delay": 0.7, "stops": 0.3},
                       "optimizer": "auto"},
            "n_trials": 25,
        },
        "optimized_nema_plan": {
            "schema": "terasim.nema_timing.v1",
            "generated_at": "2026-09-07T22:15:13+00:00",
            "corridor_id": plan.get("corridor_id"),
            "handoff_ready": True,
            "intersections": [{
                "tls_id": "J1",
                "pattern_id": "midday_peak",
                "description": intersection["patterns"][1]["description"],
                "valid_windows": [{"days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                                   "start": "09:30", "end": "15:00"}],
                "offset_s": 8.0,
                "cycle_s": 100.0,
                "phases": phases,
            }],
        },
    }


def _pdf_text(path: Path) -> str:
    """``pdftotext`` the rendered PDF (skips when poppler is unavailable)."""
    if shutil.which("pdftotext") is None:
        pytest.skip("pdftotext (poppler-utils) not available")
    out = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                         capture_output=True, text=True, encoding="utf-8", check=True)
    return out.stdout


def _page_count(path: Path) -> int:
    """Page count straight out of the PDF trailer (no external tools)."""
    return path.read_bytes().count(b"/Type /Page\n") or \
        path.read_bytes().count(b"/Type /Page")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_movement_direction(renderer):
    assert renderer.movement_direction(["EB-left"]) == "EBLT"
    assert renderer.movement_direction(["WB-through", "WB-right"]) == "WB"
    assert renderer.movement_direction(["SB-left"]) == "SBLT"
    assert renderer.movement_direction(["NB-through", "NB-right"]) == "NB"
    assert renderer.movement_direction([]) == ""


def test_phase_and_dow_masks(renderer):
    assert renderer.phase_mask([2, 6]) == "-2---6--"
    assert renderer.phase_mask([4, 8]) == "---4---8"
    assert renderer.dow_mask(["Mon", "Tue", "Wed", "Thu", "Fri"]) == \
        "-- M T W TR F --"
    assert renderer.dow_mask(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat",
                              "Sun"]) == "SU M T W TR F SA"
    assert renderer.dow_mask([]) == "-- - - - -- - --"


def test_street_split_and_shortening(renderer):
    first, second = renderer.split_intersection_name(INTERSECTION_NAME)
    assert (first, second) == ("MAIN ST", "1ST AVE")
    assert renderer.split_intersection_name("Main St & 2nd Ave") == ("Main St", "2nd Ave")
    assert renderer.street_short("1ST AVE") == "1ST"
    assert renderer.street_short("MAIN ST") == "MAIN"
    # A multi-word street keeps every word but its suffix.
    assert renderer.street_short("LAKE VIEW BLVD") == "LAKE VIEW"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_render_plan_only(renderer, tmp_path):
    out = tmp_path / "current.pdf"
    rc = renderer.main(["--plan", str(PLAN_JSON), "--out", str(out),
                        "--intersection-name", INTERSECTION_NAME,
                        "--report-date", "2026-09-07"])
    assert rc == 0
    assert out.exists() and out.stat().st_size > 0
    # cover + phase timing + 3 patterns + TOD
    assert _page_count(out) == 6

    text = _pdf_text(out)
    assert "(1-1) PHASE TIMING" in text
    assert "(3-2) COORDINATION PATTERNS" in text
    assert "(5-1) TOD PATTERNS" in text
    assert "CURRENT PLAN" in text
    # A plan-only report carries neither the optimized rows nor the KPI page.
    assert "Split (optimized)" not in text
    assert "KPI COMPARISON" not in text
    # Phase columns: directions, and the street each through phase serves
    # (E-W phases 2/6 on 1ST, N-S phases 4/8 on MAIN; lefts carry no name).
    assert "EBLT" in text and "SBLT" in text
    assert "Name 1ST MAIN 1ST MAIN" in _flat(text)
    assert "-2---6--" in text
    # The current splits and offsets as stored in the plan (AM PEAK page).
    assert "Description AM PEAK Cycle 100 Ofs 12" in _flat(text)
    assert "Split 16 34 15 35 15 35 16 34" in _flat(text)


def test_render_optimized(renderer, tmp_path, optimized_doc):
    opt_json = tmp_path / "run.json"
    opt_json.write_text(json.dumps(optimized_doc), encoding="utf-8")
    out = tmp_path / "optimized.pdf"
    rc = renderer.main(["--plan", str(PLAN_JSON), "--optimized", str(opt_json),
                        "--out", str(out),
                        "--intersection-name", INTERSECTION_NAME,
                        "--report-date", "2026-09-07",
                        "--demand-window", "2026-09-07 21:10-22:10"])
    assert rc == 0
    assert out.exists()
    # cover + phase timing + 3 patterns + TOD + KPI
    assert _page_count(out) >= 5

    text = _pdf_text(out)
    for needle in ("(1-1) PHASE TIMING", "(3-2) COORDINATION PATTERNS",
                   "(5-1) TOD PATTERNS", "KPI COMPARISON",
                   "OPTIMIZED BY TERASIM", "(OPTIMIZED)"):
        assert needle in text, f"{needle!r} missing from the rendered report"
    for row in ("Split (current)", "Split (optimized)", "Green (optimized)"):
        assert row in text, f"{row!r} row missing"
    # The optimized split values themselves.
    for value in ("15.1", "36.4", "17.2", "31.3", "13.6", "37.9", "15.9",
                  "32.6"):
        assert value in text, f"optimized split {value} missing"
    # KPI page numbers and run parameters.
    assert "42.50" in text and "31.75" in text
    assert "0.786" in text
    assert "2026-09-07 21:10-22:10" in text
    assert "24" in text


def test_unchanged_patterns_are_marked(renderer, tmp_path, optimized_doc):
    opt_json = tmp_path / "run2.json"
    opt_json.write_text(json.dumps(optimized_doc), encoding="utf-8")
    out = tmp_path / "optimized2.pdf"
    renderer.main(["--plan", str(PLAN_JSON), "--optimized", str(opt_json),
                   "--out", str(out)])
    text = _pdf_text(out)
    assert "Pattern unchanged by this optimization run" in text
    # Only the matched pattern page carries the OPTIMIZED marker.
    assert text.count("(OPTIMIZED)") == 1


def _flat(text: str) -> str:
    """The PDF text with every run of whitespace collapsed, so wrapped prose rejoins."""
    return " ".join(text.split())


def _corridor_run(optimized_doc: dict, *, cycles: tuple, junction: tuple,
                  signals: tuple | None = None) -> dict:
    """The fixture run in the shape the MCP artifact store writes it.

    ``kpis`` is the service's block (``cost`` / ``metrics`` / ``timing`` per side);
    ``result.baseline.moe`` and ``result.best.moe`` hold the same metrics, which is
    where the KPI page reads them. ``junction`` is each side's corridor mean of the
    edge-based delay; ``signals`` is each side's per-signal ``junction_delay_s``
    (default: one signal, ``n34``, at the corridor mean).
    """
    doc = copy.deepcopy(optimized_doc)
    kpis: dict = {"cost_weights": {"control_delay": 0.7, "stops": 0.3}}
    per_signal = signals or ({"n34": junction[0]}, {"n34": junction[1]})
    for label, key, cycle, delay, table in (
        ("baseline", "baseline", cycles[0], junction[0], per_signal[0]),
        ("optimized", "best", cycles[1], junction[1], per_signal[1]),
    ):
        moe = doc["result"][key]["moe"]
        moe["corridor_junction_delay_s"] = delay
        moe["junction_delay_s"] = table
        kpis[label] = {
            "cost": doc["result"][key]["score"],
            "metrics": moe,
            "timing": {"cycle_s": {"n34": cycle}, "offsets_s": {"n34": 0.0}},
        }
    doc["kpis"] = kpis
    return doc


def _render_run(renderer, tmp_path: Path, doc: dict) -> str:
    """Render ``doc`` as the optimized report and return its text, whitespace collapsed."""
    opt_json = tmp_path / "run.json"
    opt_json.write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "report.pdf"
    rc = renderer.main(["--plan", str(PLAN_JSON), "--optimized", str(opt_json),
                        "--out", str(out)])
    assert rc == 0
    return _flat(_pdf_text(out))


def test_report_states_the_cycle_change_and_the_junction_delay(
        renderer, tmp_path, optimized_doc):
    doc = _corridor_run(optimized_doc, cycles=(140.0, 164.0), junction=(47.0, 36.0))

    text = _render_run(renderer, tmp_path, doc)

    # The cover and the optimized pattern page both report the move; neither
    # claims the cycle was frozen.
    assert text.count("Cycle length changed from 140 s to 164 s") == 2
    assert "Cycle length and phase structure were kept" not in text
    assert "frozen" not in text
    # The KPI page carries the corridor's edge-based per-junction delay.
    assert "Per-junction delay, edge-based (s/veh) 47.00 36.00 -11.00 (-23.4%)" in text
    # The existing rows are still there.
    assert "Control delay (s/veh) 42.50 31.75" in text


@pytest.mark.parametrize("case", ["no_kpis", "equal_cycle", "within_half_a_second",
                                  "no_cycle_in_timing"])
def test_report_reads_an_unchanged_or_unreported_cycle_as_kept(
        renderer, tmp_path, optimized_doc, case):
    if case == "no_kpis":
        doc = optimized_doc
    elif case == "equal_cycle":
        doc = _corridor_run(optimized_doc, cycles=(140.0, 140.0), junction=(None, None))
    elif case == "within_half_a_second":
        doc = _corridor_run(optimized_doc, cycles=(140.0, 140.4), junction=(None, None))
    else:
        doc = _corridor_run(optimized_doc, cycles=(140.0, 164.0), junction=(None, None))
        for label in ("baseline", "optimized"):
            doc["kpis"][label]["timing"] = {"offsets_s": {"n34": 0.0}}

    text = _render_run(renderer, tmp_path, doc)

    assert text.count("Cycle length and phase structure were kept") == 2
    assert "changed from" not in text
    assert "frozen" not in text
    # Without the metric (absent or None) the KPI row says so instead of inventing a value.
    assert "Per-junction delay, edge-based (s/veh) n/a n/a n/a" in text


def test_report_reads_the_cycle_from_the_run_result_when_there_is_no_kpis_block(
        renderer, tmp_path, optimized_doc):
    """A run JSON straight from the optimizer carries its timing under result.baseline/best."""
    doc = copy.deepcopy(optimized_doc)
    doc["result"]["baseline"]["timing"] = {"cycle_s": {"J1": 90.0, "J2": 90.0}}
    doc["result"]["best"]["timing"] = {"cycle_s": {"J1": 100.0, "J2": 100.0}}

    text = _render_run(renderer, tmp_path, doc)

    assert text.count("Cycle length changed from 90 s to 100 s") == 2


def test_report_states_a_mixed_baseline_cycle_as_a_range(renderer, tmp_path, optimized_doc):
    doc = _corridor_run(optimized_doc, cycles=(140.0, 164.0), junction=(None, None))
    doc["kpis"]["baseline"]["timing"]["cycle_s"] = {"n16": 280.0, "n34": 140.0, "n69": 60.0}
    doc["kpis"]["optimized"]["timing"]["cycle_s"] = {"n16": 164.0, "n34": 164.0, "n69": 164.0}

    text = _render_run(renderer, tmp_path, doc)

    # The en dash survives the PDF font encoding on the cover and on the pattern page,
    # and the baseline is never reduced to its longest cycle.
    assert text.count("Cycle length changed from 60–280 s (per signal) to 164 s") == 2
    assert "280 s to" not in text


def test_report_lists_the_per_junction_delay_for_every_signal(
        renderer, tmp_path, optimized_doc):
    signals = ({"n34": 47.0, "n16": 36.9}, {"n34": 27.4, "n16": None})
    doc = _corridor_run(optimized_doc, cycles=(140.0, 164.0), junction=(41.95, 27.4),
                        signals=signals)

    text = _render_run(renderer, tmp_path, doc)

    # Under the KPI table and above the run parameters: one row per signal, sorted by
    # id, n/a where a side has no value.
    assert ("Per-junction delay (edge-based, s/veh) Signal Current Optimized "
            "n16 36.9 n/a n34 47.0 27.4 Run parameters") in text


def test_report_omits_the_per_signal_table_without_junction_delay_s(
        renderer, tmp_path, optimized_doc):
    no_metric = optimized_doc
    empty_tables = _corridor_run(optimized_doc, cycles=(140.0, 164.0), junction=(None, None),
                                 signals=({}, {}))
    for doc in (no_metric, empty_tables):
        text = _render_run(renderer, tmp_path, doc)

        assert "Per-junction delay (edge-based" not in text
        # the corridor-mean row of the KPI table is still there, as n/a
        assert "Per-junction delay, edge-based (s/veh) n/a n/a n/a" in text


def test_render_a_static_export_lists_its_stages(renderer, tmp_path):
    # The shape of a static plan's export (the bundled scenario's): no NEMA phase
    # numbers, no pattern id. It used to crash the renderer, which dropped the PDF.
    movements = (["WB-right", "WB-through", "WB-left", "EB-right", "EB-through", "EB-left"],
                 ["NB-right", "NB-through", "NB-left", "SB-right", "SB-through", "SB-left"])
    doc = {
        "result": {"baseline": {"score": 1.0, "moe": {}}, "best": {"score": 0.9, "moe": {}},
                   "config": {}},
        "optimized_nema_plan": {
            "schema": "terasim.nema_timing.v1",
            "handoff_ready": False,
            "coordination": {"cycle_length_s": 135.0, "reference_intersection": "J1"},
            "intersections": [{
                "tls_id": "J1",
                "offset_s": 0.0,
                "description": "Optimized plan",
                "valid_windows": [],
                "phases": [
                    {"nema_phase": None, "movements": movements[0], "split_s": 54.3,
                     "green_s": 51.3, "yellow_s": 3.0, "all_red_s": 0.0, "notes": ""},
                    {"nema_phase": None, "movements": movements[1], "split_s": 80.7,
                     "green_s": 77.7, "yellow_s": 3.0, "all_red_s": 0.0, "notes": ""},
                ],
            }],
        },
    }
    opt_json = tmp_path / "run.json"
    opt_json.write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "static.pdf"
    rc = renderer.main(["--plan", str(PLAN_JSON), "--optimized", str(opt_json),
                        "--out", str(out), "--report-date", "2026-10-06"])
    assert rc == 0
    text = _pdf_text(out)
    assert "OPTIMIZED TIMING (STAGES)" in text
    assert "S1" in text and "S2" in text
    assert "WB-right/through/left, EB-right/through/left" in text


def test_bare_timing_json_is_accepted(renderer, tmp_path, optimized_doc):
    """``--optimized`` also takes a plain terasim.nema_timing.v1 hand-off."""
    handoff = dict(optimized_doc["optimized_nema_plan"])
    handoff["kpis"] = {
        "baseline": optimized_doc["result"]["baseline"]["moe"],
        "optimized": optimized_doc["result"]["best"]["moe"],
        "score_best": optimized_doc["result"]["best"]["score"],
    }
    handoff["demand"] = {"tmc_windows_utc": "2026-09-07 21:10-22:10 (6 x 10 min)"}
    handoff["optimizer"] = {"mode": "stored NEMA seed; Bayesian search",
                            "trials": 24}
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(handoff), encoding="utf-8")
    out = tmp_path / "handoff.pdf"
    assert renderer.main(["--plan", str(PLAN_JSON), "--optimized", str(path),
                          "--out", str(out)]) == 0
    text = _pdf_text(out)
    assert "Split (optimized)" in text
    assert "32.6" in text
    assert "2026-09-07 21:10-22:10 (6 x 10 min)" in text


def test_sections_patterns_only_with_footer_offset(renderer, tmp_path):
    """``--sections patterns`` renders only the coordination-pattern pages, with
    the footer numbered as if they still lived at their spot in the source
    report (so they can stand in for the matching pages of the original)."""
    out = tmp_path / "patterns_only.pdf"
    rc = renderer.main(["--plan", str(PLAN_JSON), "--out", str(out),
                        "--sections", "patterns",
                        "--footer-start", "8", "--footer-total", "68"])
    assert rc == 0
    assert _page_count(out) == 3

    text = _pdf_text(out)
    assert text.count("(3-2) COORDINATION PATTERNS") == 3
    assert "Page 8 of 68" in text
    assert "Page 10 of 68" in text
    # Only the requested section is rendered.
    assert "(1-1) PHASE TIMING" not in text
    assert "(5-1) TOD PATTERNS" not in text
    assert "KPI COMPARISON" not in text


def test_footer_label_replaces_page_of_total(renderer, tmp_path, optimized_doc):
    """``--footer-label`` overrides the footer's ``Page N of M`` with fixed
    text (for example a KPI supplement page appended to an existing report)."""
    opt_json = tmp_path / "run3.json"
    opt_json.write_text(json.dumps(optimized_doc), encoding="utf-8")
    out = tmp_path / "kpi_only.pdf"
    rc = renderer.main(["--plan", str(PLAN_JSON), "--optimized", str(opt_json),
                        "--out", str(out), "--sections", "kpi",
                        "--footer-label",
                        "Supplement A - TeraSim optimization"])
    assert rc == 0
    assert _page_count(out) == 1

    text = _pdf_text(out)
    assert "KPI COMPARISON" in text
    assert "Supplement A - TeraSim optimization" in text
    assert "Page 1 of 1" not in text


def test_unknown_section_rejected(renderer, tmp_path):
    out = tmp_path / "bad.pdf"
    with pytest.raises(SystemExit):
        renderer.main(["--plan", str(PLAN_JSON), "--out", str(out),
                       "--sections", "bogus"])


def test_cli_via_subprocess(renderer, tmp_path):
    """The renderer runs as a command with only its own package on the path."""
    package_parent = Path(renderer.__file__).resolve().parents[1]
    out = tmp_path / "cli.pdf"
    proc = subprocess.run(
        [sys.executable, "-m", RENDERER_MODULE, "--plan", str(PLAN_JSON),
         "--out", str(out)],
        capture_output=True, text=True, cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(package_parent)},
    )
    assert proc.returncode == 0, proc.stderr
    assert out.exists()
    assert "(6 pages)" in proc.stdout
