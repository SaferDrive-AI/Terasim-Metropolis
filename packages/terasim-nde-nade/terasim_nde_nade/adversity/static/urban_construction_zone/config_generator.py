"""
Construction Zone Config Generator

Generate simulation configs for construction zone scenarios.
Selects edges directly from network (no AV route required).

Usage:
    from config_generator import generate_config_without_av
    configs = generate_config_without_av(map_path="path/to/map", mode="auto")
"""

import random
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import sumolib
from omegaconf import OmegaConf, ListConfig

from loader import ConfigLoader


# ============================================================================
# Utility Functions
# ============================================================================

def _is_passenger_drivable_lane(lane) -> bool:
    """Check if lane allows passenger vehicles"""
    try:
        disallowed_lane = set(lane.getDisallowed() or [])
        if "passenger" in disallowed_lane:
            return False
    except Exception:
        pass

    try:
        edge = lane.getEdge()
        disallowed_edge = set(edge.getDisallowed() or [])
        if "passenger" in disallowed_edge:
            return False
    except Exception:
        pass

    try:
        allowed_lane = set(lane.getAllowed() or [])
        if allowed_lane and "passenger" not in allowed_lane:
            return False
    except Exception:
        pass

    try:
        func = (lane.getEdge().getFunction() or "normal").lower()
        forbidden_functions = {"walkingarea", "footpath", "cycleway", "railway", "parking"}
        if func in forbidden_functions:
            return False
    except Exception:
        pass

    try:
        width = float(lane.getWidth())
        if width > 0 and width < 2.0:
            return False
    except Exception:
        pass

    try:
        speed = float(lane.getSpeed())
        if speed > 0 and speed < 2.0:
            return False
    except Exception:
        pass

    return True


def _mph(v_m_s: float) -> float:
    """Convert m/s to mph"""
    return float(v_m_s) * 2.2369362921


def _warning_len_m(s_mph: float) -> float:
    """Calculate MUTCD warning zone length (meters) based on speed (mph)"""
    s = max(0.0, float(s_mph))
    return (
        60.0 if s <= 25 else
        90.0 if s <= 30 else
        110.0 if s <= 35 else
        150.0 if s <= 40 else
        200.0 if s <= 45 else
        250.0 if s <= 50 else
        300.0 if s <= 55 else
        350.0 if s <= 60 else
        400.0 if s <= 65 else
        450.0
    )


def _taper_total_m(w_m: float, s_mph: float) -> float:
    """Calculate MUTCD taper zone length (meters)"""
    W_ft = max(0.0, float(w_m)) * 3.280839895
    if s_mph <= 40.0:
        T_ft = W_ft * (s_mph ** 2) / 60.0
    elif s_mph >= 45.0:
        T_ft = W_ft * s_mph
    else:
        t = (s_mph - 40.0) / 5.0
        T1 = W_ft * (s_mph ** 2) / 60.0
        T2 = W_ft * s_mph
        T_ft = (1.0 - t) * T1 + t * T2
    return T_ft * 0.3048


def _get_next_sequence_number(output_dir: Path, map_id: str) -> int:
    """Get next available sequence number for output folders"""
    if not output_dir.exists():
        return 1

    import re
    pattern = re.compile(rf"^{re.escape(map_id)}_(\d+)_")

    max_seq = 0
    for item in output_dir.iterdir():
        if item.is_dir():
            match = pattern.match(item.name)
            if match:
                seq_num = int(match.group(1))
                max_seq = max(max_seq, seq_num)

    return max_seq + 1


# ============================================================================
# Edge Selection
# ============================================================================

