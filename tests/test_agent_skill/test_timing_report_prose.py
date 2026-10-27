"""The timing report's cycle wording, per-signal delay rows and stage rows, without reportlab.

``timing_report`` imports reportlab at module level because its painters draw with it, but
the wording and the table rows are plain data work. These tests load a private copy of the
module with stand-ins for the two reportlab names it imports, so they run where reportlab is
not installed; the page assembly runs on a canvas that only records what is drawn. Drawing
itself is covered by ``tests/test_city/test_render_timing_report.py``, which needs the real
library.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from terasim_agent_skill import server

KEPT = "Cycle length and phase structure were kept"
MIXED = {"n16": 280.0, "n34": 140.0, "n69": 60.0}
COMMON_164 = {"n16": 164.0, "n34": 164.0, "n69": 164.0}


def _stand_in(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


@pytest.fixture(scope="module")
def timing_report():
    """The report module, loaded privately without importing the real reportlab."""
    stand_ins = {
        "reportlab": _stand_in("reportlab"),
        "reportlab.lib": _stand_in("reportlab.lib"),
        "reportlab.lib.pagesizes": _stand_in("reportlab.lib.pagesizes", letter=(612.0, 792.0)),
        "reportlab.pdfgen": _stand_in("reportlab.pdfgen"),
        "reportlab.pdfgen.canvas": _stand_in("reportlab.pdfgen.canvas", Canvas=object),
    }
    found = importlib.util.find_spec("terasim_agent_skill.timing_report")
    assert found is not None and found.origin, "terasim_agent_skill.timing_report not found"
    spec = importlib.util.spec_from_file_location("timing_report_without_reportlab", found.origin)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        for name, stand_in in stand_ins.items():
            patch.setitem(sys.modules, name, stand_in)
        spec.loader.exec_module(module)
    return module


def _run_doc(before=None, after=None, *, junction=(None, None)) -> dict:
    """A run document as the MCP artifact store writes it, with only what the prose reads.

    ``before`` / ``after`` are each side's ``timing.cycle_s`` (``None``: no timing block);
    ``junction`` is each side's ``junction_delay_s`` (``None``: the metric is absent).
    """
    sides = {}
    for label, cycles, signals in (
        ("baseline", before, junction[0]),
        ("optimized", after, junction[1]),
    ):
        metrics = {} if signals is None else {"junction_delay_s": signals}
        side = {"cost": 1.0, "metrics": metrics}
        if cycles is not None:
            side["timing"] = {"cycle_s": cycles}
        sides[label] = side
    return {
        "kpis": sides,
        "result": {
            "baseline": {"score": 1.0, "moe": sides["baseline"]["metrics"]},
            "best": {"score": 0.9, "moe": sides["optimized"]["metrics"]},
        },
    }


# One rule, worded twice: the chat summary and the report must agree on what counts as
# unchanged, as one cycle, and as a range. This table is that rule.
@pytest.mark.parametrize(
    ("before", "after", "sentence", "clause"),
    [
        pytest.param(
            {"n34": 140.0}, {"n34": 164.0},
            "Cycle 140 s -> 164 s.", "Cycle length changed from 140 s to 164 s",
            id="common_changed",
        ),
        pytest.param(
            {"n34": 140.0}, {"n34": 140.0},
            "Cycle unchanged at 140 s.", KEPT,
            id="common_equal",
        ),
        pytest.param(
            {"n34": 140.0}, {"n34": 140.4},
            "Cycle unchanged at 140 s.", KEPT,
            id="common_within_half_a_second",
        ),
        pytest.param(
            {"n34": 140.0}, {"n34": 140.5},
            "Cycle 140 s -> 140.5 s.", "Cycle length changed from 140 s to 140.5 s",
            id="common_half_a_second_apart",
        ),
        pytest.param(
            {"n34": 140.0, "n16": 140.5}, {"n34": 164.0},
            "Cycle 140.5 s -> 164 s.", "Cycle length changed from 140.5 s to 164 s",
            id="spread_of_half_a_second_is_one_cycle",
        ),
        pytest.param(
            {"n34": 140.0, "n16": 140.6}, {"n34": 164.0},
            "Cycle 140–140.6 s (per signal) -> 164 s.",
            "Cycle length changed from 140–140.6 s (per signal) to 164 s",
            id="spread_over_half_a_second_is_a_range",
        ),
        pytest.param(
            MIXED, COMMON_164,
            "Cycle 60–280 s (per signal) -> 164 s.",
            "Cycle length changed from 60–280 s (per signal) to 164 s",
            id="mixed_baseline_common_optimized",
        ),
        pytest.param(
            COMMON_164, MIXED,
            "Cycle 164 s -> 60–280 s (per signal).",
            "Cycle length changed from 164 s to 60–280 s (per signal)",
            id="common_baseline_mixed_optimized",
        ),
        pytest.param(
            MIXED, {"n16": 200.0, "n34": 150.0, "n69": 90.0},
            "Cycles 60–280 s -> 90–200 s (per signal).",
            "Cycle lengths changed from 60–280 s to 90–200 s (per signal)",
            id="both_mixed_different",
        ),
        pytest.param(
            MIXED, dict(MIXED),
            "Cycles unchanged (60–280 s per signal).",
            "Cycle lengths were kept (60–280 s per signal)",
            id="both_mixed_equal",
        ),
        pytest.param(
            MIXED, {"n16": 280.3, "n34": 140.0, "n69": 59.8},
            "Cycles unchanged (60–280 s per signal).",
            "Cycle lengths were kept (60–280 s per signal)",
            id="both_mixed_within_half_a_second",
        ),
    ],
)
def test_summary_and_report_apply_the_same_cycle_rule(
    timing_report, before, after, sentence, clause
):
    doc = _run_doc(before, after)

    summary = server.summarize_optimization({"nema_plan": {}, "kpis": doc["kpis"]})
    run = timing_report.OptimizedResult(doc)

    assert sentence in summary
    assert run.cycle_clause == clause


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(None, {"n34": 164.0}, id="baseline_has_no_timing"),
        pytest.param({"n34": 140.0}, None, id="optimized_has_no_timing"),
        pytest.param({}, {"n34": 164.0}, id="empty_cycle_map"),
        pytest.param({"n34": "140"}, {"n34": 164.0}, id="unusable_cycle_values"),
        pytest.param(None, None, id="no_timing_at_all"),
    ],
)
def test_a_cycle_the_run_does_not_report_reads_as_kept(timing_report, before, after):
    assert timing_report.OptimizedResult(_run_doc(before, after)).cycle_clause == KEPT


def test_cycle_span_is_the_shortest_and_longest_usable_per_signal_cycle(timing_report):
    unusable = {"bad": "x", "flag": True, "nan": float("nan"), "inf": float("inf")}
    doc = _run_doc({"n34": 140.0, "n16": 100.0, **unusable}, {"n34": 164.0})

    run = timing_report.OptimizedResult(doc)

    assert (run.baseline_cycles, run.optimized_cycles) == ((100.0, 140.0), (164.0, 164.0))


def test_cycles_come_from_the_runs_own_result_when_there_is_no_kpis_block(timing_report):
    # a run JSON straight from the optimizer carries its timing under result.baseline / best
    doc = {
        "result": {
            "baseline": {"score": 1.0, "moe": {}, "timing": {"cycle_s": {"J1": 90.0, "J2": 90.0}}},
            "best": {"score": 0.9, "moe": {}, "timing": {"cycle_s": {"J1": 100.0, "J2": 100.0}}},
        }
    }

    run = timing_report.OptimizedResult(doc)

    assert (run.baseline_cycles, run.optimized_cycles) == ((90.0, 90.0), (100.0, 100.0))
    assert run.cycle_clause == "Cycle length changed from 90 s to 100 s"


def test_the_kpis_timing_wins_over_the_runs_result_timing(timing_report):
    doc = _run_doc({"n34": 140.0}, {"n34": 164.0})
    doc["result"]["baseline"]["timing"] = {"cycle_s": {"n34": 1.0}}
    doc["result"]["best"]["timing"] = {"cycle_s": {"n34": 2.0}}

    assert timing_report.OptimizedResult(doc).cycle_clause == (
        "Cycle length changed from 140 s to 164 s"
    )


def test_a_bare_handoff_document_has_no_cycles_and_no_junction_rows(timing_report):
    handoff = {
        "schema": "terasim.nema_timing.v1",
        "intersections": [],
        "kpis": {"baseline": {"control_delay_s_per_veh": 40.0}, "optimized": {}},
    }

    run = timing_report.OptimizedResult(handoff)

    assert (run.baseline_cycles, run.optimized_cycles) == (None, None)
    assert run.cycle_clause == KEPT
    assert run.junction_rows == []


def test_junction_rows_pair_each_signal_and_mark_gaps(timing_report):
    junction = ({"n34": 47.0, "n16": None}, {"n34": 27.4, "n69": 12.04})

    run = timing_report.OptimizedResult(_run_doc(junction=junction))

    assert run.junction_rows == [
        ("n16", "n/a", "n/a"),
        ("n34", "47.0", "27.4"),
        ("n69", "n/a", "12.0"),
    ]


@pytest.mark.parametrize(
    "junction",
    [
        pytest.param((None, None), id="metric_absent"),
        pytest.param(({}, {}), id="empty_tables"),
        pytest.param(({}, None), id="empty_and_absent"),
        pytest.param(([], "x"), id="not_tables"),
    ],
)
def test_no_junction_rows_when_neither_side_reports_a_table(timing_report, junction):
    assert timing_report.OptimizedResult(_run_doc(junction=junction)).junction_rows == []


def test_junction_rows_survive_odd_values(timing_report):
    junction = ({1: 5.0, "n34": float("nan"), "n2": True}, {"n34": "x"})

    run = timing_report.OptimizedResult(_run_doc(junction=junction))

    assert run.junction_rows == [("1", "5.0", "n/a"), ("n2", "n/a", "n/a"), ("n34", "n/a", "n/a")]


# --- stage-based (static) exports: phases without NEMA numbers ---------------------------

# A controller plan with three coordination patterns (synthetic, intersection i1).
CITY_PLAN = Path(__file__).resolve().parent / "data" / "demo_arterial_i1.nema_plan.json"

_EW = ["WB-right", "WB-through", "WB-left", "EB-right", "EB-through", "EB-left"]
_NS = ["NB-right", "NB-through", "NB-left", "SB-right", "SB-through", "SB-left"]


def _stage_row(movements, split_s, green_s):
    return {"nema_phase": None, "movements": list(movements), "split_s": split_s,
            "green_s": green_s, "yellow_s": 3.0, "all_red_s": 0.0, "notes": ""}


# The shape of the bundled scenario's NEMA export (a static plan: no phase numbers,
# no pattern id, no per-intersection cycle).
SCENARIO_NEMA = {
    "schema": "terasim.nema_timing.v1",
    "generated_at": "2026-10-06T12:46:01.966190+00:00",
    "corridor_id": "demo_scenario",
    "handoff_ready": False,
    "coordination": {"cycle_length_s": 135.0, "reference_intersection": "J1"},
    "intersections": [
        {
            "tls_id": tls_id,
            "offset_s": offset_s,
            "description": "Optimized plan",
            "valid_windows": [{"days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                               "start": "00:00", "end": "24:00"}],
            "phases": [_stage_row(_EW, ew[0], ew[1]), _stage_row(_NS, ns[0], ns[1])],
        }
        for tls_id, offset_s, ew, ns in (
            ("J1", 0.0, (54.3, 51.3), (80.7, 77.7)),
            ("J2", 34.0, (93.2, 90.2), (41.8, 38.8)),
            ("J3", 26.0, (73.8, 70.8), (61.2, 58.2)),
        )
    ],
}


def _scenario_run_doc() -> dict:
    """The run document the MCP artifact store writes around that export."""
    return {
        "schema": "terasim.optimization_run.v1",
        "result": {"baseline": {"score": 1.0, "moe": {}}, "best": {"score": 0.9, "moe": {}},
                   "config": {}},
        "optimized_nema_plan": copy.deepcopy(SCENARIO_NEMA),
        "kpis": {},
    }


def test_static_export_rows_become_stages_by_index_with_their_movements(timing_report):
    entry = SCENARIO_NEMA["intersections"][1]

    numbered, stages = timing_report.optimized_phase_rows(entry)

    assert numbered == {}
    assert [label for label, _row in stages] == ["S1", "S2"]
    assert timing_report.stage_table_rows(stages) == [
        ["S1", "WB-right/through/left, EB-right/through/left", "93.2", "90.2", "3.0", "0.0"],
        ["S2", "NB-right/through/left, SB-right/through/left", "41.8", "38.8", "3.0", "0.0"],
    ]


def test_numbered_rows_stay_keyed_by_phase_and_unnumbered_rows_become_stages(timing_report):
    entry = {"phases": [
        {"nema_phase": 2, "movements": ["EB-through"], "split_s": 40.0},
        {"nema_phase": "6", "movements": ["WB-through"], "split_s": 40.0},
        {"nema_phase": None, "movements": ["NB-left"], "split_s": 20.0},
    ]}

    numbered, stages = timing_report.optimized_phase_rows(entry)

    assert sorted(numbered) == [2, 6]
    assert [(label, row["movements"]) for label, row in stages] == [("S1", ["NB-left"])]
    assert timing_report.optimized_phase_rows(None) == ({}, [])


class _RecordingCanvas:
    """Stands in for reportlab's Canvas: records the strings drawn on each page."""

    last = None

    def __init__(self, *_args, **_kwargs):
        self.pages: list[list[str]] = [[]]
        _RecordingCanvas.last = self

    def drawString(self, _x, _y, text):
        self.pages[-1].append(str(text))

    drawRightString = drawCentredString = drawString

    def stringWidth(self, text, _font, size):
        return len(text) * size * 0.5

    def showPage(self):
        self.pages.append([])

    def __getattr__(self, _name):  # setFont, setLineWidth, line, setTitle, setAuthor, save
        return lambda *_args, **_kwargs: None


