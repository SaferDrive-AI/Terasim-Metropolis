"""GET /v1/city/optimizations/{run_id}/report.pdf: the run's official timing report.

Rendered on first request with the agent-skill timing-report renderer and cached as
timing_report.pdf. The drawing is checked with the renderer loaded around a canvas that
records what it draws, so these tests run with or without reportlab; one skip-marked
test renders the real PDF where reportlab is installed (as in the service image), and
one checks the 404 of a deployment without it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from terasim_city.plans.model import CorridorPlan, PhaseSpec, PlanProvenance, SignalPlan
from terasim_city.plans.nema_plan import NemaCorridorPlan, NemaSignalEntry
from terasim_service.city import run_report
from terasim_service.city.job_store import InMemoryCityJobStore
from terasim_service.city.optimize_api import create_v1_city_router

_BASE = "/v1/city/optimizations"
_HAS_REPORTLAB = importlib.util.find_spec("reportlab") is not None


# --- a finished run on disk ----------------------------------------------------------


def _static_plan() -> CorridorPlan:
    phases = (
        PhaseSpec("GGrr", 30.0, "green", movements=("EB-through", "WB-through")),
        PhaseSpec("yyrr", 3.0, "yellow"),
        PhaseSpec("rrGG", 30.0, "green", movements=("NB-through", "SB-through")),
        PhaseSpec("rryy", 3.0, "yellow"),
    )
    return CorridorPlan(
        "scenario_programs",
        tuple(SignalPlan(tls, "alt", 0.0, phases) for tls in ("J1", "J2")),
        PlanProvenance(source="sumo_tls"),
    )


def _nema_plan() -> NemaCorridorPlan:
    intersection = {
        "intersection_id": "i1",
        "name": "A ST @ B ST",
        "lat": 37.3,
        "lon": -121.9,
        "phases": [
            {"phase": 2, "movements": ["EB-through"], "yellow_s": 3.5, "all_red_s": 1.5,
             "min_green_s": 5},
            {"phase": 4, "movements": ["NB-through"], "yellow_s": 3.0, "all_red_s": 1.0,
             "min_green_s": 5},
        ],
        "rings": {"ring1": [2, 4], "ring2": [], "coordinated_phases": [2]},
        "patterns": [{"pattern_id": "midday", "description": "MIDDAY", "cycle_s": 90,
                      "offset_s": 12, "splits": {"2": 50, "4": 40}}],
        "schedule": [{"days": ["Mon"], "start": "09:30", "pattern_id": "midday"}],
    }
    entry = NemaSignalEntry("J1", intersection, dict(intersection["patterns"][0]))
    return NemaCorridorPlan("stored_nema", (entry,), PlanProvenance(source="stored_nema"))


def _optimized_nema(plan) -> dict:
    """The run's optimized export: the stored plan re-split, or the static plan as is."""
    import tempfile

    from terasim_city.nema.export import export_nema, export_nema_plan

    with tempfile.TemporaryDirectory() as scratch:
        if isinstance(plan, NemaCorridorPlan):
            return export_nema_plan(plan.with_splits({"J1": {"2": 55, "4": 35}}),
                                    corridor_id="c1", out_dir=Path(scratch))
        return export_nema(plan, corridor_id="c1", out_dir=Path(scratch))


def _finished_run(output_root: Path, run_id: str, plan, *, seed_source: str,
                  exported: bool = True) -> Path:
    run_dir = output_root / run_id
    trial = run_dir / "trials" / "trial_0000"
    trial.mkdir(parents=True)
    (trial / "plan.json").write_text(json.dumps(plan.to_json_dict()), encoding="utf-8")
    moe = {"control_delay_s_per_veh": 30.0, "stops_per_veh": 0.7, "vehicles_total": 400}
    result = {
        "seed": {"source": seed_source},
        "resolved_corridor": [
            {"ref": tls, "intersection_id": "i1" if tls == "J1" else None, "lat": None,
             "lon": None, "node_id": tls, "tls_id": tls, "match_distance_m": None}
            for tls in ("J1", "J2")
        ],
        "baseline": {"plan_id": plan.plan_id, "score": 1.0, "moe": moe,
                     "timing": {"cycle_s": {"J1": 66.0}}},
        "best": {"plan_id": "bo_1", "score": 0.9,
                 "moe": {**moe, "control_delay_s_per_veh": 27.0},
                 "timing": {"cycle_s": {"J1": 66.0}}},
        "trials": [{"plan_id": plan.plan_id, "trial_dir": str(trial)}],
        "config": {"kpi_weights": {"control_delay": 0.7, "stops": 0.3}, "optimizer": "auto",
                   "max_trials": 20, "sim_end": 1800.0},
    }
    if exported:
        (run_dir / "nema_plan.json").write_text(json.dumps(_optimized_nema(plan)), encoding="utf-8")
    else:
        result["nema_export"] = {"exported": False, "reason": "NEMA export skipped: no common cycle"}
    (run_dir / "corridor_optimization_result.json").write_text(json.dumps(result), encoding="utf-8")
    return run_dir


