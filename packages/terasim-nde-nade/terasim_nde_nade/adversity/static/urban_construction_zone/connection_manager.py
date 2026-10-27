#!/usr/bin/env python3
"""
Automatic Connection Manager for Construction Zones

This module implements a two-phase approach to handle blocked turns:
- Phase 1 (Offline): Identify affected junctions and generate emergency connections
- Phase 2 (Runtime): Dynamically enable/disable connections via TraCI

When a construction zone blocks a turn (e.g., right turn), this system automatically:
1. Identifies the blocked junction and turn direction
2. Generates alternative connections from adjacent lanes
3. Modifies the network to include these "emergency connections" (initially disabled)
4. At runtime, disables blocked connections and enables emergency connections

Example:
    Normal state:     Lane 0 (right) → right turn ✓
                     Lane 1 (straight) → right turn ✗

    Construction:    Lane 0 (right) → right turn ✗ (blocked by cones)
                     Lane 1 (straight) → right turn ✓ (emergency connection enabled)
"""

import os
import sys
import math
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Set
from dataclasses import dataclass
from collections import defaultdict

try:
    import sumolib
except ImportError:
    print("Warning: sumolib not available")
    sumolib = None


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class BlockedConnection:
    """Represents a connection that will be blocked by construction"""
    from_edge: str
    to_edge: str
    from_lane: int
    to_lane: int
    junction_id: str
    turn_direction: str  # 'r', 'l', 's', 't' (right, left, straight, turnaround)
    internal_lane: Optional[str] = None  # e.g., ":junction_0_0"


@dataclass
class EmergencyConnection:
    """Represents an alternative connection to be added"""
    from_edge: str
    to_edge: str
    from_lane: int  # Alternative lane (e.g., straight lane for emergency right turn)
    to_lane: int
    junction_id: str
    original_blocked: BlockedConnection
    allow: str = ""  # Vehicle classes (empty = all disallowed by default)
    disallow: str = "all"  # Initially disallow all


@dataclass
class JunctionModification:
    """Describes all modifications needed for a junction"""
    junction_id: str
    blocked_connections: List[BlockedConnection]
    emergency_connections: List[EmergencyConnection]


# =============================================================================
# Phase 1: Network Preprocessing
# =============================================================================

