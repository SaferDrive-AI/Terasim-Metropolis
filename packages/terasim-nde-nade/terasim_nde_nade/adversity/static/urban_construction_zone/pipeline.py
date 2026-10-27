#!/usr/bin/env python3
"""
Construction Zone Simulation Pipeline

Pipeline steps:
1. Generate config
2. Run simulation
3. Visualize results
4. Convert to Waymo format

Usage:
    python pipeline.py --scene ann_arbor_whole --num 3
"""

import sys
import argparse
from pathlib import Path
from typing import List, Optional, Dict, Any
from copy import deepcopy

executor_dir = Path(__file__).parent
sys.path.insert(0, str(executor_dir))


def generate_configs(
    scene_dir: str,
    num_configs: int = 1,
    lanes_to_close: List[int] = None,
    min_open_lanes: int = 1,
    min_lane_length: float = 30.0,
    work_zone_width: float = 2.0,
    construction_type: str = "cone",
    enable_warning_zone: bool = False,
    max_candidates: int = 100,
    seed: Optional[int] = None,
) -> List[Path]:
    """
    Step 1: Generate config files

    Args:
        scene_dir: Scene directory path
        num_configs: Number of configs to generate
        lanes_to_close: Lane indices to close
        seed: Random seed

    Returns:
        List of config file paths
    """
    from config_generator import generate_config_without_av

    print("\n" + "="*70)
    print("Step 1: Generate Config")
    print("="*70)

    if lanes_to_close is None:
        lanes_to_close = [0]

    configs = generate_config_without_av(
        map_path=scene_dir,
        mode="auto",
        num_configs=num_configs,
        lanes_to_close=lanes_to_close,
        min_open_lanes=min_open_lanes,
        min_lane_length=min_lane_length,
        work_zone_width=work_zone_width,
        construction_type=construction_type,
        enable_warning_zone=enable_warning_zone,
        max_candidates=max_candidates,
        seed=seed
    )

    print(f"Generated {len(configs)} config files")
    return configs


def run_simulation(config_file: Path, gui_flag: bool = False) -> Path:
    """
    Step 2: Run simulation

    Args:
        config_file: Config file path
        gui_flag: Enable GUI mode

    Returns:
        Output directory path
    """
    from loguru import logger
    from omegaconf import OmegaConf
    from terasim.logger.infoextractor import InfoExtractor
    from terasim.simulator import Simulator
    from terasim_nde_nade.envs import NADE
    from terasim_nde_nade.vehicle import NDEVehicleFactory
    from terasim_nde_nade.vru import NDEVulnerableRoadUserFactory
    from terasim_service.utils.base import resolve_config_paths

    print("\n" + "="*70)
    print("Step 2: Run Simulation")
    print("="*70)
    print(f"Config: {config_file}")
    print(f"GUI: {'ON' if gui_flag else 'OFF'}")

    # Load config
    config = OmegaConf.load(str(config_file))
    config_dict = OmegaConf.to_container(config, resolve=True)
    config_dict = resolve_config_paths(config_dict, str(config_file))
    config = OmegaConf.create(config_dict)

    # Create output directory
    base_dir = Path(config.output.dir) / config.output.name
    base_dir.mkdir(parents=True, exist_ok=True)

    print(f"Output dir: {base_dir}")
    print(f"Run time: {config.environment.parameters.run_time}s")

    # Create environment
    env = NADE(
        vehicle_factory=NDEVehicleFactory(cfg=config.environment.parameters),
        vru_factory=NDEVulnerableRoadUserFactory(cfg=config.environment.parameters),
        info_extractor=InfoExtractor,
        log_flag=True,
        log_dir=base_dir,
        warmup_time_lb=config.environment.parameters.warmup_time_lb,
        warmup_time_ub=config.environment.parameters.warmup_time_ub,
        run_time=config.environment.parameters.run_time,
        configuration=config.environment.parameters,
    )

    # Create simulator
    sim = Simulator(
        sumo_net_file_path=config.input.sumo_net_file,
        sumo_config_file_path=config.input.sumo_config_file,
        num_tries=10,
        gui_flag=gui_flag,
        realtime_flag=config.simulator.parameters.realtime_flag,
        output_path=base_dir,
        sumo_output_file_types=config.simulator.parameters.sumo_output_file_types,
        traffic_scale=getattr(config.simulator.parameters, "traffic_scale", 1),
        additional_sumo_args=["--device.bluelight.explicit", "true"],
    )
    sim.bind_env(env)

    # Run simulation
    terasim_logger = logger.bind(name="terasim_nde_nade")
    terasim_logger.info(f"Simulation started (GUI={'ON' if gui_flag else 'OFF'})")

    sim.run()

    print(f"\nSimulation complete! Output: {base_dir}")
    return base_dir


