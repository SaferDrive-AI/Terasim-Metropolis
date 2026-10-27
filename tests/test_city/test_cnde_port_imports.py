"""The NDE chain's Neural NDE (CNDE) additions import cleanly and are complete.

``terasim_nde_nade`` carries the CNDE command type, the ``NNDE`` environment
and the vehicle factory the city optimizer builds on; all of them must import
without the optional Neural NDE runtime. Runs in a fresh interpreter so the
environment switch below is in place before anything imports ``terasim_nde_nade``.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SNIPPET = """
import os
os.environ.setdefault("TERASIM_CNDE_ENABLED", "false")  # keep the NADE base-class switch off
from terasim_nde_nade.utils.base.types import CommandType
assert hasattr(CommandType, "CNDE_TRAJECTORY"), list(CommandType)
from terasim_nde_nade.envs.nnde import NNDE  # noqa: F401
from terasim_nde_nade.vehicle.nde_vehicle_factory import NDEVehicleFactory  # noqa: F401
print("PORT-IMPORT-OK")
"""


def test_ported_chain_imports():
    proc = subprocess.run([sys.executable, "-c", SNIPPET], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=300)
    assert "PORT-IMPORT-OK" in proc.stdout, (proc.stdout + proc.stderr)[-3000:]