def _client(tmp_path: Path, plan=None, *, seed_source="sumo_tls", exported=True):
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    output_root = tmp_path / "out"
    output_root.mkdir()
    store.create("r1", origin="api", corridor_id="c1")
    run_dir = _finished_run(output_root, "r1", plan or _static_plan(),
                            seed_source=seed_source, exported=exported)
    store.set_completed("r1", str(run_dir / "corridor_optimization_result.json"), {})
    app = FastAPI()
    app.include_router(create_v1_city_router(store, lambda *_a: None, output_root=output_root))
    return TestClient(app), store, run_dir


# --- the renderer, around a recording canvas -----------------------------------------


class _RecordingCanvas:
    """Stands in for reportlab's Canvas: records the strings drawn, saves a stub PDF."""

    last = None

    def __init__(self, path, *_args, **_kwargs):
        self.path = path
        self.pages: list[list[str]] = [[]]
        _RecordingCanvas.last = self

    def drawString(self, _x, _y, text):
        self.pages[-1].append(str(text))

    drawRightString = drawCentredString = drawString

    def stringWidth(self, text, _font, size):
        return len(text) * size * 0.5

    def showPage(self):
        self.pages.append([])

    def save(self):
        Path(self.path).write_bytes(b"%PDF-1.4 stub\n")

    def __getattr__(self, _name):  # setFont, setLineWidth, line, setTitle, setAuthor
        return lambda *_args, **_kwargs: None


