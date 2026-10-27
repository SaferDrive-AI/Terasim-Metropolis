"""The timing-report renderer is importable as a package module.

The MCP server renders the optimized plan as a signal-controller timing-report
style PDF inside the container, so the renderer lives in ``terasim_agent_skill``
(``python -m terasim_agent_skill.timing_report`` is its command line).
"""

from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("reportlab") is None,
    reason="reportlab not installed (pip install reportlab)",
)


def test_renderer_is_a_package_module():
    from terasim_agent_skill import timing_report

    assert timing_report.phase_mask([2, 6]) == "-2---6--"
    assert callable(timing_report.build_report)
    assert callable(timing_report.main)