def visualize_results(
    net_file: Path,
    fcd_file: Path,
    output_file: Optional[Path] = None
) -> Optional[Path]:
    """
    Step 3: Visualize results

    Args:
        net_file: SUMO network file
        fcd_file: FCD trajectory file
        output_file: Output image path

    Returns:
        Generated image path
    """
    from visualizer import visualize_construction_zone

    print("\n" + "="*70)
    print("Step 3: Visualize Results")
    print("="*70)
    print(f"Network: {net_file}")
    print(f"FCD: {fcd_file}")

    visualize_construction_zone(
        net_file=str(net_file),
        fcd_file=str(fcd_file),
        output_file=str(output_file) if output_file else None
    )

    if output_file and output_file.exists():
        print(f"\nVisualization saved: {output_file}")
        return output_file
    return None


def convert_to_waymo(
    fcd_file: Path,
    output_file: Optional[Path] = None,
    start_at: float = 1.0,
    traj_length: int = 8,
    base_scenario=None,
    net_file: Optional[Path] = None
) -> Optional[Path]:
    """
    Step 4: Convert to Waymo format

    Args:
        fcd_file: FCD trajectory file
        output_file: Output file path
        start_at: Trajectory start time
        traj_length: Trajectory length in seconds
        base_scenario: Pre-converted map scenario (for reuse)
        net_file: SUMO network file (required when base_scenario is None)

    Returns:
        Generated Waymo .pb file path
    """
    print("\n" + "="*70)
    print("Step 4: Convert to Waymo Format")
    print("="*70)
    print(f"FCD: {fcd_file}")

    try:
        from terasim_datazoo.processors.sumo2waymo import SUMO2Waymo
        from terasim_datazoo.processors.trajectory_converter import TrajectoryConverter

        # Get or create scenario
        if base_scenario is not None:
            scenario_id = fcd_file.parent.name
            scenario = deepcopy(base_scenario)
            scenario.scenario_id = scenario_id
            print(f"Reusing cached map (ID: {scenario_id})")
        else:
            if net_file is None:
                raise ValueError("net_file required when base_scenario is None")
            print("Converting SUMO map...")
            map_converter = SUMO2Waymo(str(net_file))
            map_converter.parse(have_road_edges=True, have_road_lines=True)
            scenario_id = fcd_file.parent.name
            scenario = map_converter.convert_to_scenario(scenario_id=scenario_id)
            print("Map conversion complete")

        # Convert trajectory
        print("Converting trajectory...")
        traj_converter = TrajectoryConverter(str(fcd_file))
        traj_converter.parse(start_at=start_at, traj_length=traj_length)
        scenario = traj_converter.create_waymo_trajectory(scenario)
        print("Trajectory conversion complete")

        # Save
        if output_file is None:
            output_file = fcd_file.parent / "waymo_scenario.pb"

        with open(output_file, "wb") as f:
            f.write(scenario.SerializeToString())

        print(f"\nWaymo file saved: {output_file}")
        print(f"Size: {output_file.stat().st_size / 1024:.2f} KB")
        return output_file

    except Exception as e:
        print(f"\nWaymo conversion failed: {e}")
        import traceback
        traceback.print_exc()
        return None


def load_or_create_map_cache(scene_dir: Path) -> Any:
    """Load or create map cache for Waymo conversion"""
    net_file = scene_dir / "map.net.xml"
    cache_file = scene_dir / "map_waymo_cache.pb"

    if cache_file.exists():
        print(f"Found map cache: {cache_file.name}")
        try:
            from waymo_open_dataset.protos import scenario_pb2
            with open(cache_file, "rb") as f:
                scenario = scenario_pb2.Scenario()
                scenario.ParseFromString(f.read())
            print("Loaded from cache")
            return scenario
        except Exception as e:
            print(f"Cache load failed: {e}")

    # Create new cache
    try:
        from terasim_datazoo.processors.sumo2waymo import SUMO2Waymo
        print("Creating map cache...")
        map_converter = SUMO2Waymo(str(net_file))
        map_converter.parse(have_road_edges=True, have_road_lines=True)
        scenario = map_converter.convert_to_scenario(scenario_id="base_map")

        with open(cache_file, "wb") as f:
            f.write(scenario.SerializeToString())
        print(f"Map cache saved: {cache_file.name}")
        return scenario
    except Exception as e:
        print(f"Map conversion failed: {e}")
        return None


