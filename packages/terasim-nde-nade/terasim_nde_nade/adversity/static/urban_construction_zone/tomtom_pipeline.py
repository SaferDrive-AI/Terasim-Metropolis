#!/usr/bin/env python3
"""
TomTom Construction Zone → TeraSim Pipeline

Fetch construction zone data from TomTom Traffic API, convert to TeraSim simulation configs and run.

Usage:
    # Use API to fetch real data
    python tomtom_pipeline.py --api-key YOUR_API_KEY

    # Use local JSON file (for testing)
    python tomtom_pipeline.py --input sample_incidents.json

    # Specify region (default Ann Arbor)
    python tomtom_pipeline.py --api-key KEY --bbox "-83.80,42.20,-83.60,42.35"
"""

import json
import argparse
import requests
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
import sumolib


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class ConstructionZone:
    """TomTom construction zone data"""
    id: str
    coordinates: List[Tuple[float, float]]  # [(lon, lat), ...]
    length: float  # meters
    description: str
    lanes_affected: int  # estimated affected lane count
    start_time: Optional[str] = None
    end_time: Optional[str] = None


@dataclass
class SUMOMatch:
    """SUMO map matching result"""
    edge_id: str
    lane_id: str
    start_pos: float
    end_pos: float
    lane_count: int


# =============================================================================
# TomTom API Fetching
# =============================================================================

def fetch_tomtom_incidents(
    api_key: str,
    bbox: str = "-83.80,42.20,-83.60,42.35",  # Ann Arbor default range
    category: int = 9,  # 9 = RoadWorks
) -> List[Dict[str, Any]]:
    """
    Fetch construction zone data from TomTom Traffic Incidents API

    Args:
        api_key: TomTom API key
        bbox: Bounding box "minLon,minLat,maxLon,maxLat"
        category: Event type (9=RoadWorks)

    Returns:
        List of incidents
    """
    url = "https://api.tomtom.com/traffic/services/5/incidentDetails"

    params = {
        "key": api_key,
        "bbox": bbox,
        "fields": "{incidents{type,geometry{type,coordinates},properties{id,iconCategory,magnitudeOfDelay,events{description,code},length,startTime,endTime}}}",
        "language": "en-US",
        "categoryFilter": str(category),
        "timeValidityFilter": "present",
    }

    print(f"Fetching TomTom incidents from bbox: {bbox}")

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        incidents = data.get("incidents", [])
        print(f"Found {len(incidents)} construction zone incidents")
        return incidents

    except requests.RequestException as e:
        print(f"API request failed: {e}")
        return []


def parse_tomtom_incidents(incidents: List[Dict[str, Any]]) -> List[ConstructionZone]:
    """
    Parse TomTom incident data into ConstructionZone objects

    Args:
        incidents: List of incidents returned by TomTom API

    Returns:
        List of ConstructionZone objects
    """
    zones = []

    for inc in incidents:
        try:
            props = inc.get("properties", {})
            geom = inc.get("geometry", {})

            # Only process RoadWorks (iconCategory=9)
            if props.get("iconCategory") != 9:
                continue

            # Extract coordinates
            coords = geom.get("coordinates", [])
            geom_type = geom.get("type", "")

            if geom_type == "Point":
                coordinates = [(coords[0], coords[1])]
            elif geom_type == "LineString":
                coordinates = [(c[0], c[1]) for c in coords]
            else:
                continue

            # Extract description
            events = props.get("events", [])
            description = events[0].get("description", "Road works") if events else "Road works"

            # Estimate affected lane count based on magnitudeOfDelay
            magnitude = props.get("magnitudeOfDelay", 0)
            lanes_affected = min(magnitude + 1, 3)  # Map 0-4 to 1-3 lanes

            zone = ConstructionZone(
                id=props.get("id", f"zone_{len(zones)}"),
                coordinates=coordinates,
                length=float(props.get("length", 100)),
                description=description,
                lanes_affected=lanes_affected,
                start_time=props.get("startTime"),
                end_time=props.get("endTime"),
            )
            zones.append(zone)

        except Exception as e:
            print(f"Failed to parse incident: {e}")
            continue

    print(f"Parsed {len(zones)} valid construction zones")
    return zones