class ConnectionManager:
    """
    Manages automatic generation and control of emergency connections
    for construction zones.
    """

    def __init__(self, net_file: str):
        """
        Initialize connection manager.

        Args:
            net_file: Path to SUMO .net.xml file
        """
        if not sumolib:
            raise RuntimeError("sumolib is required for ConnectionManager")

        self.net_file = Path(net_file)
        if not self.net_file.exists():
            raise FileNotFoundError(f"Network file not found: {net_file}")

        self.net = sumolib.net.readNet(str(self.net_file))
        self.scene_dir = self.net_file.parent

    def analyze_construction_zone(
        self,
        construction_edges: List[str],
        lanes_to_close: List[int]
    ) -> List[JunctionModification]:
        """
        Analyze construction zone and identify affected junctions.

        Args:
            construction_edges: List of edge IDs affected by construction
            lanes_to_close: Lane indices to be closed (e.g., [0] for rightmost)

        Returns:
            List of junction modifications needed
        """
        modifications = []

        for edge_id in construction_edges:
            edge = self.net.getEdge(edge_id)

            # Analyze outgoing junctions (where vehicles exit this edge)
            to_junction = edge.getToNode()
            junction_mods = self._analyze_junction_blockage(
                edge, to_junction, lanes_to_close
            )
            if junction_mods:
                modifications.append(junction_mods)

        return modifications

    def _analyze_junction_blockage(
        self,
        edge,
        junction,
        lanes_to_close: List[int]
    ) -> Optional[JunctionModification]:
        """
        Analyze how construction affects a specific junction.

        Args:
            edge: Incoming edge with construction
            junction: Junction node
            lanes_to_close: Indices of lanes being closed

        Returns:
            JunctionModification if changes needed, None otherwise
        """
        junction_id = junction.getID()
        blocked = []
        emergency = []

        # Get all connections from this edge (grouped by lane)
        # Note: edge.getConnections(None) doesn't work reliably, use lane.getOutgoing() instead
        conn_by_lane = defaultdict(list)
        for lane_idx in range(edge.getLaneNumber()):
            lane = edge.getLane(lane_idx)
            for conn in lane.getOutgoing():
                conn_by_lane[lane_idx].append(conn)

        # Identify blocked connections
        for lane_idx in lanes_to_close:
            if lane_idx not in conn_by_lane:
                continue

            for conn in conn_by_lane[lane_idx]:
                to_edge = conn.getTo()
                to_lane_idx = conn.getToLane().getIndex()
                direction = conn.getDirection()

                # Get internal lane ID
                via_lane_id = conn.getViaLaneID()

                blocked_conn = BlockedConnection(
                    from_edge=edge.getID(),
                    to_edge=to_edge.getID(),
                    from_lane=lane_idx,
                    to_lane=to_lane_idx,
                    junction_id=junction_id,
                    turn_direction=direction,
                    internal_lane=via_lane_id
                )
                blocked.append(blocked_conn)

                # Generate emergency connection from adjacent lane
                emergency_lane = self._find_emergency_lane(
                    edge, lane_idx, lanes_to_close, required_vclass="passenger"
                )
                if emergency_lane is not None:
                    emerg_conn = EmergencyConnection(
                        from_edge=edge.getID(),
                        to_edge=to_edge.getID(),
                        from_lane=emergency_lane,
                        to_lane=to_lane_idx,
                        junction_id=junction_id,
                        original_blocked=blocked_conn,
                        disallow=None  # Enabled by default for routing
                    )
                    emergency.append(emerg_conn)

        if blocked and emergency:
            return JunctionModification(
                junction_id=junction_id,
                blocked_connections=blocked,
                emergency_connections=emergency
            )

        return None

    def _find_emergency_lane(
        self,
        edge,
        blocked_lane_idx: int,
        all_closed_lanes: List[int],
        required_vclass: str = "passenger"
    ) -> Optional[int]:
        """
        Find an adjacent lane that can serve as emergency connection.

        Now filters out lanes that don't allow the required vehicle class
        (e.g., BRT lanes, emergency-only lanes).

        Prefers the nearest non-closed lane to the blocked lane.

        Args:
            edge: Edge object
            blocked_lane_idx: Index of blocked lane
            all_closed_lanes: All lanes being closed
            required_vclass: Required vehicle class (default: "passenger")

        Returns:
            Lane index to use for emergency, or None if not available
        """
        num_lanes = edge.getLaneNumber()

        # Try lanes in order of proximity to blocked lane
        candidates = []
        for i in range(num_lanes):
            if i in all_closed_lanes:
                continue

            lane = edge.getLane(i)

            # NEW: Check lane type compatibility
            if not self._is_lane_compatible(lane, required_vclass):
                continue

            distance = abs(i - blocked_lane_idx)
            candidates.append((distance, i))

        if candidates:
            candidates.sort()
            return candidates[0][1]

        return None

    def _is_lane_compatible(self, lane, vclass: str) -> bool:
        """
        Check if a lane is compatible with the given vehicle class.

        This filters out special lanes like BRT-only, emergency-only, etc.

        Args:
            lane: Lane object (sumolib)
            vclass: Vehicle class to check (e.g., "passenger")

        Returns:
            True if lane allows the vehicle class, False otherwise
        """
        # Use the built-in allows() method which handles both allow and disallow lists
        return lane.allows(vclass)

    def generate_enhanced_network(
        self,
        modifications: List[JunctionModification],
        output_net_file: Optional[str] = None
    ) -> Path:
        """
        Generate enhanced network with emergency connections.

        This is Phase 1 (Offline processing).

        Args:
            modifications: Junction modifications to apply
            output_net_file: Output network file (default: scene_dir/map_enhanced.net.xml)

        Returns:
            Path to generated enhanced network file
        """
        if output_net_file is None:
            output_net_file = self.scene_dir / "map_enhanced.net.xml"
        else:
            output_net_file = Path(output_net_file)

        # Step 1: Export to plain XML
        plain_prefix = self.scene_dir / "plain"
        self._export_plain_xml(plain_prefix)

        # Step 2: Modify connections
        con_file = Path(f"{plain_prefix}.con.xml")
        self._add_emergency_connections(con_file, modifications)

        # Step 3: Rebuild network
        self._rebuild_network(plain_prefix, output_net_file)

        print(f"Enhanced network generated: {output_net_file}")
        return output_net_file

    def _export_plain_xml(self, prefix: Path):
        """Export network to plain XML format."""
        cmd = [
            "netconvert",
            "-s", str(self.net_file),
            "--plain-output-prefix", str(prefix)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to export plain XML: {result.stderr}")

        print(f"  Exported plain XML: {prefix}.*")

    def _add_emergency_connections(
        self,
        con_file: Path,
        modifications: List[JunctionModification]
    ):
        """
        Add emergency connections to connection file.

        Args:
            con_file: Path to .con.xml file
            modifications: List of modifications to apply
        """
        # Parse existing connections
        tree = ET.parse(con_file)
        root = tree.getroot()

        # Add emergency connections
        for mod in modifications:
            for emerg in mod.emergency_connections:
                # Create new connection element
                conn_elem = ET.Element('connection')
                conn_elem.set('from', emerg.from_edge)
                conn_elem.set('to', emerg.to_edge)
                conn_elem.set('fromLane', str(emerg.from_lane))
                conn_elem.set('toLane', str(emerg.to_lane))

                # Only set disallow if it's specified (not None)
                if emerg.disallow is not None:
                    conn_elem.set('disallow', emerg.disallow)

                # Add comment for clarity
                comment = ET.Comment(
                    f' Emergency connection: replaces blocked lane {emerg.original_blocked.from_lane} '
                )
                root.append(comment)
                root.append(conn_elem)

        # Save modified connections
        tree.write(con_file, encoding='utf-8', xml_declaration=True)
        print(f"  Added {sum(len(m.emergency_connections) for m in modifications)} emergency connections")

    def _rebuild_network(self, plain_prefix: Path, output_file: Path):
        """Rebuild network from plain XML files."""
        cmd = [
            "netconvert",
            "--node-files", f"{plain_prefix}.nod.xml",
            "--edge-files", f"{plain_prefix}.edg.xml",
            "--connection-files", f"{plain_prefix}.con.xml",
            "--output", str(output_file)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to rebuild network: {result.stderr}")


# =============================================================================
# Phase 2: Runtime Connection Control
# =============================================================================

class RuntimeConnectionController:
    """
    Controls connections at runtime via TraCI.

    This is Phase 2 (Online control during simulation).
    """

    def __init__(self):
        """Initialize runtime controller."""
        self.active_modifications: List[JunctionModification] = []
        self.applied = False

    def register_modifications(self, modifications: List[JunctionModification]):
        """
        Register modifications to be applied at runtime.

        Args:
            modifications: Junction modifications from Phase 1 analysis
        """
        self.active_modifications = modifications

    def apply_construction_rerouting(self, traci_module):
        """
        Apply construction zone rerouting by enabling emergency connections.

        This should be called when construction zone is activated.

        Args:
            traci_module: The TraCI module instance (import traci)
        """
        if self.applied:
            return

        for mod in self.active_modifications:
            # Disable blocked connections via internal lanes
            for blocked in mod.blocked_connections:
                if blocked.internal_lane:
                    try:
                        traci_module.lane.setDisallowed(blocked.internal_lane, ["all"])
                        print(f"  Disabled: {blocked.internal_lane} ({blocked.turn_direction} turn)")
                    except Exception as e:
                        print(f"  Warning: Could not disable {blocked.internal_lane}: {e}")

            # Enable emergency connections
            # Note: We need to find the corresponding internal lanes for emergency connections
            # This requires querying the network at runtime
            for emerg in mod.emergency_connections:
                # Find internal lane for emergency connection
                internal_lane = self._find_internal_lane_for_connection(
                    traci_module, emerg
                )
                if internal_lane:
                    try:
                        traci_module.lane.setAllowed(internal_lane, ["passenger", "bus", "truck"])
                        print(f"  Enabled emergency: {internal_lane}")
                    except Exception as e:
                        print(f"  Warning: Could not enable {internal_lane}: {e}")

        self.applied = True
        print(f"Construction rerouting applied to {len(self.active_modifications)} junctions")

    def _find_internal_lane_for_connection(
        self,
        traci_module,
        emerg: EmergencyConnection
    ) -> Optional[str]:
        """
        Find internal lane ID for an emergency connection.

        This queries the live network to find the internal lane that
        corresponds to the from_edge -> to_edge connection via from_lane.

        Args:
            traci_module: TraCI module
            emerg: Emergency connection spec

        Returns:
            Internal lane ID (e.g., ":junction_5_0") or None
        """
        try:
            # Get lane object
            lane_id = f"{emerg.from_edge}_{emerg.from_lane}"
            links = traci_module.lane.getLinks(lane_id)

            # Find link to target edge
            for link in links:
                # link format: (toLane, viaLane, direction, state, ...)
                to_lane = link[0]
                via_lane = link[1] if len(link) > 1 else None

                # Check if this goes to target edge
                to_edge = to_lane.rsplit('_', 1)[0]
                if to_edge == emerg.to_edge and via_lane:
                    return via_lane
        except Exception as e:
            print(f"  Error finding internal lane: {e}")

        return None

    def remove_construction_rerouting(self, traci_module):
        """
        Restore normal connections after construction ends.

        Args:
            traci_module: The TraCI module instance
        """
        if not self.applied:
            return

        for mod in self.active_modifications:
            # Re-enable original connections
            for blocked in mod.blocked_connections:
                if blocked.internal_lane:
                    try:
                        traci_module.lane.setAllowed(blocked.internal_lane, [])
                    except Exception as e:
                        print(f"  Warning: Could not restore {blocked.internal_lane}: {e}")

            # Disable emergency connections
            for emerg in mod.emergency_connections:
                internal_lane = self._find_internal_lane_for_connection(
                    traci_module, emerg
                )
                if internal_lane:
                    try:
                        traci_module.lane.setDisallowed(internal_lane, ["all"])
                    except Exception as e:
                        print(f"  Warning: Could not disable emergency {internal_lane}: {e}")

        self.applied = False
        print("Construction rerouting removed")


# =============================================================================
# Integration Helper
# =============================================================================

def prepare_construction_zone_network(
    net_file: str,
    construction_edges: List[str],
    lanes_to_close: List[int],
    output_net_file: Optional[str] = None
) -> Tuple[Path, List[JunctionModification]]:
    """
    Convenience function: Prepare enhanced network for construction zone.

    This executes Phase 1 (offline preprocessing).

    Args:
        net_file: Original SUMO network file
        construction_edges: Edges affected by construction
        lanes_to_close: Lane indices to close (e.g., [0])
        output_net_file: Output enhanced network file

    Returns:
        (enhanced_network_path, modifications_list)

    Example:
        enhanced_net, mods = prepare_construction_zone_network(
            net_file="map.net.xml",
            construction_edges=["edge_123", "edge_456"],
            lanes_to_close=[0]
        )
        # Use enhanced_net in simulation config
    """
    manager = ConnectionManager(net_file)
    modifications = manager.analyze_construction_zone(construction_edges, lanes_to_close)

    print(f"\nAnalysis complete:")
    print(f"  Affected junctions: {len(modifications)}")
    print(f"  Blocked connections: {sum(len(m.blocked_connections) for m in modifications)}")
    print(f"  Emergency connections: {sum(len(m.emergency_connections) for m in modifications)}")

    enhanced_net = manager.generate_enhanced_network(modifications, output_net_file)

    return enhanced_net, modifications


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Automatic Connection Manager for Construction Zones"
    )
    parser.add_argument("--net", required=True, help="SUMO network file")
    parser.add_argument("--edges", required=True, help="Construction edges (comma-separated)")
    parser.add_argument("--lanes", default="0", help="Lanes to close (comma-separated, default: 0)")
    parser.add_argument("--output", help="Output enhanced network file")

    args = parser.parse_args()

    edges = [e.strip() for e in args.edges.split(",")]
    lanes = [int(l.strip()) for l in args.lanes.split(",")]

    enhanced_net, mods = prepare_construction_zone_network(
        net_file=args.net,
        construction_edges=edges,
        lanes_to_close=lanes,
        output_net_file=args.output
    )

    print(f"\n✓ Enhanced network ready: {enhanced_net}")
    print(f"✓ Use this network in your simulation configuration")
