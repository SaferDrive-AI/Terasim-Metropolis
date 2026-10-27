#!/usr/bin/env python3
"""
Test script for automatic connection management system.

This demonstrates the two-phase approach for handling blocked turns
in construction zones.
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from connection_manager import (
    ConnectionManager,
    RuntimeConnectionController,
    prepare_construction_zone_network
)


def test_basic_workflow():
    """Test the complete workflow with a sample network."""
    print("="*70)
    print("Test: Basic Connection Management Workflow")
    print("="*70)

    # Example network file (replace with your actual network)
    net_file = Path(__file__).parent / "scenes" / "ann_arbor_whole" / "map.net.xml"

    if not net_file.exists():
        print(f"⚠ Network file not found: {net_file}")
        print("  Please provide a valid network file to test.")
        return

    # Example: Construction on edge "12345" closing lane 0 (rightmost)
    construction_edges = ["12345"]  # Replace with actual edge IDs from your network
    lanes_to_close = [0]

    print(f"\n📋 Configuration:")
    print(f"   Network: {net_file.name}")
    print(f"   Construction edges: {construction_edges}")
    print(f"   Lanes to close: {lanes_to_close}")

    # =========================================================================
    # Phase 1: Offline Preprocessing
    # =========================================================================
    print(f"\n{'='*70}")
    print("PHASE 1: Offline Network Preprocessing")
    print('='*70)

    try:
        enhanced_net, modifications = prepare_construction_zone_network(
            net_file=str(net_file),
            construction_edges=construction_edges,
            lanes_to_close=lanes_to_close
        )

        print(f"\n✓ Phase 1 complete!")
        print(f"  Enhanced network: {enhanced_net}")
        print(f"\n📊 Analysis results:")

        for i, mod in enumerate(modifications, 1):
            print(f"\n  Junction {i}: {mod.junction_id}")
            print(f"    Blocked connections: {len(mod.blocked_connections)}")

            for blocked in mod.blocked_connections:
                print(f"      ✗ {blocked.from_edge}[lane {blocked.from_lane}] "
                      f"→ {blocked.to_edge} ({blocked.turn_direction} turn)")
                if blocked.internal_lane:
                    print(f"        Internal lane: {blocked.internal_lane}")

            print(f"    Emergency connections: {len(mod.emergency_connections)}")

            for emerg in mod.emergency_connections:
                print(f"      ✓ {emerg.from_edge}[lane {emerg.from_lane}] "
                      f"→ {emerg.to_edge} (replaces lane {emerg.original_blocked.from_lane})")

        # =========================================================================
        # Phase 2: Runtime Control (Simulation)
        # =========================================================================
        print(f"\n{'='*70}")
        print("PHASE 2: Runtime Connection Control")
        print('='*70)

        controller = RuntimeConnectionController()
        controller.register_modifications(modifications)

        print("\n📝 Controller initialized with modifications")
        print("   In actual simulation, call:")
        print("   controller.apply_construction_rerouting(traci)")
        print("\n   This will:")
        print("   1. Disable blocked internal lanes via traci.lane.setDisallowed()")
        print("   2. Enable emergency internal lanes via traci.lane.setAllowed()")

        # Simulate what would happen at runtime
        print("\n🔄 Simulated runtime actions:")
        for mod in modifications:
            print(f"\n  At junction {mod.junction_id}:")
            for blocked in mod.blocked_connections:
                if blocked.internal_lane:
                    print(f"    Would execute: traci.lane.setDisallowed('{blocked.internal_lane}', ['all'])")

            for emerg in mod.emergency_connections:
                print(f"    Would execute: traci.lane.setAllowed('<internal_lane_for_emerg>', ['passenger', 'bus'])")

        print("\n✓ Test completed successfully!")

    except FileNotFoundError as e:
        print(f"\n✗ Error: {e}")
        print("  Please ensure the network file exists and contains the specified edges.")
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()


def test_list_junctions():
    """List all junctions in a network for testing."""
    print("="*70)
    print("Test: List Network Junctions")
    print("="*70)

    net_file = Path(__file__).parent / "scenes" / "ann_arbor_whole" / "map.net.xml"

    if not net_file.exists():
        print(f"⚠ Network file not found: {net_file}")
        return

    try:
        import sumolib

        net = sumolib.net.readNet(str(net_file))
        junctions = net.getNodes()

        print(f"\nFound {len(junctions)} junctions in network")
        print("\nSample junctions (first 10):")

        for i, junction in enumerate(list(junctions)[:10], 1):
            j_id = junction.getID()
            incoming = junction.getIncoming()
            outgoing = junction.getOutgoing()

            print(f"\n  {i}. Junction: {j_id}")
            print(f"     Type: {junction.getType()}")
            print(f"     Incoming edges: {len(incoming)} ({', '.join([e.getID() for e in incoming[:3]])}...)")
            print(f"     Outgoing edges: {len(outgoing)} ({', '.join([e.getID() for e in outgoing[:3]])}...)")

            # Show connections from first incoming edge
            if incoming:
                first_edge = incoming[0]
                connections = first_edge.getConnections(None)
                print(f"     Sample connections from {first_edge.getID()}: {len(connections)}")
                for conn in connections[:3]:
                    print(f"       - Lane {conn.getFromLane().getIndex()} → "
                          f"{conn.getTo().getID()} (lane {conn.getToLane().getIndex()}, "
                          f"dir: {conn.getDirection()})")

    except ImportError:
        print("✗ sumolib not available")
    except Exception as e:
        print(f"✗ Error: {e}")


def test_analyze_specific_edge():
    """Analyze connections for a specific edge."""
    print("="*70)
    print("Test: Analyze Specific Edge")
    print("="*70)

    net_file = Path(__file__).parent / "scenes" / "ann_arbor_whole" / "map.net.xml"
    edge_id = input("\nEnter edge ID to analyze: ").strip()

    if not edge_id:
        print("No edge ID provided")
        return

    if not net_file.exists():
        print(f"⚠ Network file not found: {net_file}")
        return

    try:
        import sumolib

        net = sumolib.net.readNet(str(net_file))
        edge = net.getEdge(edge_id)

        print(f"\n📍 Edge: {edge.getID()}")
        print(f"   Function: {edge.getFunction()}")
        print(f"   Number of lanes: {edge.getLaneNumber()}")
        print(f"   From junction: {edge.getFromNode().getID()}")
        print(f"   To junction: {edge.getToNode().getID()}")

        connections = edge.getConnections(None)
        print(f"\n🔗 Outgoing connections: {len(connections)}")

        # Group by from_lane
        from collections import defaultdict
        by_lane = defaultdict(list)

        for conn in connections:
            from_lane = conn.getFromLane().getIndex()
            by_lane[from_lane].append(conn)

        for lane_idx in sorted(by_lane.keys()):
            print(f"\n   Lane {lane_idx}:")
            for conn in by_lane[lane_idx]:
                print(f"      → {conn.getTo().getID()} "
                      f"(lane {conn.getToLane().getIndex()}, "
                      f"direction: {conn.getDirection()}, "
                      f"via: {conn.getViaLaneID()})")

        # Simulate what would happen if lane 0 is closed
        print(f"\n🚧 Simulation: If lane 0 is closed by construction...")

        manager = ConnectionManager(str(net_file))
        junction = edge.getToNode()
        mod = manager._analyze_junction_blockage(edge, junction, [0])

        if mod:
            print(f"   Blocked connections: {len(mod.blocked_connections)}")
            for blocked in mod.blocked_connections:
                print(f"      ✗ Lane {blocked.from_lane} → {blocked.to_edge} ({blocked.turn_direction})")

            print(f"   Emergency connections would be created: {len(mod.emergency_connections)}")
            for emerg in mod.emergency_connections:
                print(f"      ✓ Lane {emerg.from_lane} → {emerg.to_edge} (emergency)")
        else:
            print("   No modifications needed")

    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test connection management system")
    parser.add_argument("--mode", choices=["workflow", "junctions", "edge"],
                        default="workflow",
                        help="Test mode (default: workflow)")

    args = parser.parse_args()

    if args.mode == "workflow":
        test_basic_workflow()
    elif args.mode == "junctions":
        test_list_junctions()
    elif args.mode == "edge":
        test_analyze_specific_edge()