# =============================================================================
# SUMO Map Matching
# =============================================================================

def match_to_sumo_network(
    zones: List[ConstructionZone],
    net_file: str,
    max_distance: float = 50.0,
) -> List[Tuple[ConstructionZone, SUMOMatch]]:
    """
    Match TomTom construction zones to SUMO road network

    Args:
        zones: List of ConstructionZone objects
        net_file: SUMO network file path
        max_distance: Maximum matching distance (meters)

    Returns:
        List of (ConstructionZone, SUMOMatch) tuples
    """
    print(f"Loading SUMO network: {net_file}")
    net = sumolib.net.readNet(net_file)

    matches = []

    for zone in zones:
        try:
            # ============================================================
            # 1. Determine start and end coordinates
            # ============================================================
            if len(zone.coordinates) >= 2:
                # LineString: has explicit start and end points
                start_lon, start_lat = zone.coordinates[0]
                end_lon, end_lat = zone.coordinates[-1]
            else:
                # Point: only one point, use length to estimate end point
                start_lon, start_lat = zone.coordinates[0]
                end_lon, end_lat = start_lon, start_lat  # compensate with length later

            # ============================================================
            # 2. Convert start coordinates, find nearest edge
            # ============================================================
            start_x, start_y = net.convertLonLat2XY(start_lon, start_lat)

            edges = net.getNeighboringEdges(start_x, start_y, max_distance)
            if not edges:
                print(f"  Zone {zone.id}: No nearby edge found")
                continue

            # Sort by distance, take the nearest non-internal edge
            edges = sorted(edges, key=lambda e: e[1])
            edge, dist = None, None
            for e, d in edges:
                if e.getFunction() != "internal":
                    edge, dist = e, d
                    break

            if edge is None:
                print(f"  Zone {zone.id}: Only internal edges found")
                continue

            edge_id = edge.getID()
            lane_count = edge.getLaneNumber()
            lane = edge.getLane(0)
            lane_length = lane.getLength()
            lane_id = f"{edge_id}_0"

            # ============================================================
            # 3. Calculate start position projection on edge (start_pos)
            # ============================================================
            # Get lane shape (coordinate sequence)
            lane_shape = lane.getShape()

            # Find nearest position on lane from start point
            start_pos = get_position_on_lane(lane_shape, start_x, start_y)

            # ============================================================
            # 4. Calculate end position projection on edge (end_pos)
            # ============================================================
            if len(zone.coordinates) >= 2:
                # Has explicit end coordinates
                end_x, end_y = net.convertLonLat2XY(end_lon, end_lat)
                end_pos = get_position_on_lane(lane_shape, end_x, end_y)
            else:
                # Only one point, use TomTom provided length
                end_pos = start_pos + zone.length

            # ============================================================
            # 5. Ensure start < end, and clip to valid range
            # ============================================================
            if start_pos > end_pos:
                start_pos, end_pos = end_pos, start_pos

            # Leave boundary margin
            start_pos = max(5.0, start_pos)
            end_pos = min(lane_length - 5.0, end_pos)

            # Ensure minimum length
            if end_pos - start_pos < 20:
                # If too short, expand from center point
                mid = (start_pos + end_pos) / 2
                half_len = max(zone.length / 2, 15)
                start_pos = max(5.0, mid - half_len)
                end_pos = min(lane_length - 5.0, mid + half_len)

            match = SUMOMatch(
                edge_id=edge_id,
                lane_id=lane_id,
                start_pos=start_pos,
                end_pos=end_pos,
                lane_count=lane_count,
            )

            matches.append((zone, match))
            print(f"  Zone {zone.id}: matched to {edge_id}")
            print(f"    TomTom coords: ({start_lon:.5f}, {start_lat:.5f}) -> ({end_lon:.5f}, {end_lat:.5f})")
            print(f"    SUMO position: {start_pos:.1f}m - {end_pos:.1f}m (length: {end_pos-start_pos:.1f}m)")
            print(f"    Match distance: {dist:.1f}m")

        except Exception as e:
            print(f"  Zone {zone.id}: match failed - {e}")
            continue

    print(f"Successfully matched {len(matches)}/{len(zones)} zones")
    return matches