def _render(timing_report, monkeypatch, tmp_path, plan) -> tuple[int, list[str]]:
    monkeypatch.setattr(timing_report, "Canvas", _RecordingCanvas)
    optimized = timing_report.OptimizedResult(_scenario_run_doc(), source="optimization.json")
    pages, _path = timing_report.build_report(
        plan, Path("current_plan.json"), optimized, Path("optimization.json"),
        tmp_path / "timing_report.pdf", None, None,
    )
    return pages, [" | ".join(page) for page in _RecordingCanvas.last.pages if page]


def test_report_lists_a_static_exports_stages_on_their_own_page(
    timing_report, monkeypatch, tmp_path
):
    plan = json.loads(CITY_PLAN.read_text(encoding="utf-8"))

    pages, drawn = _render(timing_report, monkeypatch, tmp_path, plan)

    # cover, phase timing, three patterns, the stage page, TOD, KPI
    assert pages == 8
    stage_pages = [page for page in drawn if "OPTIMIZED TIMING (STAGES)" in page]
    assert len(stage_pages) == 1
    page = stage_pages[0]
    for cell in ("S1", "S2", "WB-right/through/left, EB-right/through/left",
                 "NB-right/through/left, SB-right/through/left", "54.3", "80.7", "135"):
        assert cell in page
    # The city's own patterns are printed as read, not as re-timed.
    assert not any("Split (optimized)" in page for page in drawn)


