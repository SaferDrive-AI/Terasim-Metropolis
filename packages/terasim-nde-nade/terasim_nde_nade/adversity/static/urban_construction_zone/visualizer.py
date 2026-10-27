#!/usr/bin/env python3
"""
Construction Zone Visualizer

Extract construction cone positions from FCD data and visualize on road network.

Supports multiple visualization modes:
- Single image (for short zones)
- Segmented images (for long zones, e.g., 500m per segment)
- Interactive HTML map (zoomable, using folium)

Usage:
    python visualizer.py --experiment ann_arbor_whole_30961 --scene ann_arbor_whole
    python visualizer.py --experiment detroit_i94 --scene detroit_i94 --mode segments --segment-length 500
    python visualizer.py --experiment detroit_i94 --scene detroit_i94 --mode interactive
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
import math

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# Add visualization tools path
terasim_root = Path(__file__).parent.parent.parent.parent.parent.parent
sys.path.insert(0, str(terasim_root / "packages" / "terasim-vis"))


def find_construction_cones(
    fcd_file: str,
    target_time: float = 5.0
) -> Tuple[Optional[float], List[Dict[str, Any]]]:
    """
    Find construction cones from FCD file

    Args:
        fcd_file: FCD XML file path
        target_time: Target time in seconds (default 5.0s for cone stabilization)

    Returns:
        (timestamp, list of cone data)
    """
    print(f"Searching for cones: {fcd_file}")
    print(f"Target time: {target_time:.2f}s")

    first_cone_time = None
    target_cones = None

    for event, elem in ET.iterparse(fcd_file, events=('end',)):
        if elem.tag == 'timestep':
            time = float(elem.attrib["time"])
            cones = []

            for veh in elem:
                if veh.tag == "vehicle":
                    veh_type = veh.attrib.get("type", "")
                    if veh_type == "CONSTRUCTION_CONE_MIN":
                        cones.append({
                            'id': veh.attrib["id"],
                            'x': float(veh.attrib["x"]),
                            'y': float(veh.attrib["y"]),
                            'angle': float(veh.attrib["angle"]),
                            'lane': veh.attrib.get("lane", ""),
                            'pos': float(veh.attrib.get("pos", 0))
                        })

            if cones and first_cone_time is None:
                first_cone_time = time
                print(f"First found {len(cones)} cones (t={time:.2f}s)")

            if cones and abs(time - target_time) < 0.01:
                target_cones = cones
                print(f"Found {len(cones)} cones at target time")
                elem.clear()
                break

            elem.clear()

            if time > target_time + 1.0:
                break

    if target_cones:
        return target_time, target_cones
    elif first_cone_time is not None:
        print(f"Using first occurrence at {first_cone_time:.2f}s")
        for event, elem in ET.iterparse(fcd_file, events=('end',)):
            if elem.tag == 'timestep':
                time = float(elem.attrib["time"])
                if abs(time - first_cone_time) < 0.01:
                    cones = []
                    for veh in elem:
                        if veh.tag == "vehicle" and veh.attrib.get("type") == "CONSTRUCTION_CONE_MIN":
                            cones.append({
                                'id': veh.attrib["id"],
                                'x': float(veh.attrib["x"]),
                                'y': float(veh.attrib["y"]),
                                'angle': float(veh.attrib["angle"]),
                                'lane': veh.attrib.get("lane", ""),
                                'pos': float(veh.attrib.get("pos", 0))
                            })
                    elem.clear()
                    return first_cone_time, cones
                elem.clear()

    print("No construction cones found")
    return None, []


def calculate_zone_length(cones: List[Dict]) -> float:
    """Calculate the total length of construction zone from cone positions"""
    if not cones:
        return 0.0
    xs = [c['x'] for c in cones]
    ys = [c['y'] for c in cones]
    return math.sqrt((max(xs) - min(xs))**2 + (max(ys) - min(ys))**2)


def segment_cones_by_edge(cones: List[Dict]) -> List[List[Dict]]:
    """
    Split cones by their edge (lane) - most natural for road networks.

    Args:
        cones: List of cone data with 'lane' field

    Returns:
        List of cone lists, one per edge
    """
    if not cones:
        return []

    from collections import defaultdict

    # Group by edge
    by_edge = defaultdict(list)
    for cone in cones:
        # Extract edge ID from lane (e.g., "123456_0" -> "123456")
        lane = cone.get('lane', '')
        edge = lane.rsplit('_', 1)[0] if lane else 'unknown'
        by_edge[edge].append(cone)

    # Sort edges by average x+y position (approximate road order)
    edge_order = []
    for edge, edge_cones in by_edge.items():
        avg_pos = sum(c['x'] + c['y'] for c in edge_cones) / len(edge_cones)
        edge_order.append((edge, avg_pos, edge_cones))

    edge_order.sort(key=lambda x: x[1])

    return [cones for _, _, cones in edge_order]


def segment_cones_by_distance(
    cones: List[Dict],
    segment_length: float = 500.0
) -> List[List[Dict]]:
    """
    Split cones into segments based on distance along the construction zone.

    First groups by edge, then merges small edges into larger segments.

    Args:
        cones: List of cone data
        segment_length: Target length per segment in meters

    Returns:
        List of cone lists, one per segment
    """
    if not cones:
        return []

    # First, group by edge for natural road-based segmentation
    edge_segments = segment_cones_by_edge(cones)

    if len(edge_segments) <= 1:
        return edge_segments

    # Merge small edge segments into larger ones based on target length
    merged_segments = []
    current_segment = []
    current_length = 0.0

    for edge_cones in edge_segments:
        # Calculate edge length
        xs = [c['x'] for c in edge_cones]
        ys = [c['y'] for c in edge_cones]
        edge_length = math.sqrt((max(xs) - min(xs))**2 + (max(ys) - min(ys))**2)

        # If adding this edge would exceed target, start new segment
        if current_segment and current_length + edge_length > segment_length * 1.5:
            merged_segments.append(current_segment)
            current_segment = edge_cones.copy()
            current_length = edge_length
        else:
            current_segment.extend(edge_cones)
            current_length += edge_length

    # Don't forget the last segment
    if current_segment:
        merged_segments.append(current_segment)

    return merged_segments


def load_network_safe(net_file: str):
    """Safely load network file with error handling"""
    try:
        from terasim_vis import Net
        return Net(net_file)
    except Exception as e:
        print(f"Full load failed: {e}")
        print("Attempting simplified load...")

        import xml.etree.ElementTree as ET2
        from terasim_vis.Net import Net as NetClass

        net = object.__new__(NetClass)
        net.additionals = []
        net.edges = dict()
        net.junctions = dict()
        net.tlLogics = dict()
        net.connections = []
        net.netOffset = (0, 0)
        net.projParameter = "!"

        net_tree = ET2.parse(net_file).getroot()

        from terasim_vis.Net import _Edge, _Lane, _Junction

        for obj in net_tree:
            if obj.tag == "location":
                if "netOffset" in obj.attrib:
                    net.netOffset = tuple(float(i) for i in obj.attrib["netOffset"].split(","))
            if obj.tag == "edge":
                if obj.attrib.get("function") == "walkingarea":
                    continue
                edge = _Edge(obj.attrib)
                for edgeChild in obj:
                    if edgeChild.tag == "lane":
                        try:
                            lane = _Lane(edgeChild.attrib)
                            edge.append_lane(lane)
                        except Exception:
                            pass
                net.edges[edge.id] = edge
            elif obj.tag == "junction":
                junction = _Junction(obj.attrib)
                net.junctions[junction.id] = junction

        for edge in net.edges.values():
            edge.from_junction = net.junctions.get(edge.from_junction_id, None)
            edge.to_junction = net.junctions.get(edge.to_junction_id, None)

        print("Simplified load successful")
        return net


def visualize_construction_zone(
    net_file: str,
    fcd_file: str,
    output_file: Optional[str] = None,
    target_time: float = 5.0
):
    """
    Visualize construction zone

    Args:
        net_file: SUMO network file path
        fcd_file: FCD XML file path
        output_file: Output image path (optional)
        target_time: Target time point
    """
    # Find construction cones
    timestep, cones = find_construction_cones(fcd_file, target_time)

    if timestep is None or not cones:
        print("Cannot visualize: no construction cones found")
        return

    # Load network
    print(f"\nLoading network: {net_file}")
    net = load_network_safe(net_file)

    # Create figure
    fig, ax = plt.subplots(figsize=(16, 12))

    # Draw road network
    print("Rendering road network...")
    net.plot(
        ax,
        style='EUR',
        zoom_to_extents=False,
        plot_stop_lines=False,
        lane_kwargs=dict(color='#333333'),
        junction_kwargs=dict(color='#333333'),
        lane_marking_kwargs=dict(color='white')
    )

    # Calculate view bounds
    cone_xs = [cone['x'] for cone in cones]
    cone_ys = [cone['y'] for cone in cones]

    center_x = np.mean(cone_xs)
    center_y = np.mean(cone_ys)

    margin = 50
    x_range = max(cone_xs) - min(cone_xs)
    y_range = max(cone_ys) - min(cone_ys)

    view_width = max(x_range + 2 * margin, 100)
    view_height = max(y_range + 2 * margin, 100)

    ax.set_xlim(center_x - view_width/2, center_x + view_width/2)
    ax.set_ylim(center_y - view_height/2, center_y + view_height/2)
    ax.set_aspect('equal')

    # Draw construction cones
    print(f"Drawing {len(cones)} cones...")

    zone_colors = {
        'taper_in': ('orange', 'Taper In'),
        'taper_out': ('green', 'Taper Out'),
        'work': ('red', 'Work Zone'),
        'warning': ('yellow', 'Warning'),
    }

    labels_used = set()

    for cone in cones:
        cone_id = cone['id']

        # Determine zone type
        zone_type = 'Other'
        color = 'yellow'
        for key, (c, label) in zone_colors.items():
            if key in cone_id:
                color = c
                zone_type = label
                break

        # Avoid duplicate legend entries
        label = zone_type if zone_type not in labels_used else None
        if label:
            labels_used.add(zone_type)

        # Draw cone
        circle = mpatches.Circle(
            (cone['x'], cone['y']),
            0.6,
            facecolor=color,
            edgecolor='black',
            linewidth=1.5,
            alpha=0.95,
            zorder=1000,
            label=label
        )
        ax.add_patch(circle)

    # Title and legend
    ax.set_title(
        f'Construction Zone Visualization\nTime: {timestep:.2f}s | Cones: {len(cones)}',
        fontsize=16, fontweight='bold', pad=20
    )

    handles, labels = ax.get_legend_handles_labels()
    by_label = dict(zip(labels, handles))
    ax.legend(by_label.values(), by_label.keys(),
              loc='upper right', fontsize=12, framealpha=0.9)

    ax.set_xlabel('X coordinate (m)', fontsize=12)
    ax.set_ylabel('Y coordinate (m)', fontsize=12)
    ax.margins(0)
    plt.tight_layout()

    # Save or show
    if output_file:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"\nSaving image: {output_file}")
        plt.savefig(output_file, dpi=300, bbox_inches='tight')
        print("Save successful!")
    else:
        print("\nDisplaying figure...")
        plt.show()

    # Print details
    print(f"\n{'='*60}")
    print(f"Cone Details (t={timestep:.2f}s)")
    print(f"{'='*60}")
    print(f"{'ID':<35} {'X':>8} {'Y':>8} {'Lane':>12}")
    print(f"{'-'*60}")
    for cone in cones:
        print(f"{cone['id']:<35} {cone['x']:>8.2f} {cone['y']:>8.2f} {cone['lane']:>12}")
    print(f"{'='*60}\n")


def visualize_segments(
    net_file: str,
    fcd_file: str,
    output_dir: str,
    segment_length: float = 500.0,
    target_time: float = 5.0
) -> List[Path]:
    """
    Visualize construction zone in segments for long zones.

    Args:
        net_file: SUMO network file path
        fcd_file: FCD XML file path
        output_dir: Output directory for segment images
        segment_length: Length of each segment in meters
        target_time: Target time point

    Returns:
        List of generated image paths
    """
    # Find construction cones
    timestep, cones = find_construction_cones(fcd_file, target_time)

    if timestep is None or not cones:
        print("Cannot visualize: no construction cones found")
        return []

    # Calculate zone length
    zone_length = calculate_zone_length(cones)
    print(f"\nTotal zone length: {zone_length:.1f}m")

    # Segment cones
    segments = segment_cones_by_distance(cones, segment_length)
    print(f"Split into {len(segments)} segments (~{segment_length}m each)")

    # Load network
    print(f"\nLoading network: {net_file}")
    net = load_network_safe(net_file)

    # Create output directory
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    generated_files = []

    # Generate overview image first
    overview_file = output_path / "construction_zone_overview.png"
    print(f"\nGenerating overview image...")

    fig, ax = plt.subplots(figsize=(20, 4))  # Wide aspect ratio for overview

    # Draw road network
    net.plot(
        ax,
        style='EUR',
        zoom_to_extents=False,
        plot_stop_lines=False,
        lane_kwargs=dict(color='#333333'),
        junction_kwargs=dict(color='#333333'),
        lane_marking_kwargs=dict(color='white')
    )

    # Draw all cones (smaller for overview)
    for cone in cones:
        zone_type = 'work'
        color = 'red'
        for key, c in [('taper_in', 'orange'), ('taper_out', 'green'), ('warning', 'yellow')]:
            if key in cone['id']:
                color = c
                break
        circle = mpatches.Circle(
            (cone['x'], cone['y']),
            1.5,  # Larger radius for visibility in overview
            facecolor=color,
            edgecolor='black',
            linewidth=0.5,
            alpha=0.8,
            zorder=1000
        )
        ax.add_patch(circle)

    # Set bounds for overview
    cone_xs = [c['x'] for c in cones]
    cone_ys = [c['y'] for c in cones]
    margin = 100
    ax.set_xlim(min(cone_xs) - margin, max(cone_xs) + margin)
    ax.set_ylim(min(cone_ys) - margin, max(cone_ys) + margin)
    ax.set_aspect('equal')
    ax.set_title(f'Construction Zone Overview\nTotal Length: {zone_length:.0f}m | {len(cones)} Cones | {len(segments)} Segments',
                 fontsize=14, fontweight='bold')
    ax.set_xlabel('X (m)')
    ax.set_ylabel('Y (m)')
    plt.tight_layout()
    plt.savefig(overview_file, dpi=200, bbox_inches='tight')
    plt.close()
    generated_files.append(overview_file)
    print(f"Saved: {overview_file}")

    # Generate segment images
    for i, segment_cones in enumerate(segments):
        segment_file = output_path / f"construction_zone_segment_{i+1:02d}.png"
        print(f"Generating segment {i+1}/{len(segments)}...")

        fig, ax = plt.subplots(figsize=(16, 12))

        # Draw road network
        net.plot(
            ax,
            style='EUR',
            zoom_to_extents=False,
            plot_stop_lines=False,
            lane_kwargs=dict(color='#333333'),
            junction_kwargs=dict(color='#333333'),
            lane_marking_kwargs=dict(color='white')
        )

        zone_colors = {
            'taper_in': ('orange', 'Taper In'),
            'taper_out': ('green', 'Taper Out'),
            'work': ('red', 'Work Zone'),
            'warning': ('yellow', 'Warning'),
        }
        labels_used = set()

        # Draw cones in this segment
        for cone in segment_cones:
            zone_type = 'Work Zone'
            color = 'red'
            for key, (c, label) in zone_colors.items():
                if key in cone['id']:
                    color = c
                    zone_type = label
                    break

            label = zone_type if zone_type not in labels_used else None
            if label:
                labels_used.add(zone_type)

            circle = mpatches.Circle(
                (cone['x'], cone['y']),
                0.6,
                facecolor=color,
                edgecolor='black',
                linewidth=1.5,
                alpha=0.95,
                zorder=1000,
                label=label
            )
            ax.add_patch(circle)

        # Set bounds for segment
        seg_xs = [c['x'] for c in segment_cones]
        seg_ys = [c['y'] for c in segment_cones]
        margin = 30
        x_range = max(seg_xs) - min(seg_xs)
        y_range = max(seg_ys) - min(seg_ys)
        view_width = max(x_range + 2 * margin, 100)
        view_height = max(y_range + 2 * margin, 100)
        center_x = np.mean(seg_xs)
        center_y = np.mean(seg_ys)

        ax.set_xlim(center_x - view_width/2, center_x + view_width/2)
        ax.set_ylim(center_y - view_height/2, center_y + view_height/2)
        ax.set_aspect('equal')

        seg_length = math.sqrt(
            (max(seg_xs) - min(seg_xs))**2 +
            (max(seg_ys) - min(seg_ys))**2
        )
        ax.set_title(
            f'Construction Zone - Segment {i+1}/{len(segments)}\n'
            f'Length: {seg_length:.0f}m | Cones: {len(segment_cones)}',
            fontsize=14, fontweight='bold'
        )

        handles, labels = ax.get_legend_handles_labels()
        by_label = dict(zip(labels, handles))
        if by_label:
            ax.legend(by_label.values(), by_label.keys(),
                      loc='upper right', fontsize=10, framealpha=0.9)

        ax.set_xlabel('X (m)')
        ax.set_ylabel('Y (m)')
        plt.tight_layout()
        plt.savefig(segment_file, dpi=300, bbox_inches='tight')
        plt.close()
        generated_files.append(segment_file)

    print(f"\nGenerated {len(generated_files)} visualization files in {output_dir}")
    return generated_files


def visualize_interactive(
    net_file: str,
    fcd_file: str,
    output_file: str,
    target_time: float = 5.0
) -> Optional[Path]:
    """
    Generate an interactive HTML map using folium.

    Args:
        net_file: SUMO network file path
        fcd_file: FCD XML file path
        output_file: Output HTML file path
        target_time: Target time point

    Returns:
        Path to generated HTML file
    """
    try:
        import folium
    except ImportError:
        print("folium not installed. Install with: pip install folium")
        print("Falling back to static visualization...")
        return None

    # Find construction cones
    timestep, cones = find_construction_cones(fcd_file, target_time)

    if timestep is None or not cones:
        print("Cannot visualize: no construction cones found")
        return None

    # Load network to get projection info
    net = load_network_safe(net_file)

    # Get projection from network
    try:
        import sumolib
        net_sumo = sumolib.net.readNet(net_file)
        # Convert SUMO XY back to lat/lon
        cone_locations = []
        for cone in cones:
            lon, lat = net_sumo.convertXY2LonLat(cone['x'], cone['y'])
            cone_locations.append({
                'lat': lat,
                'lon': lon,
                'id': cone['id'],
                'x': cone['x'],
                'y': cone['y'],
                'lane': cone.get('lane', '')
            })
    except Exception as e:
        print(f"Could not convert coordinates: {e}")
        print("Using raw XY coordinates (may not display correctly on map)")
        # Fallback: assume coordinates are already lat/lon
        cone_locations = [
            {'lat': c['y'], 'lon': c['x'], 'id': c['id'], 'lane': c.get('lane', '')}
            for c in cones
        ]

    # Calculate bounds
    lats = [c['lat'] for c in cone_locations]
    lons = [c['lon'] for c in cone_locations]
    center_lat = np.mean(lats)
    center_lon = np.mean(lons)

    # Calculate appropriate zoom level based on extent
    lat_range = max(lats) - min(lats)
    lon_range = max(lons) - min(lons)
    max_range = max(lat_range, lon_range)

    # Approximate zoom level (higher = more zoomed in)
    if max_range > 0.1:
        zoom = 11
    elif max_range > 0.05:
        zoom = 12
    elif max_range > 0.02:
        zoom = 13
    elif max_range > 0.01:
        zoom = 14
    else:
        zoom = 15

    # Create map with satellite imagery as default
    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=zoom,
        tiles=None  # We'll add tiles manually
    )

    # Add multiple tile layers for user choice
    # Google Satellite (supports high zoom levels)
    folium.TileLayer(
        tiles='https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',
        attr='Google',
        name='Satellite',
        max_zoom=21,
        overlay=False,
        control=True
    ).add_to(m)

    # Google Hybrid (satellite + labels, high zoom)
    folium.TileLayer(
        tiles='https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}',
        attr='Google',
        name='Satellite + Labels',
        max_zoom=21,
        overlay=False,
        control=True
    ).add_to(m)

    # OpenStreetMap (colorful street map)
    folium.TileLayer('openstreetmap', name='Street Map', max_zoom=19).add_to(m)

    # CartoDB Positron (light/minimal)
    folium.TileLayer('cartodbpositron', name='Light Mode', max_zoom=20).add_to(m)

    # CartoDB Dark Matter (dark mode)
    folium.TileLayer('cartodbdark_matter', name='Dark Mode', max_zoom=20).add_to(m)

    # Fit bounds to show all markers
    m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])

    # Color mapping
    zone_colors = {
        'taper_in': '#FFA500',  # orange
        'taper_out': '#00FF00',  # green
        'work': '#FF0000',  # red
        'warning': '#FFFF00',  # yellow
    }

    # Group cones by edge for better visualization
    from collections import defaultdict
    by_edge = defaultdict(list)
    for cone in cone_locations:
        lane = cone.get('lane', '')
        edge = lane.rsplit('_', 1)[0] if lane else 'unknown'
        by_edge[edge].append(cone)

    # Create a FeatureGroup for each edge
    for edge_id, edge_cones in by_edge.items():
        # Sort cones within edge by position
        edge_cones_sorted = sorted(edge_cones, key=lambda c: c.get('x', 0) + c.get('y', 0))

        # Draw polyline for this edge (connecting cones in order)
        if len(edge_cones_sorted) >= 2:
            line_coords = [[c['lat'], c['lon']] for c in edge_cones_sorted]
            folium.PolyLine(
                line_coords,
                color='#0066FF',
                weight=4,
                opacity=0.6,
                tooltip=f'Edge: {edge_id}'
            ).add_to(m)

        # Add individual cone markers (smaller, no clustering)
        for cone in edge_cones:
            # Determine zone type and color
            color = zone_colors['work']
            zone_type = 'work'
            for key, c in zone_colors.items():
                if key in cone['id']:
                    color = c
                    zone_type = key
                    break

            # Create popup content
            popup_html = f"""
            <b>Cone ID:</b> {cone['id']}<br>
            <b>Type:</b> {zone_type}<br>
            <b>Edge:</b> {edge_id}<br>
            <b>Lane:</b> {cone.get('lane', 'N/A')}<br>
            <b>Position:</b> ({cone.get('x', 0):.1f}, {cone.get('y', 0):.1f})
            """

            folium.CircleMarker(
                location=[cone['lat'], cone['lon']],
                radius=3,  # Smaller radius
                color=color,
                fill=True,
                fillColor=color,
                fillOpacity=0.9,
                weight=1,
                popup=folium.Popup(popup_html, max_width=300),
                tooltip=f"{zone_type}: {cone['id']}"
            ).add_to(m)

    # Add legend
    zone_length = calculate_zone_length(cones)
    legend_html = f'''
    <div style="position: fixed; bottom: 50px; left: 50px; z-index: 1000;
                background-color: white; padding: 10px; border-radius: 5px;
                border: 2px solid grey; font-size: 12px;">
        <b>Construction Zone</b><br>
        <span style="color: orange;">&#9679;</span> Taper In<br>
        <span style="color: red;">&#9679;</span> Work Zone<br>
        <span style="color: green;">&#9679;</span> Taper Out<br>
        <span style="color: yellow;">&#9679;</span> Warning<br>
        <hr>
        <b>Total Cones:</b> {len(cones)}<br>
        <b>Zone Length:</b> {zone_length:.0f}m
    </div>
    '''
    m.get_root().html.add_child(folium.Element(legend_html))

    # Add layer control
    folium.LayerControl().add_to(m)

    # Save
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(output_path))

    print(f"\nInteractive map saved: {output_path}")
    print("Open in a web browser to view and zoom")

    return output_path


def visualize_construction_zone_auto(
    net_file: str,
    fcd_file: str,
    output_file: str,
    target_time: float = 5.0,
    segment_threshold: float = 1000.0,
    segment_length: float = 500.0
) -> List[Path]:
    """
    Automatically choose visualization mode based on zone length.

    - Zone < segment_threshold: Single static image
    - Zone >= segment_threshold: Segmented images + interactive HTML

    Args:
        net_file: SUMO network file path
        fcd_file: FCD XML file path
        output_file: Base output file path
        target_time: Target time point
        segment_threshold: Zone length threshold for switching to segments (default 1000m)
        segment_length: Length of each segment in meters (default 500m)

    Returns:
        List of generated file paths
    """
    # Find cones first to determine zone length
    timestep, cones = find_construction_cones(fcd_file, target_time)

    if timestep is None or not cones:
        print("Cannot visualize: no construction cones found")
        return []

    zone_length = calculate_zone_length(cones)
    print(f"\nConstruction zone length: {zone_length:.1f}m")

    output_path = Path(output_file)
    generated_files = []

    if zone_length < segment_threshold:
        # Short zone: single image + interactive HTML
        print(f"Zone is short (<{segment_threshold}m), using single image mode")
        visualize_construction_zone(net_file, fcd_file, str(output_path), target_time)
        if output_path.exists():
            generated_files.append(output_path)

        # Also generate interactive HTML for short zones
        html_file = output_path.parent / "construction_zone.html"
        html_path = visualize_interactive(net_file, fcd_file, str(html_file), target_time)
        if html_path:
            generated_files.append(html_path)
    else:
        # Long zone: segmented images + interactive
        print(f"Zone is long (>={segment_threshold}m), using segmented + interactive mode")

        # Generate segmented images
        segment_dir = output_path.parent
        seg_files = visualize_segments(
            net_file, fcd_file, str(segment_dir),
            segment_length=segment_length, target_time=target_time
        )
        generated_files.extend(seg_files)

        # Generate interactive HTML
        html_file = output_path.parent / "construction_zone_interactive.html"
        html_path = visualize_interactive(net_file, fcd_file, str(html_file), target_time)
        if html_path:
            generated_files.append(html_path)

    return generated_files


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Construction Zone Visualizer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single image (default)
  python visualizer.py --experiment ann_arbor_whole_30961 --scene ann_arbor_whole

  # Segmented images for long zones
  python visualizer.py --experiment detroit_i94 --scene detroit_i94 --mode segments

  # Interactive HTML map
  python visualizer.py --experiment detroit_i94 --scene detroit_i94 --mode interactive

  # Auto mode (chooses based on zone length)
  python visualizer.py --experiment detroit_i94 --scene detroit_i94 --mode auto
        """
    )

    parser.add_argument("--experiment", type=str, default=None,
                        help="Experiment name (e.g., ann_arbor_whole_30961)")
    parser.add_argument("--scene", type=str, default=None,
                        help="Scene name (e.g., ann_arbor_whole)")
    parser.add_argument("--net", type=str, default=None,
                        help="Network file path")
    parser.add_argument("--fcd", type=str, default=None,
                        help="FCD file path")
    parser.add_argument("--output", type=str, default=None,
                        help="Output image/directory path")
    parser.add_argument("--time", type=float, default=5.0,
                        help="Target time (default: 5.0s)")
    parser.add_argument("--mode", type=str, default="auto",
                        choices=["single", "segments", "interactive", "auto"],
                        help="Visualization mode (default: auto)")
    parser.add_argument("--segment-length", type=float, default=500.0,
                        help="Segment length in meters (default: 500m)")
    parser.add_argument("--threshold", type=float, default=1000.0,
                        help="Zone length threshold for auto mode (default: 1000m)")

    args = parser.parse_args()

    executor_dir = Path(__file__).parent

    if args.net and args.fcd:
        net_file = args.net
        fcd_file = args.fcd
        output_file = args.output
    else:
        experiment = args.experiment or "ann_arbor_whole_30961"
        scene = args.scene or "ann_arbor_whole"

        net_file = executor_dir / f"scenes/{scene}/map.net.xml"
        fcd_file = executor_dir / f"outputs/{experiment}/fcd_all.xml"
        output_file = executor_dir / f"outputs/{experiment}/construction_zone.png"

        if not net_file.exists():
            print(f"Network file not found: {net_file}")
            sys.exit(1)

        if not fcd_file.exists():
            print(f"FCD file not found: {fcd_file}")
            sys.exit(1)

    print("="*60)
    print("Construction Zone Visualization")
    print("="*60)
    print(f"Network: {net_file}")
    print(f"FCD: {fcd_file}")
    print(f"Output: {output_file}")
    print(f"Mode: {args.mode}")
    print("="*60)

    try:
        if args.mode == "single":
            visualize_construction_zone(
                net_file=str(net_file),
                fcd_file=str(fcd_file),
                output_file=str(output_file) if output_file else None,
                target_time=args.time
            )
        elif args.mode == "segments":
            output_dir = Path(output_file).parent if output_file else executor_dir / "outputs" / experiment
            visualize_segments(
                net_file=str(net_file),
                fcd_file=str(fcd_file),
                output_dir=str(output_dir),
                segment_length=args.segment_length,
                target_time=args.time
            )
        elif args.mode == "interactive":
            html_file = Path(output_file).with_suffix('.html') if output_file else \
                        executor_dir / f"outputs/{experiment}/construction_zone_interactive.html"
            visualize_interactive(
                net_file=str(net_file),
                fcd_file=str(fcd_file),
                output_file=str(html_file),
                target_time=args.time
            )
        else:  # auto
            visualize_construction_zone_auto(
                net_file=str(net_file),
                fcd_file=str(fcd_file),
                output_file=str(output_file) if output_file else str(executor_dir / f"outputs/{experiment}/construction_zone.png"),
                target_time=args.time,
                segment_threshold=args.threshold,
                segment_length=args.segment_length
            )

        print("\nVisualization complete!")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