def get_position_on_lane(lane_shape: List[Tuple[float, float]], x: float, y: float) -> float:
    """
    Calculate the projection position of point (x, y) on lane (distance along lane)

    Args:
        lane_shape: Lane shape point list [(x1,y1), (x2,y2), ...]
        x, y: Coordinates of point to project

    Returns:
        Distance from lane start (meters)
    """
    import math

    min_dist = float('inf')
    best_pos = 0.0
    cumulative_length = 0.0

    for i in range(len(lane_shape) - 1):
        x1, y1 = lane_shape[i]
        x2, y2 = lane_shape[i + 1]

        # Calculate segment length
        seg_len = math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
        if seg_len < 1e-6:
            cumulative_length += seg_len
            continue

        # Calculate point projection onto segment
        # Parameter t represents projection position on segment (0=start, 1=end)
        dx, dy = x2 - x1, y2 - y1
        t = max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / (seg_len * seg_len)))

        # Projection point coordinates
        proj_x = x1 + t * dx
        proj_y = y1 + t * dy

        # Calculate distance
        dist = math.sqrt((x - proj_x)**2 + (y - proj_y)**2)

        if dist < min_dist:
            min_dist = dist
            best_pos = cumulative_length + t * seg_len

        cumulative_length += seg_len

    return best_pos


# =============================================================================
# TeraSim Config Generation
# =============================================================================

