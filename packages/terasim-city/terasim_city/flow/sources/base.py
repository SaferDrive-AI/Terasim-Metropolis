"""Abstract base class shared by all empirical flow sources.

The canonical empirical source is per-intersection turn-movement-count data
camera turning-movement counts, implemented by
:class:`~terasim_city.flow.sources.corridor_graph.CorridorGraphFlowSource`. The
base class purposefully avoids prescribing how the route generation is
implemented — each source can use whatever algorithm fits its input format.
The only contract is the input/output shape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class FlowSource(ABC):
    """Abstract contract for an empirical data source that emits SUMO routes.

    Subclasses must:

    * Expose a stable :pyattr:`source_name` for logging and registry lookups.
    * Implement :py:meth:`generate`, which performs the algorithm and writes
      a SUMO ``rou.xml`` file.
    """

    @property
    @abstractmethod
    def source_name(self) -> str:
        """A short stable identifier for the source type (e.g. ``"corridor_graph"``)."""

    @abstractmethod
    def generate(
        self,
        net_path: Path,
        output_path: Path,
        sim_end: float = 1800.0,
    ) -> Path:
        """Run the source-specific algorithm and write ``rou.xml`` to disk.

        Args:
            net_path: Path to the SUMO ``net.xml`` used to interpret the
                source data (e.g. to resolve lat/lon to edges).
            output_path: Destination ``rou.xml`` file. Parent directories are
                created if missing.
            sim_end: Simulation horizon in seconds. The source decides whether
                to clamp flow ``end`` attributes to this value.

        Returns:
            The same ``output_path`` (for chainable use).
        """
