"""Entry points TeraSim imports: ``load_cnde_model`` and ``build_nnde_simulator``.

The checkpoint is read from ``<runtime>/assets/nn_weights.pth``, where TeraSim's runtime check
(``terasim_city.factories.cnde_behavior.missing_cnde_runtime``) also looks for it.
"""

from __future__ import annotations

from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
CHECKPOINT = RUNTIME_ROOT / "assets" / "nn_weights.pth"


def load_cnde_model():
    from open_nnde.mtr_backend import load_predictor

    return load_predictor(CHECKPOINT)


def build_nnde_simulator(map_path=None, desc="terasim-cnde", model=None, **_):
    """A fresh simulator for one run. ``map_path``: the package's cnde_map.pkl (or its net)."""
    from open_nnde.simulator import OpenNndeSimulator, map_for

    if map_path is None:
        raise ValueError("build_nnde_simulator needs map_path")
    return OpenNndeSimulator(
        map_for(map_path), model if model is not None else load_cnde_model(), desc=desc
    )
