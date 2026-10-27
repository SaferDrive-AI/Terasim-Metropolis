"""Model-reuse interface of the optional Neural NDE runtime (signature checks only, no GPU).

terasim-city builds one Neural NDE simulator per trial around a model that is
loaded once per process (``terasim_city.factories.cnde_behavior``), and the
map conversion persists the route record the simulator reads. The runtime
(``csim`` / ``tools.dataset``, the ``cnde`` package) is an optional GPU module
installed separately; these checks skip when it is not installed.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect

import pytest

NEURAL_NDE_MISSING = "the optional Neural NDE runtime (cnde) is not installed"

requires_neural_nde = pytest.mark.skipif(
    importlib.util.find_spec("csim") is None, reason=NEURAL_NDE_MISSING
)


def _simulator_module():
    """Import the runtime's simulator module the way terasim-city does."""
    import torch  # noqa: F401  (loads libc10 before the runtime's compiled extension)

    return importlib.import_module("csim.simulator.terasim_cnde_simulator")


@requires_neural_nde
def test_build_nnde_simulator_accepts_model_kwarg():
    params = inspect.signature(_simulator_module().build_nnde_simulator).parameters
    assert list(params)[:3] == ["map_path", "desc", "model"]
    assert params["model"].default is None


@requires_neural_nde
def test_load_cnde_model_exists():
    assert callable(_simulator_module().load_cnde_model)


@requires_neural_nde
def test_x2cnde_persists_routes():
    # Located through the import system, read without executing it (the module
    # pulls in the heavy conversion dependencies at import time).
    spec = importlib.util.find_spec("tools.dataset.x2cnde")
    assert spec is not None, "tools.dataset.x2cnde is missing from the Neural NDE runtime"
    src = spec.loader.get_source(spec.name)
    assert "RouteVectorMap" in src and '"routes"' in src