def test_report_never_crashes_on_unnumbered_phases_even_without_pattern_ids(
    timing_report, monkeypatch, tmp_path
):
    plan = json.loads(CITY_PLAN.read_text(encoding="utf-8"))
    for pattern in plan["intersections"][0]["patterns"]:
        pattern.pop("pattern_id", None)  # used to match the static entry and crash

    pages, drawn = _render(timing_report, monkeypatch, tmp_path, plan)

    assert pages == 8
    assert sum("OPTIMIZED TIMING (STAGES)" in page for page in drawn) == 1


# --- a current plan that is not a controller report ------------------------------------

_DEFAULT_COVER = "read from the city's controller"
_DEFAULT_KPI = "the current plan is simulated with the splits and offset read from"


def _signals_only_plan(**provenance) -> dict:
    """What a run without a stored plan gives the report: its signals, nothing to print."""
    plan = {
        "schema": "terasim.corridor_plan.v1",
        "corridor_id": "demo_scenario",
        "intersections": [
            {"intersection_id": tls, "name": tls, "phases": [], "patterns": [], "schedule": []}
            for tls in ("J1", "J2", "J3")
        ],
    }
    if provenance:
        plan["provenance"] = provenance
    return plan


def _render_with_input_plan(timing_report, monkeypatch, tmp_path, plan, sections=None):
    monkeypatch.setattr(timing_report, "Canvas", _RecordingCanvas)
    doc = _scenario_run_doc()
    doc["input_plan"] = plan
    optimized = timing_report.OptimizedResult(doc, source="optimization.json")
    pages, _path = timing_report.build_report(
        plan, Path("current_plan.json"), optimized, Path("optimization.json"),
        tmp_path / "timing_report.pdf", None, None, sections=sections,
    )
    return pages, [" | ".join(page) for page in _RecordingCanvas.last.pages if page]