def _stand_in(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


@pytest.fixture
def recording_renderer(monkeypatch):
    """The timing-report module loaded with stand-ins for reportlab, drawing on a recorder."""
    stand_ins = {
        "reportlab": _stand_in("reportlab"),
        "reportlab.lib": _stand_in("reportlab.lib"),
        "reportlab.lib.pagesizes": _stand_in("reportlab.lib.pagesizes", letter=(612.0, 792.0)),
        "reportlab.pdfgen": _stand_in("reportlab.pdfgen"),
        "reportlab.pdfgen.canvas": _stand_in("reportlab.pdfgen.canvas", Canvas=_RecordingCanvas),
    }
    found = importlib.util.find_spec("terasim_agent_skill.timing_report")
    spec = importlib.util.spec_from_file_location("timing_report_recording", found.origin)
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        for name, stand_in in stand_ins.items():
            patch.setitem(sys.modules, name, stand_in)
        spec.loader.exec_module(module)
    calls = []

    def load():
        calls.append(1)
        return module

    monkeypatch.setattr(run_report, "renderer_unavailable_reason", lambda: None)
    monkeypatch.setattr(run_report, "load_renderer", load)
    return calls


def _drawn() -> list[str]:
    return [" | ".join(page) for page in _RecordingCanvas.last.pages if page]


def test_a_static_run_gets_cover_stages_and_kpis_with_its_own_provenance(
    tmp_path, recording_renderer
):
    client, _store, run_dir = _client(tmp_path)

    response = client.get(f"{_BASE}/r1/report.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "inline" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")
    assert (run_dir / run_report.REPORT_FILENAME).read_bytes() == response.content
    drawn = _drawn()
    text = " || ".join(drawn)
    assert "OPTIMIZED BY TERASIM" in text
    assert "the scenario's own signal programs" in text
    assert "read from the city's controller" not in text
    assert sum("OPTIMIZED TIMING (STAGES)" in page for page in drawn) == 2
    assert any("KPI COMPARISON" in page for page in drawn)
    assert not any("(1-1) PHASE TIMING" in page for page in drawn)
    assert "Run id | r1" in text or "r1" in text
    # The inputs the renderer read are kept next to the PDF.
    document = json.loads((run_dir / "report" / "optimization.json").read_text())
    assert document["run_id"] == "r1"
    assert document["kpis"]["optimized"]["metrics"]["control_delay_s_per_veh"] == 27.0
    assert document["optimized_nema_plan"]["schema"] == "terasim.nema_timing.v1"

    # Cached: the second request does not render again.
    assert client.get(f"{_BASE}/r1/report.pdf").status_code == 200
    assert len(recording_renderer) == 1


def test_a_stored_plan_run_prints_the_citys_plan_with_the_optimized_splits(
    tmp_path, recording_renderer
):
    client, _store, _run_dir = _client(tmp_path, _nema_plan(), seed_source="stored_nema")

    response = client.get(f"{_BASE}/r1/report.pdf")

    assert response.status_code == 200
    drawn = _drawn()
    text = " || ".join(drawn)
    assert any("(1-1) PHASE TIMING" in page for page in drawn)
    assert any("(3-2) COORDINATION PATTERNS" in page for page in drawn)
    assert any("(5-1) TOD PATTERNS" in page for page in drawn)
    assert "Split (optimized)" in text
    assert "read from the city's controller" in text


def test_a_run_without_a_nema_export_has_no_report(tmp_path, recording_renderer):
    client, _store, _run_dir = _client(tmp_path, exported=False)

    response = client.get(f"{_BASE}/r1/report.pdf")

    assert response.status_code == 404
    assert "no common cycle" in response.json()["detail"]


def test_a_rendering_failure_is_404_with_the_reason(tmp_path, monkeypatch):
    client, _store, run_dir = _client(tmp_path)

    def broken():
        raise RuntimeError("font table damaged")

    monkeypatch.setattr(run_report, "renderer_unavailable_reason", lambda: None)
    monkeypatch.setattr(run_report, "load_renderer", broken)

    response = client.get(f"{_BASE}/r1/report.pdf")

    assert response.status_code == 404
    assert "font table damaged" in response.json()["detail"]
    assert (run_dir / run_report.REPORT_ERROR_FILENAME).is_file()
    assert run_report.has_report(run_dir, completed=True) is False


def test_without_the_renderer_the_report_is_404_with_the_reason(tmp_path, monkeypatch):
    client, _store, run_dir = _client(tmp_path)
    monkeypatch.setattr(
        run_report, "renderer_unavailable_reason",
        lambda: "the PDF renderer (reportlab) is not installed on this deployment",
    )

    response = client.get(f"{_BASE}/r1/report.pdf")

    assert response.status_code == 404
    assert "reportlab" in response.json()["detail"]
    assert run_report.has_report(run_dir, completed=True) is False
    # A missing renderer is the deployment's state, not the run's: no failure is recorded.
    assert not (run_dir / run_report.REPORT_ERROR_FILENAME).exists()


@pytest.mark.skipif(_HAS_REPORTLAB, reason="checks the deployment without reportlab")
def test_here_reportlab_is_missing_so_the_report_is_404(tmp_path) -> None:
    client, _store, _run_dir = _client(tmp_path)

    response = client.get(f"{_BASE}/r1/report.pdf")

    assert response.status_code == 404
    assert "reportlab" in response.json()["detail"]


def test_the_report_is_409_until_the_run_completes(tmp_path) -> None:
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    app = FastAPI()
    app.include_router(create_v1_city_router(store, lambda *_a: None, output_root=tmp_path))
    client = TestClient(app)
    store.create("r1", origin="direct")
    store.set_running("r1")

    assert client.get(f"{_BASE}/r1/report.pdf").status_code == 409
    store.set_failed("r1", "boom")
    assert client.get(f"{_BASE}/r1/report.pdf").status_code == 409
    assert client.get(f"{_BASE}/nope/report.pdf").status_code == 404


def test_has_report_follows_the_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run_report, "renderer_unavailable_reason", lambda: None)
    run_dir = _finished_run(tmp_path, "r1", _static_plan(), seed_source="sumo_tls")
    assert run_report.has_report(run_dir, completed=False) is False
    assert run_report.has_report(run_dir, completed=True) is True  # renderable
    (run_dir / "nema_plan.json").unlink()
    assert run_report.has_report(run_dir, completed=True) is False
    (run_dir / run_report.REPORT_FILENAME).write_bytes(b"%PDF")
    assert run_report.has_report(run_dir, completed=True) is True  # rendered


@pytest.mark.skipif(not _HAS_REPORTLAB, reason="reportlab not installed (it is in the image)")
@pytest.mark.parametrize("stored", [False, True], ids=["static", "stored_nema"])
def test_the_real_renderer_writes_a_pdf(tmp_path, stored) -> None:
    plan, source = (_nema_plan(), "stored_nema") if stored else (_static_plan(), "sumo_tls")
    client, _store, run_dir = _client(tmp_path, plan, seed_source=source)

    response = client.get(f"{_BASE}/r1/report.pdf")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert (run_dir / run_report.REPORT_FILENAME).is_file()
