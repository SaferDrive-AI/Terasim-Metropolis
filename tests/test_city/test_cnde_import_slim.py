"""The Neural NDE simulator imports without the nuplan-devkit / tuplan_garage chain.

The safety layer (``_safety_layer_forward()``) is not disabled system-wide: its
call site is commented out in the base class
``RuleEnhancedController.post_process()`` but active in the subclass
``IDM_MOBIL_AVController.post_process()``. ``RuleController`` and its
nuplan / tuplan / torch_cluster / igraph dependency chain must therefore never
be forced at import time: the runtime's ``controller.py`` degrades
``RuleController`` to ``None`` behind a try/except, and
``_safety_layer_forward()`` returns early when ``self.rule_controller is None``,
so both ``post_process`` paths degrade gracefully when those dependencies are
missing instead of failing at import time.

The runtime (``csim``, the ``cnde`` package) is an optional GPU module
installed separately; this check skips when it is not installed.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

import pytest

NEURAL_NDE_MISSING = "the optional Neural NDE runtime (cnde) is not installed"

# Runs in a fresh interpreter: a meta-path finder makes the optional chain
# look uninstalled before anything imports it.
BLOCKER = """
import importlib.abc
import sys

class _Blocker(importlib.abc.MetaPathFinder):
    BLOCKED = ("nuplan", "tuplan_garage", "torch_cluster", "igraph")

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.BLOCKED:
            raise ModuleNotFoundError(f"blocked: {name}", name=name)
        return None

sys.meta_path.insert(0, _Blocker())
import torch  # noqa: F401  (loads libc10.so first)
from csim.simulator.terasim_cnde_simulator import build_nnde_simulator  # noqa: F401
print("SLIM-IMPORT-OK")
"""


@pytest.mark.skipif(importlib.util.find_spec("csim") is None, reason=NEURAL_NDE_MISSING)
def test_csim_imports_without_nuplan_chain():
    # Same interpreter, same environment (PYTHONPATH included) as this process.
    proc = subprocess.run([sys.executable, "-c", BLOCKER], capture_output=True, text=True,
                          env=dict(os.environ), timeout=300)
    assert "SLIM-IMPORT-OK" in proc.stdout, proc.stderr[-2000:]