def generate_terasim_config(
    matches: List[Tuple[ConstructionZone, SUMOMatch]],
    scene_dir: str,
    output_dir: str,
    work_zone_width: float = 2.0,
) -> List[Path]:
    """
    Generate TeraSim simulation config files

    Args:
        matches: List of (ConstructionZone, SUMOMatch) tuples
        scene_dir: Scene directory
        output_dir: Output directory
        work_zone_width: Work zone width

    Returns:
        List of generated config file paths
    """
    import yaml
    import random

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    config_files = []

    for i, (zone, match) in enumerate(matches):
        seed = random.randint(10000, 99999)

        # Calculate zone length
        total_length = match.end_pos - match.start_pos

        # Allocate segment lengths
        taper_in = min(total_length * 0.15, 10.0)
        taper_out = min(total_length * 0.15, 10.0)
        work = total_length - taper_in - taper_out
        warning = 0.0  # Simplified config, no warning zone

        # Build lane_plans
        lane_plans = {
            match.lane_id: {
                "start": float(match.start_pos),
                "end": float(match.end_pos),
                "warning": float(warning),
                "taper_in": float(taper_in),
                "work": float(max(work, 10.0)),
                "taper_out": float(taper_out),
                "zone_spacing": {
                    "taper_in": 2.0,
                    "work": 2.0,
                    "taper_out": 2.0,
                },
                "work_zone_offset": float(work_zone_width / 2),
            }
        }

        # Build adversity config (matching original format)
        adversity_cfg = {
            "static": {
                "simple_urban_construction_zone": {
                    "_convert_": "all",
                    "_target_": "terasim_nde_nade.adversity.static.urban_construction_simple.UrbanConstructionZone",
                    "allow_exceed_lane_boundary": True,
                    "construction_type": "cone",
                    "edges": [match.edge_id],
                    "end_position": None,
                    "end_time": -1.0,
                    "lane_plans": lane_plans,
                    "lanes_to_close": [0],
                    "min_lane_length": 30.0,
                    "min_open_lanes": 1,
                    "spacing": 20.0,
                    "speed_limit_mph": None,
                    "start_position": None,
                    "start_time": 0.0,
                    "use_dynamic_spacing": False,
                    "work_zone_offset": 0.0,
                }
            }
        }

        # Build complete config (matching pipeline.py expected structure)
        config = {
            "seed": seed,
            "input": {
                "sumo_net_file": f"{scene_dir}/map.net.xml",
                "sumo_config_file": f"{scene_dir}/simulation_no_traffic.sumocfg",
            },
            "output": {
                "dir": str(output_path),
                "name": f"tomtom_{zone.id}_{i}",
                "aggregated_dir": "aggregated",
                "nth": f"tomtom_{i:03d}",
            },
            "logging": {
                "levels": ["TRACE", "INFO"],
            },
            "simulator": {
                "class": "Simulator",
                "module": "terasim.simulator",
                "parameters": {
                    "gui_flag": False,
                    "num_tries": 10,
                    "realtime_flag": False,
                    "sumo_output_file_types": ["fcd_all", "collision", "tripinfo"],
                    "sumo_seed": seed,
                },
            },
            "environment": {
                "class": "NADE",
                "module": "terasim_nde_nade.envs",
                "parameters": {
                    "run_time": 10,
                    "warmup_time_lb": 0,
                    "warmup_time_ub": 1,
                    "log_flag": True,
                    "drive_rule": "righthand",
                    "MOBIL_lc_flag": True,
                    "stochastic_acc_flag": False,
                    "adversity_sampling_probability": 0.1,
                    "vehicle_factory": "terasim_nde_nade.vehicle.nde_vehicle_factory.NDEVehicleFactory",
                    "info_extractor": "terasim.logger.infoextractor.InfoExtractor",
                    "adversity_cfg": adversity_cfg,
                },
            },
        }

        # Save config
        config_file = output_path / f"config_tomtom_{i:03d}.yaml"
        with open(config_file, 'w') as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
        config_files.append(config_file)

        print(f"Generated config: {config_file.name}")
        print(f"  Zone: {zone.id} - {zone.description}")
        print(f"  Edge: {match.edge_id}, pos: {match.start_pos:.1f}-{match.end_pos:.1f}m")

    return config_files


# =============================================================================
# Run Simulations
# =============================================================================

def run_simulations(
    config_files: List[Path],
    gui_flag: bool = False,
    skip_waymo: bool = True,
) -> List[Dict[str, Any]]:
    """
    Run TeraSim simulations

    Args:
        config_files: List of config files
        gui_flag: Whether to enable GUI
        skip_waymo: Whether to skip Waymo conversion

    Returns:
        List of results
    """
    from pipeline import run_simulation, visualize_results

    results = []

    for config_file in config_files:
        print(f"\n{'='*60}")
        print(f"Running simulation: {config_file.name}")
        print(f"{'='*60}")

        try:
            output_dir = run_simulation(config_file, gui_flag=gui_flag)

            # Visualization
            scene_dir = Path(__file__).parent / "scenes/ann_arbor_whole"
            net_file = scene_dir / "map.net.xml"
            fcd_file = output_dir / "fcd_all.xml"
            vis_file = output_dir / "construction_zone.png"

            if fcd_file.exists():
                visualize_results(net_file, fcd_file, vis_file)

            results.append({
                "config": config_file,
                "output_dir": output_dir,
                "status": "success",
            })

        except Exception as e:
            print(f"Simulation failed: {e}")
            results.append({
                "config": config_file,
                "status": "failed",
                "error": str(e),
            })

    return results


# =============================================================================
# Sample Data (for testing)
# =============================================================================