def test_the_current_plan_note_replaces_the_controller_report_sentences(
    timing_report, monkeypatch, tmp_path
):
    plan = _signals_only_plan(
        current_plan_note="The current plan is the scenario's own signal programs.",
        current_plan_kpi_note="Measured in TeraSim/SUMO; current = the scenario's programs.",
    )

    pages, drawn = _render_with_input_plan(
        timing_report, monkeypatch, tmp_path, plan, sections=("cover", "patterns", "kpi")
    )

    # cover, one stage page per signal, KPI
    assert pages == 5
    text = " || ".join(drawn)
    assert "The current plan is the scenario's own signal programs." in text
    assert "Measured in TeraSim/SUMO; current = the scenario's programs." in text
    assert _DEFAULT_COVER not in text and _DEFAULT_KPI not in text
    assert "source report" not in text
    assert sum("OPTIMIZED TIMING (STAGES)" in page for page in drawn) == 3


def test_without_a_note_the_report_reads_as_before(timing_report, monkeypatch, tmp_path):
    _pages, drawn = _render_with_input_plan(
        timing_report, monkeypatch, tmp_path, _signals_only_plan(),
        sections=("cover", "patterns", "kpi"),
    )
    text = " || ".join(drawn)
    assert _DEFAULT_COVER in text and _DEFAULT_KPI in text
