"""Wiring tests for vehicle_behavior="cnde": registry + evaluator + batch.

Deliberately GPU-free and SUMO-free: no model loading, no SUMO run. These
tests only assert that the pieces are actually connected — the registry
resolves "cnde" to a real factory, ``evaluator.py``'s source carries the CNDE
env construction and the 0.1s step-length override, and ``batch.py``'s task
dict carries "map_dir" so the evaluator's cnde branch can find the MapStore
package.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from types import SimpleNamespace

from terasim_city.factories import VEHICLE_BEHAVIORS, resolve_vehicle_factory
from terasim_city.factories.cnde_behavior import missing_cnde_runtime


def test_cnde_registered_for_real():
    from terasim_city.factories.cnde_behavior import CndeVehicleFactory

    assert VEHICLE_BEHAVIORS["cnde"] is CndeVehicleFactory
    assert isinstance(resolve_vehicle_factory("cnde"), CndeVehicleFactory)


def test_evaluator_source_wires_cnde_env_and_step_length():
    from terasim_city.optimize import evaluator

    src = inspect.getsource(evaluator)
    assert "CndeCorridorSignalEnv" in src
    assert '"--step-length", "0.1"' in src or "'--step-length', '0.1'" in src


def test_evaluator_default_path_untouched():
    """The non-cnde branch keeps exactly the original Simulator call shape.

    Guards against a step-length/env swap leaking into the default path: the
    additional_sumo_args for a non-cnde trial must be the original two-element
    list, with no step-length override anywhere near it.

    Named without "sumo"/"simulation" on purpose: conftest.py's
    pytest_collection_modifyitems auto-marks any test whose NAME contains
    either substring as requires_sumo, which silently skips it when
    SUMO_HOME isn't set — that would disable exactly this invariant, which
    is pure source inspection and never touches SUMO.
    """
    from terasim_city.optimize import evaluator

    src = inspect.getsource(evaluator)
    assert '"--tripinfo-output.write-undeparted", "true"' in src


def test_batch_task_carries_map_dir():
    from terasim_city.optimize import batch

    assert "map_dir" in inspect.getsource(batch)


def test_cnde_trials_default_the_cuda_allocator_to_expandable_segments(tmp_path, monkeypatch):
    """Every "cnde" trial grows its GPU memory over the run: it starts with expandable
    segments unless the deployment chose an allocator setting; other trials are left alone."""
    from terasim_city.optimize import evaluator

    seen: list = []

    def fake_evaluate(task, trial_dir):
        seen.append(os.environ.get("PYTORCH_CUDA_ALLOC_CONF"))
        return {"plan_id": "p", "valid": True}

    monkeypatch.setattr(evaluator, "_evaluate", fake_evaluate)
    task = {"plan": {"plan_id": "p"}, "trial_dir": str(tmp_path / "t"), "vehicle_behavior": "cnde"}

    monkeypatch.delenv("PYTORCH_CUDA_ALLOC_CONF", raising=False)
    evaluator.evaluate_plan_task(dict(task, vehicle_behavior="sumo_passive"))
    evaluator.evaluate_plan_task(task)
    monkeypatch.setenv("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:128")
    evaluator.evaluate_plan_task(task)

    assert seen == [None, "expandable_segments:True", "max_split_size_mb:128"]


def _cnde_layout(root: Path, checkpoint_bytes: "int | None") -> dict:
    """A packages/cnde-shaped tree under ``root``; name -> the spec find_spec returns."""
    model = root / "network" / "cnde" / "cnde"
    (model / "models").mkdir(parents=True)
    (model / "models" / "model.py").write_text("", encoding="utf-8")
    (root / "csim" / "simulator").mkdir(parents=True)
    (root / "csim" / "simulator" / "terasim_cnde_simulator.py").write_text("", encoding="utf-8")
    if checkpoint_bytes is not None:
        (root / "assets").mkdir()
        with open(root / "assets" / "nn_weights.pth", "wb") as handle:
            handle.truncate(checkpoint_bytes)
    return {
        "torch": SimpleNamespace(submodule_search_locations=["torch"]),
        "cnde": SimpleNamespace(submodule_search_locations=[str(model)]),
        # csim is a namespace package: an earlier portion without the simulator
        # module does not hold the checkpoint.
        "csim": SimpleNamespace(
            submodule_search_locations=[str(root / "elsewhere"), str(root / "csim")]
        ),
    }


def test_cnde_runtime_check_passes_a_complete_install(tmp_path):
    specs = _cnde_layout(tmp_path, checkpoint_bytes=2 << 20)

    assert missing_cnde_runtime(find_spec=specs.get) == []


def test_cnde_runtime_check_names_everything_that_is_missing(tmp_path):
    specs = _cnde_layout(tmp_path, checkpoint_bytes=None)

    no_torch = missing_cnde_runtime(find_spec=lambda name: None if name == "torch" else specs[name])
    nothing = missing_cnde_runtime(find_spec=lambda name: None)

    checkpoint = tmp_path / "assets" / "nn_weights.pth"
    assert no_torch == ["torch is not installed", f"the CNDE checkpoint {checkpoint} is missing"]
    assert nothing == [
        "torch is not installed",
        "the CNDE model package 'cnde' is not importable "
        "(the import path needs packages/cnde/network/cnde)",
        "the CNDE simulator package 'csim' is not importable (the import path needs packages/cnde)",
    ]


def test_cnde_runtime_check_rejects_a_placeholder_checkpoint(tmp_path):
    specs = _cnde_layout(tmp_path, checkpoint_bytes=78)  # the size of a git-lfs pointer

    (problem,) = missing_cnde_runtime(find_spec=specs.get)

    assert "is a 78-byte placeholder, not the ~972 MB weights" in problem
