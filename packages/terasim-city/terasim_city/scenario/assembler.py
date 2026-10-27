"""Combine a generated ``rou.xml`` with a base sumocfg into a runnable scenario.

The :class:`ScenarioAssembler` is intentionally tiny — it does not produce any
new business logic, only file plumbing. Separating it from
the flow source keeps responsibilities clean: the source generates
routes, the assembler attaches them to a usable SUMO configuration.
"""

from __future__ import annotations

from pathlib import Path

from terasim_city.scenario.sumocfg_io import (
    SumocfgInfo,
    parse_sumocfg,
    write_sumocfg,
)


class ScenarioAssembler:
    """Assemble a SUMO-runnable scenario from a routes file + base sumocfg.

    Typical use::

        info = ScenarioAssembler(sim_end=200, seed=42).assemble(
            routes_file=Path("routes.rou.xml"),
            base_sumocfg=Path("examples/corridor_demo/demo.sumocfg"),
            output_dir=Path("out/"),
        )

    The returned path points at the freshly written ``simulation.sumocfg``,
    which references the base scenario's ``net.xml`` (absolute path) and the
    routes file (absolute path) and inherits the base's processing/routing
    tuning.
    """

    def __init__(self, *, sim_end: float = 1800.0, seed: int = 42):
        """Construct an assembler with simulation defaults.

        Args:
            sim_end: Simulation end time written into the generated sumocfg.
            seed: Random seed pinned into the generated sumocfg for
                reproducible vehicle dispatch.
        """
        self.sim_end = sim_end
        self.seed = seed

    def assemble(
        self,
        routes_file: Path | str,
        base_sumocfg: Path | str,
        output_dir: Path | str,
    ) -> Path:
        """Write a new ``simulation.sumocfg`` linking ``routes_file`` to the base.

        Args:
            routes_file: The rou.xml just produced by a flow source.
            base_sumocfg: The pre-existing sumocfg whose net/tls/view we want
                to reuse. The base file is not modified.
            output_dir: Directory where the new sumocfg will be placed.
                Created if missing.

        Returns:
            Absolute path of the newly written ``simulation.sumocfg``.
        """
        routes_file = Path(routes_file).resolve()
        base_sumocfg = Path(base_sumocfg).resolve()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        info: SumocfgInfo = parse_sumocfg(base_sumocfg)

        new_sumocfg = output_dir / "simulation.sumocfg"
        return write_sumocfg(
            new_sumocfg,
            net_file=info.net_file,
            route_file=routes_file,
            additional_files=info.additional_files,
            gui_settings_file=info.gui_settings_file,
            processing_params=info.processing_params,
            routing_params=info.routing_params,
            report_params=info.report_params,
            sim_end=self.sim_end,
            seed=self.seed,
            step_length=info.step_length,
        )