def _pick_start_edge_from_network(
    map_dir: Path,
    required_lanes: int,
    lanes_to_close: Sequence[int],
    min_lane_length: float,
    max_candidates: int = 100,
    seed: Optional[int] = None
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Select suitable edge from network (no AV route required)"""
    net_path = Path(map_dir) / "map.net.xml"
    if not net_path.exists():
        print(f"[error] Network file not found: {net_path}")
        return None

    print(f"\n{'='*60}")
    print(f"Selecting construction edge from network")
    print(f"{'='*60}")

    net = sumolib.net.readNet(str(net_path), withInternal=False)
    rng = random.Random(seed)
    lane_indices = sorted({idx for idx in lanes_to_close if isinstance(idx, int) and idx >= 0})

    def get_edge_info(edge) -> Optional[Dict[str, Any]]:
        try:
            lanes = edge.getLanes()
            lane_count = len(lanes)
            lengths, widths, speeds, drivable_mask = [], [], [], []

            for lane in lanes:
                try:
                    lengths.append(float(lane.getLength()))
                    widths.append(float(lane.getWidth()))
                    speeds.append(float(lane.getSpeed()))
                    drivable_mask.append(_is_passenger_drivable_lane(lane))
                except Exception:
                    lengths.append(0.0)
                    widths.append(0.0)
                    speeds.append(0.0)
                    drivable_mask.append(False)

            drivable_count = sum(1 for d in drivable_mask if d)
            if drivable_count == 0:
                return None

            return {
                "lane_count": lane_count,
                "drivable_count": drivable_count,
                "lengths": lengths,
                "widths": widths,
                "speeds": speeds,
                "drivable_mask": drivable_mask,
                "avg_length": sum(lengths) / len(lengths) if lengths else 0,
                "avg_width": sum(widths) / len(widths) if widths else 0,
                "avg_speed_mph": (sum(speeds) / len(speeds) * 2.237) if speeds else 0
            }
        except Exception:
            return None

    def edge_meets_criteria(edge_info: Dict[str, Any]) -> bool:
        if edge_info["drivable_count"] < required_lanes:
            return False
        for idx in lane_indices:
            if idx >= len(edge_info["drivable_mask"]):
                return False
            if not edge_info["drivable_mask"][idx]:
                return False
            if edge_info["lengths"][idx] < min_lane_length:
                return False
        return True

    all_edges = [e for e in net.getEdges() if not e.getID().startswith(":")]
    rng.shuffle(all_edges)

    eligible_edges = []
    for edge in all_edges[:max_candidates]:
        edge_info = get_edge_info(edge)
        if edge_info and edge_meets_criteria(edge_info):
            eligible_edges.append((edge.getID(), edge_info))

    if not eligible_edges:
        print(f"No suitable edge found")
        return None

    selected_edge_id, selected_info = rng.choice(eligible_edges)
    print(f"Selected edge: {selected_edge_id}")
    return selected_edge_id, selected_info


# ============================================================================
# Static Construction Config
# ============================================================================

def build_static_construction_cfg(
    map_path: Path,
    template_filename: str,
    overrides: Mapping[str, Any] | None = None,
    selected_edge: str = None,
) -> Tuple[str, OmegaConf]:
    """Build static construction adversity config from template"""
    project_root = Path(__file__).resolve().parent
    tmpl_path = project_root / "configs" / "adversities" / "static" / template_filename
    if not tmpl_path.exists():
        raise FileNotFoundError(f"Template not found: {tmpl_path}")

    full_cfg = OmegaConf.load(tmpl_path)
    static_cfg = full_cfg.get("adversity_cfg", {}).get("static", {})
    items = list(static_cfg.items()) if hasattr(static_cfg, "items") else []
    if not items:
        raise ValueError(f"Static adversities not defined in template: {tmpl_path}")

    key, scen_cfg = items[0]
    scen_cfg = OmegaConf.create(scen_cfg)

    if overrides:
        scen_cfg = OmegaConf.merge(scen_cfg, OmegaConf.create(overrides))

    if not hasattr(scen_cfg, "corridor_definition") or scen_cfg.corridor_definition is None:
        scen_cfg.corridor_definition = OmegaConf.create({})

    construction_settings = scen_cfg.get("construction_settings") or {}
    lane_cfg = scen_cfg.get("lane_configuration") or {}

    lanes_to_close = lane_cfg.get("lanes_to_close", [0])
    if isinstance(lanes_to_close, ListConfig):
        lanes_iter = list(lanes_to_close)
    elif isinstance(lanes_to_close, (list, tuple)):
        lanes_iter = list(lanes_to_close)
    else:
        lanes_iter = [lanes_to_close]

    def _is_int(value):
        try:
            int(value)
            return True
        except (TypeError, ValueError):
            return False

    lane_indices = sorted({int(idx) for idx in lanes_iter if _is_int(idx) and int(idx) >= 0}) or [0]

    min_open = lane_cfg.get("minimum_open_lanes", 1)
    try:
        min_open_int = int(min_open)
    except (TypeError, ValueError):
        min_open_int = 1

    try:
        min_lane_length = float(construction_settings.get("min_lane_length", 30.0))
    except (TypeError, ValueError):
        min_lane_length = 30.0

    # Use provided edge or select from corridor_definition
    start_edge = selected_edge or scen_cfg.corridor_definition.get("start_edge")
    if not start_edge:
        return key, None

    scen_cfg.corridor_definition.start_edge = start_edge

    # Handle UrbanConstructionZone
    target = str(scen_cfg.get("_target_", ""))
    if "UrbanConstructionZone" in target:
        def _get_float(d: Mapping[str, Any], k: str, default: float | None = None):
            val = d.get(k, default)
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return default

        resolved = {
            "_target_": target,
            "_convert_": "all",
            "edges": [start_edge] if start_edge else [],
            "lanes_to_close": lane_indices,
            "min_open_lanes": min_open_int,
            "min_lane_length": _get_float(construction_settings, "min_lane_length", 30.0),
            "start_time": _get_float(construction_settings, "start_time", 0.0),
            "end_time": _get_float(construction_settings, "end_time", -1.0),
            "start_position": None,
            "end_position": None,
            "spacing": _get_float(construction_settings, "spacing", 20.0),
            "work_zone_offset": _get_float(construction_settings, "work_zone_offset", 0.0),
            "use_dynamic_spacing": bool(construction_settings.get("use_dynamic_spacing", False)),
            "construction_type": construction_settings.get("construction_type", "cone"),
            "allow_exceed_lane_boundary": True,
        }

        sp_mph = construction_settings.get("speed_limit_mph", construction_settings.get("speed_limit", None))
        try:
            resolved["speed_limit_mph"] = float(sp_mph) if sp_mph is not None else None
        except (TypeError, ValueError):
            resolved["speed_limit_mph"] = None

        # Calculate lane_plans
        net_path = Path(map_path) / "map.net.xml"
        net = sumolib.net.readNet(str(net_path), withInternal=False)
        try:
            edge = net.getEdge(start_edge)
        except Exception:
            return key, None
        if edge is None:
            return key, None

        lanes = edge.getLanes()
        rng_seed = hash((start_edge, tuple(lane_indices))) & 0xFFFFFFFF
        rng = random.Random(rng_seed)

        lane_plans: Dict[str, Dict[str, Any]] = {}
        for idx in lane_indices:
            if idx < 0 or idx >= len(lanes):
                continue
            lane = lanes[idx]
            if not _is_passenger_drivable_lane(lane):
                continue
            try:
                L = float(lane.getLength())
                lane_width_m = float(lane.getWidth())
                v_m_s = float(lane.getSpeed())
            except Exception:
                continue

            if L < min_lane_length:
                continue

            # Work zone width
            custom_width = construction_settings.get("work_zone_width", None)
            if custom_width is not None:
                try:
                    w_m = max(0.5, float(custom_width))
                except (TypeError, ValueError):
                    w_m = lane_width_m
            else:
                w_m = lane_width_m

            # Calculate work_zone_offset
            lane_right_edge = -(lane_width_m / 2 - 0.3)
            wz_offset = lane_right_edge + w_m

            s_mph = resolved.get("speed_limit_mph") or _mph(v_m_s)

            # Random start/end
            s = rng.uniform(0.0, max(0.0, L - 10.0))
            e = rng.uniform(s + 10.0, L)

            U = max(0.0, e - s)
            warning = _warning_len_m(s_mph)
            T = _taper_total_m(w_m, s_mph)
            tin = max(10.0, (2.0 / 3.0) * T)
            tout = max(5.0, (1.0 / 3.0) * T)

            total_taper = tin + tout
            cap = 0.5 * U
            if total_taper > cap and total_taper > 0:
                scale = cap / total_taper
                tin *= scale
                tout *= scale

            work = max(0.0, U - tin - tout)
            end_final = min(L, e)

            lane_id = f"{start_edge}_{idx}"
            lane_plans[lane_id] = {
                "start": s,
                "end": end_final,
                "warning": warning,
                "taper_in": tin,
                "work": work,
                "taper_out": tout,
                "zone_spacing": {"taper_in": 2.0, "work": 2.0, "taper_out": 2.0},
                "work_zone_offset": wz_offset,
            }

        resolved["lane_plans"] = lane_plans
        return key, OmegaConf.create(resolved)

    return key, scen_cfg


def inject_urban_construction(
    config: OmegaConf,
    map_path: Path,
    template_filename: str = "urban_construction_zone.yaml",
    overrides: Mapping[str, Any] | None = None,
    selected_edge: str = None,
) -> None:
    """Inject urban construction static adversity into config"""
    if not hasattr(config, "environment") or config.environment is None:
        config.environment = OmegaConf.create({})
    if not hasattr(config.environment, "parameters") or config.environment.parameters is None:
        config.environment.parameters = OmegaConf.create({})
    if not hasattr(config.environment.parameters, "adversity_cfg") or config.environment.parameters.adversity_cfg is None:
        config.environment.parameters.adversity_cfg = OmegaConf.create({})
    if not hasattr(config.environment.parameters.adversity_cfg, "static") or config.environment.parameters.adversity_cfg.static is None:
        config.environment.parameters.adversity_cfg.static = OmegaConf.create({})

    try:
        key, static_cfg = build_static_construction_cfg(
            map_path,
            template_filename=template_filename,
            overrides=overrides,
            selected_edge=selected_edge,
        )
        if static_cfg is None:
            print(f"[warn] Static adversity injection skipped: no suitable edge")
            return
        config.environment.parameters.adversity_cfg.static[key] = static_cfg
    except Exception as e:
        print(f"[warn] Static adversity injection skipped: {e}")


# ============================================================================
# Main Entry Point
# ============================================================================

def generate_config_without_av(
    map_path: Path,
    mode: str = "auto",
    num_configs: int = 1,
    lanes_to_close: List[int] = None,
    min_open_lanes: int = 1,
    min_lane_length: float = 30.0,
    work_zone_width: float = 2.0,
    construction_type: str = "cone",
    enable_warning_zone: bool = False,
    max_candidates: int = 100,
    seed: Optional[int] = None
) -> List[Path]:
    """
    Generate config files (no AV route required)

    Args:
        map_path: Map path (directory or map.net.xml)
        mode: "auto" or "manual"
        num_configs: Number of configs to generate
        lanes_to_close: Lane indices to close
        min_open_lanes: Minimum open lanes to keep
        min_lane_length: Minimum lane length
        work_zone_width: Work zone width
        construction_type: cone | barrier | mixed
        enable_warning_zone: Enable warning zone
        max_candidates: Max edges to check
        seed: Random seed

    Returns:
        List of generated config file paths
    """
    if lanes_to_close is None:
        lanes_to_close = [0]

    project_root = Path(__file__).resolve().parent
    config_loader = ConfigLoader(project_root)

    map_dir = Path(map_path)
    if map_dir.is_file():
        map_dir = map_dir.parent

    net_file = map_dir / "map.net.xml"
    if not net_file.exists():
        raise FileNotFoundError(f"Network file not found: {net_file}")

    if map_dir.name.isdigit():
        map_id = f"{map_dir.parent.name}_{map_dir.name}"
    else:
        map_id = map_dir.name

    output_folder = project_root / "config_yamls" / "generated" / "1"
    output_folder.mkdir(parents=True, exist_ok=True)

    required_lanes = max(min_open_lanes + len(lanes_to_close), 2)
    base_seed = seed if seed is not None else random.randint(0, 999999)

    outputs_dir = project_root / "outputs"
    start_seq = _get_next_sequence_number(outputs_dir, map_id)

    generated_paths = []

    for idx in range(num_configs):
        config_seed = base_seed + idx

        result = _pick_start_edge_from_network(
            map_dir,
            required_lanes=required_lanes,
            lanes_to_close=lanes_to_close,
            min_lane_length=min_lane_length,
            max_candidates=max_candidates,
            seed=config_seed
        )

        if result is None:
            print(f"Config {idx+1} failed: no suitable edge")
            continue

        selected_edge, edge_info = result

        overrides = {
            "corridor_definition": {"start_edge": selected_edge},
            "lane_configuration": {
                "lanes_to_close": lanes_to_close,
                "minimum_open_lanes": min_open_lanes
            },
            "construction_settings": {
                "start_end_mode": mode,
                "work_zone_width": work_zone_width,
                "construction_type": construction_type,
                "enable_warning_zone": enable_warning_zone,
                "min_lane_length": min_lane_length
            }
        }

        config = config_loader.load_config(
            map_dir,
            behavior_adversity_list=None,
            static_adversity_list=None,
        )

        inject_urban_construction(
            config, map_dir,
            template_filename="simple_urban_construction_zone.yaml",
            overrides=overrides,
            selected_edge=selected_edge,
        )

        if hasattr(config, "environment") and hasattr(config.environment, "parameters"):
            config.environment.parameters.run_time = 10
            config.environment.parameters.warmup_time_lb = 0
            config.environment.parameters.warmup_time_ub = 1

        rand_seed = random.randint(0, 99999)
        rand_sumo_seed = random.randint(0, 99999)
        config.seed = rand_seed
        if hasattr(config, "simulator"):
            if not hasattr(config.simulator, "parameters"):
                config.simulator.parameters = {}
            config.simulator.parameters.sumo_seed = rand_sumo_seed

        if not hasattr(config, "output"):
            config.output = OmegaConf.create({})
        seq_num = start_seq + idx
        config.output.name = f"{map_id}_{seq_num}_{rand_seed}"
        config.output.nth = f"{map_id}_{idx:03d}"

        out_file = output_folder / f"config_{map_id}_{idx:03d}.yaml"
        config_loader.save_config(config, str(out_file))
        generated_paths.append(out_file.resolve())

        print(f"[{idx+1}/{num_configs}] edge={selected_edge}, seed={rand_seed}")

    print(f"\nGenerated {len(generated_paths)}/{num_configs} configs")
    return generated_paths


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Construction Zone Config Generator")
    parser.add_argument("--map", type=str, required=True, help="Map path")
    parser.add_argument("--num", type=int, default=1, help="Number of configs")
    parser.add_argument("--lanes", type=str, default="0", help="Lanes to close (comma-separated)")
    parser.add_argument("--width", type=float, default=2.0, help="Work zone width")
    parser.add_argument("--seed", type=int, default=None, help="Random seed")

    args = parser.parse_args()

    lanes_to_close = [int(x) for x in args.lanes.split(",")]

    configs = generate_config_without_av(
        map_path=args.map,
        num_configs=args.num,
        lanes_to_close=lanes_to_close,
        work_zone_width=args.width,
        seed=args.seed
    )

    print(f"\nGenerated configs:")
    for cfg in configs:
        print(f"  {cfg}")
