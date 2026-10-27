from pathlib import Path
from omegaconf import OmegaConf
import os
import yaml
import random
import xml.etree.ElementTree as ET
import sumolib

def get_lane_id_pos(map_path: Path):
    """
    Given map_path, parse vehicle.rou.xml to get av_route,
    randomly select an edge and any lane, generate and return (lane_id, lane_position),
    logic is consistent with steps 6-7 in generate_static_adv_config.
    """
    # 1. Load SUMO network (including internal edges)
    net = sumolib.net.readNet(str(map_path / "map.net.xml"), with_internal=True)

    # 2. Parse vehicle.rou.xml to get av_route edges
    rou_file = map_path / "vehicles.rou.xml"
    tree = ET.parse(str(rou_file))
    root = tree.getroot()
    route_elem = root.find(".//route[@id='av_route']")
    if route_elem is None or "edges" not in route_elem.attrib:
        raise RuntimeError("Could not find <route> tag with id='av_route' or edges attribute")
    edges = route_elem.attrib["edges"].split()

    # 3. Filter out all internal edges starting with ':'
    valid_edges = [e for e in edges if not e.startswith(':')]
    if not valid_edges:
        # If all are internal edges, fall back to original list
        valid_edges = edges

    # 4. Randomly select edge
    chosen = random.choice(valid_edges)
    print(f"Randomly selected edge: {chosen}, map_path: {map_path}")

    edge_obj = net.getEdge(chosen)

    if edge_obj is None:
        raise RuntimeError(f"Edge {chosen} does not exist in network")

    # 5. Randomly select a lane from the edge
    lanes = edge_obj.getLanes()
    if not lanes:
        raise RuntimeError(f"Edge {chosen} has no lanes")
    lane = random.choice(lanes)
    lane_id = lane.getID()

    # 6. Randomly generate lane_position
    length = lane.getLength()
    lane_position = random.uniform(0, length)

    return lane_id, lane_position

class ConfigLoader:
    def __init__(self, project_root=None):
        """
        Initialize configuration loader
        
        Args:
            project_root (Path, optional): Project root directory path. If None, will be auto-detected.
        """
        self.project_root = project_root
        self.config_dir = self.project_root / "configs"
        self.examples_dir = self.project_root / "examples"
    
    def load_config(self, map_path, behavior_adversity_list=None, static_adversity_list=None):
        """
        Load and merge configurations
        
        Args:
            map_id (str): Map ID
            adversity_name (str, optional): Specific scenario name
            
        Returns:
            OmegaConf: Merged configuration
        """
        # 1. Load base configurations
        base_config = OmegaConf.load(self.config_dir / "base.yaml")
        env_config = OmegaConf.load(self.config_dir / "environment.yaml")
        av_config = OmegaConf.load(self.config_dir / "av_config.yaml") # This is the default AV config and can be overrided by the map-specific config
        
        map_av_config_path = Path(map_path) / "config" / "av_config.yaml"
        if map_av_config_path.exists():
            map_av_config = OmegaConf.load(map_av_config_path)
            av_config = OmegaConf.merge(av_config, map_av_config)
        
        # 4. Set map file paths
        input_cfg = {
            "input": {
                "sumo_net_file": str(Path(map_path) / "map.net.xml"),
                #"sumo_config_file": str(Path(map_path) / "sumo_medium.sumocfg")
                #"sumo_config_file": str(Path(map_path) / "simulation.sumocfg"),  # With background traffic
                "sumo_config_file": str(Path(map_path) / "simulation_no_traffic.sumocfg"),  # No background traffic, only cones
            }
        }
        input_config = OmegaConf.create(input_cfg)
        
        # 5. Merge base configurations
        config = OmegaConf.merge(
            base_config,
            env_config,#
            input_config,
            av_config
        )
        
        # 6. Load scenario config if specified
        if static_adversity_list:
            static_map = {}
            for idx, static_adversity in enumerate(static_adversity_list):
                adv_path = self.config_dir / "adversities" / "static" / f"{static_adversity}.yaml"
                full_cfg = OmegaConf.load(adv_path) if adv_path.exists() else OmegaConf.create()
                # Extract "adversity_cfg.static.<static_adversity>" level
                scen_cfg = (
                    full_cfg.get("adversity_cfg", {})
                            .get("static", {})
                            .get(static_adversity, OmegaConf.create())
                )

                # For construction / stalled_object, randomly generate lane
                if static_adversity in ("construction", "stalled_object"):
                    lane_id, lane_pos = get_lane_id_pos(Path(map_path))
                    if "lane_id" in scen_cfg:
                        scen_cfg.lane_id = lane_id
                    if "lane_position" in scen_cfg:
                        scen_cfg.lane_position = lane_pos

                # Add suffix when key name is duplicated
                count_before = static_adversity_list[:idx].count(static_adversity)
                key = static_adversity if count_before == 0 else f"{static_adversity}_{count_before}"

                if static_adversity == "stalled_object":
                    suffix = count_before + 1
                    if "object_type" in scen_cfg:
                        scen_cfg.object_type = f"{scen_cfg.object_type}_{suffix}"
                static_map[key] = scen_cfg

            # Ensure adversity_cfg.static path exists, then replace entirely
            config.environment = getattr(config, "environment", {})
            config.environment.parameters = getattr(config.environment, "parameters", {})
            config.environment.parameters.adversity_cfg = getattr(
                config.environment.parameters, "adversity_cfg", {}
            )
            config.environment.parameters.adversity_cfg.static = static_map

        if behavior_adversity_list:
            for behavior_adversity in behavior_adversity_list:
                adv_path = self.config_dir / "adversities" / "vehicle" / f"{behavior_adversity}.yaml"
                if adv_path.exists():
                    adv_config = OmegaConf.load(adv_path)
                else:
                    adv_config = OmegaConf.create()
                # Merge adversity config under environment.parameters
                if "environment" not in config:
                    config.environment = {}
                if "parameters" not in config.environment:
                    config.environment.parameters = {}
                config.environment.parameters = OmegaConf.merge(config.environment.parameters, adv_config)
        return config

    def save_config(self, config, output_path):
        """
        Save configuration to file
        
        Args:
            config (OmegaConf): Configuration object
            output_path (str): Output file path
        """
        with open(output_path, "w") as f:
            yaml.dump(OmegaConf.to_container(config), f)


# Usage example
if __name__ == "__main__":
    # Initialize config loader
    config_loader = ConfigLoader()
    
    # Load base config (map 0)
    base_config = config_loader.load_config("0")
    
    # Load specific scenario config
    # highway_config = config_loader.load_config("0", "highway_1")
    
    # Save config file
    config_loader.save_config(base_config, "dynamic_config.yaml") 