def get_sample_incidents() -> List[Dict[str, Any]]:
    """Return sample TomTom incident data (for testing)"""
    return [
        {
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [-83.7382, 42.2808],
                    [-83.7365, 42.2812],
                ]
            },
            "properties": {
                "id": "sample_construction_1",
                "iconCategory": 9,
                "magnitudeOfDelay": 2,
                "length": 150,
                "events": [{"description": "Road construction - lane closure"}],
            }
        },
        {
            "geometry": {
                "type": "Point",
                "coordinates": [-83.7450, 42.2750]
            },
            "properties": {
                "id": "sample_construction_2",
                "iconCategory": 9,
                "magnitudeOfDelay": 1,
                "length": 80,
                "events": [{"description": "Utility work"}],
            }
        },
    ]


# =============================================================================
# Main Function
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="TomTom Construction Zone → TeraSim Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--api-key", type=str, default=None,
                        help="TomTom API key")
    parser.add_argument("--bbox", type=str, default="-83.80,42.20,-83.60,42.35",
                        help="Bounding box: minLon,minLat,maxLon,maxLat (default: Ann Arbor)")
    parser.add_argument("--input", type=str, default=None,
                        help="Input JSON file (instead of API)")
    parser.add_argument("--sample", action="store_true",
                        help="Use sample data for testing")
    parser.add_argument("--scene", type=str, default="ann_arbor_whole",
                        help="Scene name (default: ann_arbor_whole)")
    parser.add_argument("--output", type=str, default="outputs/tomtom",
                        help="Output directory")
    parser.add_argument("--gui", action="store_true",
                        help="Enable SUMO GUI")
    parser.add_argument("--config-only", action="store_true",
                        help="Only generate configs, don't run simulation")

    args = parser.parse_args()

    executor_dir = Path(__file__).parent
    scene_dir = executor_dir / "scenes" / args.scene
    net_file = scene_dir / "map.net.xml"

    if not net_file.exists():
        print(f"Error: Network file not found: {net_file}")
        return

    print("="*60)
    print("TomTom → TeraSim Construction Zone Pipeline")
    print("="*60)

    # 1. Get TomTom data
    if args.sample:
        print("\nUsing sample data...")
        incidents = get_sample_incidents()
    elif args.input:
        print(f"\nLoading from file: {args.input}")
        with open(args.input) as f:
            incidents = json.load(f)
    elif args.api_key:
        print(f"\nFetching from TomTom API...")
        incidents = fetch_tomtom_incidents(args.api_key, args.bbox)
    else:
        print("\nNo data source specified. Use --api-key, --input, or --sample")
        print("Using sample data for demonstration...")
        incidents = get_sample_incidents()

    if not incidents:
        print("No incidents found")
        return

    # 2. Parse construction zone data
    print("\nParsing construction zones...")
    zones = parse_tomtom_incidents(incidents)

    if not zones:
        print("No valid construction zones parsed")
        return

    # 3. Match to SUMO road network
    print("\nMatching to SUMO network...")
    matches = match_to_sumo_network(zones, str(net_file))

    if not matches:
        print("No zones matched to SUMO network")
        return

    # 4. Generate TeraSim configs
    print("\nGenerating TeraSim configs...")
    config_files = generate_terasim_config(
        matches,
        scene_dir=str(scene_dir),
        output_dir=str(executor_dir / args.output),
    )

    print(f"\nGenerated {len(config_files)} config files")

    if args.config_only:
        print("\n--config-only specified, skipping simulation")
        return

    # 5. Run simulations
    print("\nRunning simulations...")
    results = run_simulations(config_files, gui_flag=args.gui)

    # 6. Output summary
    print("\n" + "="*60)
    print("Pipeline Summary")
    print("="*60)
    success = sum(1 for r in results if r["status"] == "success")
    print(f"Total: {len(results)}, Success: {success}, Failed: {len(results)-success}")

    for r in results:
        status = "OK" if r["status"] == "success" else "FAILED"
        print(f"  [{status}] {r['config'].name}")
        if r["status"] == "success":
            print(f"       Output: {r['output_dir']}")


if __name__ == "__main__":
    main()