def run_pipeline(
    scene_name: str = "ann_arbor_whole",
    num_configs: int = 1,
    work_zone_width: float = 2.0,
    construction_type: str = "cone",
    enable_warning_zone: bool = False,
    gui_flag: bool = False,
    seed: Optional[int] = None,
    skip_visualization: bool = False,
    skip_waymo: bool = False,
) -> List[Dict[str, Any]]:
    """
    Run the complete pipeline

    Args:
        scene_name: Scene name
        num_configs: Number of configs
        work_zone_width: Work zone width in meters
        construction_type: Construction type (cone/barrier/mixed)
        enable_warning_zone: Enable warning zone
        gui_flag: Enable SUMO GUI
        seed: Random seed
        skip_visualization: Skip visualization step
        skip_waymo: Skip Waymo conversion step

    Returns:
        List of results
    """
    print("="*70)
    print("Construction Zone Simulation Pipeline")
    print("="*70)
    print(f"Scene: {scene_name}")
    print(f"Num configs: {num_configs}")
    print(f"Work zone width: {work_zone_width}m")
    print(f"Construction type: {construction_type}")
    print(f"GUI: {'ON' if gui_flag else 'OFF'}")
    print("="*70)

    scene_dir = executor_dir / "scenes" / scene_name

    # Step 1: Generate config
    config_files = generate_configs(
        scene_dir=str(scene_dir),
        num_configs=num_configs,
        lanes_to_close=[0],
        work_zone_width=work_zone_width,
        construction_type=construction_type,
        enable_warning_zone=enable_warning_zone,
        seed=seed
    )

    if not config_files:
        print("\nConfig generation failed")
        return []

    # Pre-load map cache
    base_scenario = None
    if not skip_waymo and len(config_files) > 0:
        print("\n" + "="*70)
        print("Pre-processing: Load Map Cache")
        print("="*70)
        base_scenario = load_or_create_map_cache(scene_dir)

    # Process each config
    results = []
    for i, config_file in enumerate(config_files):
        print("\n" + "#"*70)
        print(f"Processing config [{i+1}/{len(config_files)}]: {config_file.name}")
        print("#"*70)

        result = {"config": config_file}

        try:
            # Run simulation
            output_dir = run_simulation(config_file, gui_flag=gui_flag)
            result["output_dir"] = output_dir

            fcd_file = output_dir / "fcd_all.xml"
            if not fcd_file.exists():
                print(f"FCD file not found: {fcd_file}")
                result["status"] = "fcd_missing"
                results.append(result)
                continue

            net_file = scene_dir / "map.net.xml"

            # Visualization
            if not skip_visualization:
                vis_output = output_dir / "construction_zone.png"
                vis_file = visualize_results(net_file, fcd_file, vis_output)
                result["visualization"] = vis_file

            # Waymo conversion
            if not skip_waymo:
                waymo_output = output_dir / "waymo_scenario.pb"
                waymo_file = convert_to_waymo(
                    fcd_file=fcd_file,
                    output_file=waymo_output,
                    base_scenario=base_scenario,
                    net_file=net_file if base_scenario is None else None
                )
                result["waymo"] = waymo_file

            result["status"] = "success"
            print(f"\nConfig {i+1} complete!")

        except Exception as e:
            print(f"\nConfig {i+1} failed: {e}")
            import traceback
            traceback.print_exc()
            result["status"] = "failed"
            result["error"] = str(e)

        results.append(result)

    # Summary
    print("\n" + "="*70)
    print("Pipeline Summary")
    print("="*70)
    success_count = sum(1 for r in results if r.get("status") == "success")
    print(f"Total configs: {len(config_files)}")
    print(f"Success: {success_count}")
    print(f"Failed: {len(config_files) - success_count}")
    print("="*70)

    if results:
        print("\nOutput files:")
        for i, r in enumerate(results):
            print(f"\n[{i+1}] {r['config'].name}")
            if "output_dir" in r:
                print(f"    Output dir: {r['output_dir']}")
            if "visualization" in r and r["visualization"]:
                print(f"    Visualization: {r['visualization']}")
            if "waymo" in r and r["waymo"]:
                print(f"    Waymo: {r['waymo']}")

    print("\n" + "="*70)
    print("Pipeline complete!")
    print("="*70)

    return results


def main():
    """Command line entry point"""
    parser = argparse.ArgumentParser(
        description="Construction Zone Simulation Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pipeline.py --scene ann_arbor_whole --num 1
  python pipeline.py --scene ann_arbor_whole --gui
  python pipeline.py --scene ann_arbor_whole --num 3 --width 3.0 --type barrier
        """
    )

    parser.add_argument("--scene", type=str, default="ann_arbor_whole",
                        help="Scene name (default: ann_arbor_whole)")
    parser.add_argument("--num", type=int, default=1,
                        help="Number of configs (default: 1)")
    parser.add_argument("--width", type=float, default=2.0,
                        help="Work zone width (default: 2.0m)")
    parser.add_argument("--type", type=str, default="cone",
                        choices=["cone", "barrier", "mixed"],
                        help="Construction type (default: cone)")
    parser.add_argument("--warning", action="store_true",
                        help="Enable warning zone")
    parser.add_argument("--gui", action="store_true",
                        help="Enable SUMO GUI")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed")
    parser.add_argument("--skip-vis", action="store_true",
                        help="Skip visualization")
    parser.add_argument("--skip-waymo", action="store_true",
                        help="Skip Waymo conversion")

    args = parser.parse_args()

    run_pipeline(
        scene_name=args.scene,
        num_configs=args.num,
        work_zone_width=args.width,
        construction_type=args.type,
        enable_warning_zone=args.warning,
        gui_flag=args.gui,
        seed=args.seed,
        skip_visualization=args.skip_vis,
        skip_waymo=args.skip_waymo,
    )


if __name__ == "__main__":
    main()
