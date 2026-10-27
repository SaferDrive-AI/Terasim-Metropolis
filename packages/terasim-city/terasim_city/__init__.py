"""terasim-city: city traffic data integration for TeraSim.

Public exports re-expose the most common entry points so callers can simply
write `from terasim_city import run_tmc_pipeline`.
"""

from terasim_city.api import run_tmc_pipeline
from terasim_city.flow.descriptors import (
    FlowDescriptor,
    GeneratedScenario,
    TMCData,
    TMCIntersection,
)
from terasim_city.flow.generate import FlowGenerationResult, generate_flow
from terasim_city.flow.sources.base import FlowSource
from terasim_city.flow.sources.corridor_graph import CorridorGraphFlowSource
from terasim_city.scenario.assembler import ScenarioAssembler

__all__ = [
    "CorridorGraphFlowSource",
    "FlowDescriptor",
    "FlowGenerationResult",
    "FlowSource",
    "GeneratedScenario",
    "ScenarioAssembler",
    "TMCData",
    "TMCIntersection",
    "generate_flow",
    "run_tmc_pipeline",
]
__version__ = "0.1.0"
