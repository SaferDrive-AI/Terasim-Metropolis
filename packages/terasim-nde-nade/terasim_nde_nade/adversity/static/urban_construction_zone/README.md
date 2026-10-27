# Urban Construction Zone with Automatic Connection Management

Generate and simulate realistic construction zone scenarios for autonomous vehicle testing with intelligent traffic flow rerouting.

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [Generation Modes](#generation-modes)
- [Connection Management System](#connection-management-system)
- [Advanced Features](#advanced-features)
- [API Reference](#api-reference)
- [System Architecture](#system-architecture)
- [Testing & Validation](#testing--validation)
- [Troubleshooting](#troubleshooting)
- [Technical Reference](#technical-reference)

---

## Overview

### What is This Module?

This module provides a complete solution for generating and simulating construction zone scenarios in SUMO-based traffic simulations. It combines:

1. **Physical Construction Elements**: Traffic cones, barriers, warning signs
2. **Intelligent Traffic Management**: Automatic connection rerouting when lanes are blocked
3. **Multiple Generation Methods**: Random placement, coordinate-based, or real-world data from TomTom API
4. **Full Automation**: One command generates everything needed

### Key Features

- **v2.0 Full Automation**: Single command creates enhanced network, deploys construction elements, and configures intelligent rerouting
- **Realistic Construction Zones**: 4-segment layout (Warning → Taper-In → Work Zone → Taper-Out)
- **Smart Connection Management**: Automatically enables adjacent lanes to handle turns when construction blocks dedicated turn lanes
- **Lane Compatibility Filtering**: Intelligently avoids using BRT/bus-only lanes for emergency connections
- **Multi-Edge Support**: Handle construction zones spanning multiple road segments
- **Visualization Tools**: Generate PNG images and interactive HTML maps
- **Real-World Data**: Fetch actual construction zones via TomTom Traffic API

### When to Use

- **AV Safety Testing**: Test autonomous vehicle responses to construction zones
- **Traffic Flow Analysis**: Study impact of lane closures on traffic patterns
- **Emergency Response**: Simulate how traffic adapts when lanes are blocked
- **Urban Planning**: Evaluate construction impact before implementation

---

## Quick Start

### Installation

```bash
cd packages/terasim-nde-nade/terasim_nde_nade/adversity/static/urban_construction_zone
conda activate terasim
```

### Basic Usage

Generate a construction zone from coordinates (fully automatic):

```bash
python construction_zone_generator.py coords \
    --scene scenes/ann_arbor_whole \
    --start 42.280,-83.738 \
    --end 42.281,-83.736 \
    --lanes 0
```

This single command:
1. ✅ Analyzes which intersections are affected
2. ✅ Generates enhanced network with emergency turn connections
3. ✅ Creates configuration file with connection management enabled
4. ✅ Ready to run simulation with intelligent rerouting

Run the simulation:

```bash
python run_experiments.py --config outputs/config_manual.yaml
```

### What Happens

**Physical Layer (Visible)**:
- Warning signs placed 50m before construction
- Traffic cones create taper zones guiding vehicles to open lanes
- Barriers mark the work zone

**Logical Layer (Invisible)**:
- Blocked lane's turn connections disabled
- Adjacent lanes enabled to perform those turns
- Vehicles automatically reroute through emergency connections

---

## Generation Modes

### Mode 1: Random Generation

Randomly select roads in an existing scene for construction.

```bash
# Using existing scene
python construction_zone_generator.py random \
    --scene scenes/ann_arbor_whole \
    --num 5

# Auto-create new scene from center coordinates
python construction_zone_generator.py random \
    --center 42.28,-83.74 \
    --size 500 \
    --num 3

# Generate and run immediately
python construction_zone_generator.py random \
    --scene scenes/ann_arbor_whole \
    --num 1 \
    --run
```

**Parameters**:
- `--scene PATH`: Use existing scene directory (optional if --center provided)
- `--center LAT,LON`: Auto-create scene at coordinates
- `--size METERS`: Map range (default 500m)
- `--num N`: Number of configurations to generate (default 5)
- `--min-length M`: Minimum zone length (default 30m)
- `--max-length M`: Maximum zone length (default 100m)
- `--seed N`: Random seed for reproducibility
- `--run`: Execute simulations after generation

### Mode 2: Coordinate-Based Generation

Generate construction zones from precise start/end coordinates.

```bash
# Using existing scene
python construction_zone_generator.py coords \
    --scene scenes/ann_arbor_whole \
    --start 42.280,-83.738 \
    --end 42.281,-83.736 \
    --lanes 0,1

# Auto-create scene (no --scene needed)
python construction_zone_generator.py coords \
    --start 42.29799971530919,-83.72030298642086 \
    --end 42.29818025072757,-83.71945138489987

# Custom zone ID and immediate simulation
python construction_zone_generator.py coords \
    --start 42.298,-83.720 \
    --end 42.299,-83.718 \
    --id my_zone \
    --run
```

**Auto Scene Creation**:
- When `--scene` is omitted, center point calculated from start/end coordinates
- Scene size auto-calculated as 1.5× coordinate distance (minimum 500m)
- Use `--size` to manually specify scene size

**Coordinate Matching Algorithm**:
1. Convert lat/lon to SUMO XY coordinates using `convertLonLat2XY()`
2. Find nearest road edge within 100m radius using `getNeighboringEdges()`
3. Project point onto lane centerline, calculate precise meter position
4. Use BFS to find path if start and end are on different edges

### Mode 3: TomTom API

Fetch real construction zone data from TomTom Traffic API.

```bash
# Using existing scene
python construction_zone_generator.py tomtom \
    --api-key YOUR_TOMTOM_API_KEY \
    --scene scenes/ann_arbor_whole

# Auto-create scene from center coordinates
python construction_zone_generator.py tomtom \
    --api-key YOUR_TOMTOM_API_KEY \
    --center 42.28,-83.74 \
    --size 1000

# Use API key from environment variable
export TOMTOM_API_KEY=your_key_here
python construction_zone_generator.py tomtom \
    --scene scenes/ann_arbor_whole

# Test with sample data (no API key needed)
python tomtom_pipeline.py --sample --config-only
```

**Getting API Key**:
1. Visit [TomTom Developer Portal](https://developer.tomtom.com/)
2. Register account and create application
3. Copy API Key

**Typical Construction Zone Counts by City**:
- Los Angeles: ~35 roadworks
- Denver: ~27 roadworks
- New York: ~20 roadworks
- Chicago: ~15 roadworks
- Ann Arbor: Usually fewer

---

## Connection Management System

### The Problem

When construction blocks a turn lane (e.g., right-turn lane), real-world traffic management allows adjacent lanes (e.g., through lane) to also perform that turn temporarily. In standard SUMO simulations, vehicles would be stuck.

**Example Scenario**:

```
Normal Intersection:
┌─────────────────────┐
│   Lane 1 (Through)   │ ────→  Straight
│   Lane 0 (Right)     │ ────→  Right Turn ✓
└─────────────────────┘

Construction Blocks Lane 0:
┌─────────────────────┐
│   Lane 1 (Through)   │ ────→  Straight only
│   Lane 0 🚧🚧🚧       │ ────X  Blocked!
└─────────────────────┘
Problem: No way to turn right!

With Connection Management:
┌─────────────────────┐
│   Lane 1 (Through)   │ ────→  Straight OR Right Turn ✓
│   Lane 0 🚧🚧🚧       │ ────X  Blocked
└─────────────────────┘
Solution: Lane 1 gains temporary right-turn ability
```

### Two-Phase Architecture

**Phase 1: Offline Network Enhancement** (Automatic)

When you run `construction_zone_generator.py`, the system:

1. Analyzes which intersections are affected by construction
2. Identifies which turn connections will be blocked
3. Finds suitable adjacent lanes to serve as emergency alternatives
4. Filters out incompatible lanes (BRT, emergency-only, etc.)
5. Generates `map_enhanced.net.xml` with emergency connections pre-defined but initially disabled
6. Embeds connection management instructions in the configuration file

**Phase 2: Runtime Activation** (Automatic)

When simulation starts with the enhanced network:

1. `UrbanConstructionZone` reads connection management config
2. Deploys physical construction elements (cones, barriers, signs)
3. Uses TraCI to disable blocked lane connections: `traci.lane.setDisallowed(":junction_0_0", ["all"])`
4. Uses TraCI to enable emergency connections: `traci.lane.setAllowed(":junction_5_0", ["passenger"])`
5. Vehicles automatically reroute using the new turn options

### Key Innovation: Why Two Phases?

**SUMO Limitation**: Cannot dynamically create/delete turn connections at runtime (they're part of the static network topology).

**Solution**: Pre-generate all possible emergency connections in the enhanced network, but keep them disabled. At runtime, simply toggle permissions.

### Automatic Integration (v2.0)

**No manual steps required!** The system is fully integrated:

```bash
# This single command does everything:
python construction_zone_generator.py coords \
    --scene scenes/ann_arbor_whole \
    --start 42.280,-83.738 \
    --end 42.281,-83.736 \
    --lanes 0

# Output:
# ✓ Generated enhanced network with 3 junction modifications
#   Enhanced network saved to: scenes/ann_arbor_whole/map_enhanced.net.xml
# Config saved to: outputs/config_manual.yaml
```

The generated config automatically includes:

```yaml
input:
  sumo_net_file: scenes/ann_arbor_whole/map_enhanced.net.xml  # Enhanced network

environment:
  parameters:
    adversity_cfg:
      static:
        simple_urban_construction_zone:
          edges: [...]
          lanes_to_close: [0]
          connection_management:  # Automatic!
            enabled: true
            modifications:
              - junction_id: junction_456
                blocked_connections: [...]
                emergency_connections: [...]
```

### Lane Compatibility Filtering (v1.1)

The system automatically filters out incompatible lanes when selecting emergency alternatives:

**Filtered Lane Types**:
- BRT/bus-only lanes (`allow=["bus"]` or `disallow=["passenger"]`)
- Emergency vehicle lanes (`allow=["emergency"]`)
- HOV lanes, truck-only lanes, etc.

**Example**:

```
Lane 2: BRT lane (allow=["bus"])                 ✗ Skipped
Lane 1: Normal lane (allow=["passenger", "bus"]) ✓ Selected
Lane 0: Right-turn lane 🚧 Blocked
```

**How It Works**:

```python
def _is_lane_compatible(lane, vclass="passenger"):
    # Check lane-level restrictions
    if vclass in lane.getDisallowed():
        return False

    # Check edge-level restrictions
    if vclass in lane.getEdge().getDisallowed():
        return False

    # Check explicit allow list
    allowed = lane.getAllowed()
    if allowed and vclass not in allowed:
        return False  # Lane only allows specific types (e.g., only "bus")

    return True
```

---

## Advanced Features

### Visualization

Generate visualizations of construction zones and traffic flow.

**Static PNG Image**:

```bash
python visualizer.py \
    --net scenes/ann_arbor_whole/map.net.xml \
    --fcd outputs/zone_xxx/fcd_all.xml \
    --output outputs/zone_xxx/construction_zone.png
```

**Interactive HTML Map**:

```bash
python visualizer.py \
    --net scenes/ann_arbor_whole/map.net.xml \
    --fcd outputs/zone_xxx/fcd_all.xml \
    --output outputs/zone_xxx/map.html \
    --mode interactive
```

**HTML Map Features**:
- Multiple base map options (Google Satellite, Street Map, Dark Mode)
- High zoom level support (zoom 21)
- Color-coded cones by zone: orange=taper_in, red=work, green=taper_out
- Click cones to view detailed information

**Long-Distance Segmented Visualization**:

```bash
python visualizer.py \
    --net scenes/scene_xxx/map.net.xml \
    --fcd outputs/zone_xxx/fcd_all.xml \
    --output outputs/zone_xxx/construction_zone.png \
    --mode segmented \
    --segment-length 500
```

### Multi-Edge Construction Zones

The system supports construction zones spanning multiple road segments:

- Automatically uses BFS (Breadth-First Search) to find path between start/end coordinates
- Each edge gets individual lane plans (taper_in, work, taper_out)
- Connection management processes all affected intersections
- Smooth transitions between segments

### Construction Zone Layout

```
Vehicle travel direction →

|←─ warning ─→|←─ taper_in ─→|←───── work ─────→|←─ taper_out ─→|
  Warning Zone    Taper In         Work Zone         Taper Out

    ⚠️  ⚠️  ⚠️     🔶 🔶 🔶 🔶     🔶 🔶 🔶 🔶 🔶 🔶     🔶 🔶 🔶 🔶
  (Warning signs) (Diagonal cones)  (Straight cones)   (Diagonal cones)
```

**Zone Segments**:

1. **Warning Zone**: Advance warning signs (50m before construction)
2. **Taper-In**: Cones gradually shift vehicles away from blocked lane
3. **Work Zone**: Main construction area with dense barriers/cones
4. **Taper-Out**: Cones guide vehicles back to normal lanes

### Pipeline for Full Workflow

Use `pipeline.py` to run complete workflow (generate → simulate → visualize → convert):

```bash
# Basic usage
python pipeline.py --scene ann_arbor_whole --num 1

# With GUI
python pipeline.py --scene ann_arbor_whole --gui

# Full parameters
python pipeline.py \
    --scene ann_arbor_whole \
    --num 5 \
    --width 2.0 \
    --type cone \
    --warning \
    --seed 42 \
    --skip-vis \
    --skip-waymo
```

---

## API Reference

### Python API

```python
from construction_zone_generator import ConstructionZoneGenerator

# ===== Method 1: Use existing scene =====
gen = ConstructionZoneGenerator(0, 0)  # Placeholder coords
gen.use_existing_scene("scenes/ann_arbor_whole")

# ===== Method 2: Create new scene =====
gen = ConstructionZoneGenerator(
    center_lat=42.28,
    center_lon=-83.74,
    size_meters=500,
)
gen.setup_scene()  # Download OSM and generate SUMO map

# ===== Random generation =====
configs = gen.random_generate(
    num_configs=5,
    min_length=30,
    max_length=100,
    run_simulation=False,
)

# ===== Generate from coordinates =====
config = gen.generate_from_coordinates(
    start_lat=42.280,
    start_lon=-83.738,
    end_lat=42.281,
    end_lon=-83.736,
    zone_id="custom_zone",
    run_simulation=False,
)

# ===== Fetch from TomTom =====
configs = gen.generate_from_tomtom(
    api_key="YOUR_API_KEY",
    run_simulation=False,
)
```

### Connection Manager API

```python
from connection_manager import prepare_construction_zone_network

# Generate enhanced network with emergency connections
enhanced_net, modifications = prepare_construction_zone_network(
    net_file="scenes/ann_arbor_whole/map.net.xml",
    construction_edges=["edge_123", "edge_456"],
    lanes_to_close=[0],
    output_net_file="scenes/ann_arbor_whole/map_enhanced.net.xml"
)

print(f"Enhanced network: {enhanced_net}")
print(f"Affected junctions: {len(modifications)}")

# Modifications can be serialized to config
for mod in modifications:
    print(f"Junction {mod.junction_id}:")
    print(f"  Blocked: {len(mod.blocked_connections)}")
    print(f"  Emergency: {len(mod.emergency_connections)}")
```

### CLI Arguments Reference

#### construction_zone_generator.py

**random mode**:
```bash
python construction_zone_generator.py random [OPTIONS]

Required (one of):
  --scene PATH      Use existing scene directory
  --center LAT,LON  Center coordinates (auto-create scene)

Optional:
  --size METERS     Map range (default 500m)
  --num N           Number of configs (default 5)
  --min-length M    Minimum zone length (default 30m)
  --max-length M    Maximum zone length (default 100m)
  --seed N          Random seed
  --run             Run simulations after generation
```

**coords mode**:
```bash
python construction_zone_generator.py coords [OPTIONS]

Required:
  --start LAT,LON   Start coordinates
  --end LAT,LON     End coordinates

Optional:
  --scene PATH      Use existing scene (auto-creates if not provided)
  --size METERS     Scene size (for auto-creation)
  --id NAME         Zone ID (default "manual")
  --lanes N [N...]  Lane indices to close (default [0])
  --run             Run simulation after generation
```

**tomtom mode**:
```bash
python construction_zone_generator.py tomtom [OPTIONS]

Required (one of):
  --api-key KEY     TomTom API key
  (or set TOMTOM_API_KEY environment variable)

Required (one of):
  --scene PATH      Use existing scene
  --center LAT,LON  Center coordinates (auto-create scene)

Optional:
  --size METERS     Search range (default 1000m)
  --run             Run simulations after generation
```

#### pipeline.py

```bash
python pipeline.py [OPTIONS]

  --scene NAME      Scene name (default ann_arbor_whole)
  --num N           Number of configs (default 1)
  --width W         Work zone width (default 2.0m)
  --type TYPE       cone|barrier|mixed (default cone)
  --warning         Enable warning zone
  --gui             Enable SUMO GUI
  --seed N          Random seed
  --skip-vis        Skip visualization
  --skip-waymo      Skip Waymo conversion
```

#### visualizer.py

```bash
python visualizer.py [OPTIONS]

Required:
  --net PATH        SUMO network file (map.net.xml)
  --fcd PATH        FCD trajectory file (fcd_all.xml)

Optional:
  --output PATH     Output file path (default auto-generated)
  --mode MODE       static|interactive|segmented (default auto)
  --segment-length  Segment length (segmented mode, default 500m)
  --time TIME       Time point to extract cones (default 5.0s)
```

### Configuration Format

Example configuration generated by the system:

```yaml
seed: 42
input:
  sumo_net_file: scenes/ann_arbor_whole/map_enhanced.net.xml
  sumo_config_file: scenes/ann_arbor_whole/simulation_no_traffic.sumocfg

environment:
  class: NADE
  module: terasim_nde_nade.envs
  parameters:
    adversity_cfg:
      static:
        simple_urban_construction_zone:
          edges:
            - edge_main_st_1
            - edge_main_st_2
          lanes_to_close:
            - 0
          lane_plans:
            edge_main_st_1_0:
              start: 0.0
              end: 150.0
              warning: 30.0
              taper_in: 15.0
              work: 120.0
              taper_out: 15.0
              zone_spacing:
                warning: 15.0
                work: 20.0
              work_zone_offset: 1.0
          connection_management:
            enabled: true
            modifications:
              - junction_id: junction_456
                blocked_connections:
                  - from_edge: edge_main_st_1
                    to_edge: edge_liberty_st
                    from_lane: 0
                    to_lane: 0
                    direction: r
                    internal_lane: ":junction_456_0_0"
                emergency_connections:
                  - from_edge: edge_main_st_1
                    to_edge: edge_liberty_st
                    from_lane: 1
                    to_lane: 0
                    original_from_lane: 0
```

---

## System Architecture

### Component Overview

```
urban_construction_zone/
├── construction_zone_generator.py  # Main entry point, integrates Phase 1
├── connection_manager.py           # Core connection management (both phases)
├── urban_construction_simple.py    # Runtime adversity, deploys cones + Phase 2
├── pipeline.py                     # Full workflow automation
├── visualizer.py                   # Visualization tools
├── config_generator.py             # Config generation utilities
├── tomtom_pipeline.py              # TomTom API integration
└── scenes/                         # Scene directories
    └── ann_arbor_whole/
        ├── map.net.xml             # Original network
        ├── map_enhanced.net.xml    # Enhanced with emergency connections
        └── simulation_no_traffic.sumocfg
```

### Key Modules

#### connection_manager.py

**Phase 1 Classes**:
- `ConnectionManager`: Analyzes construction impact, generates enhanced networks
- `BlockedConnection`: Dataclass representing blocked turn connection
- `EmergencyConnection`: Dataclass representing emergency alternative
- `JunctionModification`: Container for all modifications at one junction

**Phase 2 Class**:
- `RuntimeConnectionController`: Applies connection rerouting via TraCI at runtime

**Key Methods**:
- `prepare_construction_zone_network()`: One-call wrapper for Phase 1
- `analyze_construction_zone()`: Identifies affected junctions and connections
- `generate_enhanced_network()`: Creates enhanced .net.xml file
- `apply_construction_rerouting()`: Enables emergency connections via TraCI

#### construction_zone_generator.py

**Integration Points**:
- Lines 298-332: Import connection_manager and define serialization
- Lines 742-759: Call `prepare_construction_zone_network()` during config generation
- Lines 783-789: Embed connection_management in configuration

**Key Functions**:
- `_build_config_multi()`: Generates configuration with connection management
- `_serialize_modification()`: Converts Python objects to YAML-compatible dicts
- `_match_to_network_multi()`: Maps coordinates to SUMO network edges

#### urban_construction_simple.py

**Integration Points**:
- Lines 95-121: Initialize `RuntimeConnectionController` from config
- Lines 125-181: `_deserialize_modifications()` reconstructs objects from YAML
- Lines 295-303: Apply connection rerouting during `initialize()`

**Key Functions**:
- `__init__()`: Parse connection management config
- `initialize()`: Deploy cones + enable emergency connections
- `_deploy_lane_zone()`: Place warning signs, cones, barriers
- `update()`: Maintain static objects each simulation step

### Execution Flow

```
┌─────────────────────────────────────────────────────────────┐
│ USER COMMAND                                                 │
│ $ python construction_zone_generator.py coords ...          │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ PHASE 1: OFFLINE PREPARATION                                │
├─────────────────────────────────────────────────────────────┤
│ 1. Parse arguments, create ConstructionZoneSpec             │
│ 2. Match coordinates to network edges                       │
│ 3. Call: prepare_construction_zone_network()                │
│    a. ConnectionManager.analyze_construction_zone()         │
│       - Find affected junctions                             │
│       - Identify blocked turn connections                   │
│       - Find compatible emergency lanes                     │
│    b. ConnectionManager.generate_enhanced_network()         │
│       - Export to PlainXML (.nod/.edg/.con)                 │
│       - Modify .con.xml with emergency connections          │
│       - Rebuild with netconvert                             │
│    c. Return: (map_enhanced.net.xml, modifications)         │
│ 4. Serialize modifications to YAML                          │
│ 5. Generate complete configuration file                     │
│ 6. Save: outputs/config_manual.yaml                         │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ USER RUNS SIMULATION                                         │
│ $ python run_experiments.py --config outputs/config_manual.yaml │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ PHASE 2: RUNTIME EXECUTION                                   │
├─────────────────────────────────────────────────────────────┤
│ 1. TeraSim loads configuration                              │
│ 2. SUMO starts with map_enhanced.net.xml                    │
│ 3. UrbanConstructionZone.__init__()              │
│    a. Parse connection_management from config               │
│    b. Deserialize modifications                             │
│    c. Create RuntimeConnectionController                    │
│ 4. UrbanConstructionZone.initialize()            │
│    a. Deploy physical elements:                             │
│       - Warning signs (50m before)                          │
│       - Taper-in cones (guide to open lanes)                │
│       - Work zone barriers (block closed lanes)             │
│       - Taper-out cones (guide back)                        │
│    b. Apply connection rerouting:                           │
│       - traci.lane.setDisallowed(blocked_internal)          │
│       - traci.lane.setAllowed(emergency_internal)           │
│ 5. Simulation runs with intelligent rerouting               │
└─────────────────────────────────────────────────────────────┘
```

### Data Structures

**BlockedConnection**:
```python
@dataclass
class BlockedConnection:
    from_edge: str           # Source edge ID
    to_edge: str             # Destination edge ID
    from_lane: int           # Source lane index
    to_lane: int             # Destination lane index
    junction_id: str         # Affected junction
    turn_direction: str      # 'r', 'l', 's', 't'
    internal_lane: str       # Internal lane ID (e.g., ":junction_0_0")
```

**EmergencyConnection**:
```python
@dataclass
class EmergencyConnection:
    from_edge: str           # Source edge ID
    to_edge: str             # Destination edge ID
    from_lane: int           # Emergency source lane
    to_lane: int             # Destination lane index
    junction_id: str         # Affected junction
    original_blocked: BlockedConnection  # Reference to replaced connection
    disallow: str = "all"    # Initially disabled
```

**JunctionModification**:
```python
@dataclass
class JunctionModification:
    junction_id: str
    blocked_connections: List[BlockedConnection]
    emergency_connections: List[EmergencyConnection]
```

### Traffic Cone Deployment Details

**Lane Plan Structure** (from configuration):

```python
lane_plans = {
    "edge_123_0": {
        "start": 0.0,         # Construction start position (m)
        "end": 150.0,         # Construction end position (m)
        "warning": 30.0,      # Warning zone length (m)
        "taper_in": 15.0,     # Taper-in length (m)
        "work": 120.0,        # Work zone length (m)
        "taper_out": 15.0,    # Taper-out length (m)
        "zone_spacing": {
            "warning": 15.0,  # Spacing between warning signs (m)
            "work": 20.0      # Spacing between cones/barriers (m)
        },
        "work_zone_offset": 1.0  # Lateral offset for work zone (m)
    }
}
```

**Deployment Algorithm** (`_deploy_lane_zone()`):

1. **Create Route**: `traci.route.add(route_id, [edge_id])`
2. **Calculate Zone Boundaries**:
   - Warning: `[start - warning_len, start]`
   - Taper-In: `[start, start + taper_in_len]`
   - Work: `[start + taper_in_len, start + taper_in_len + work_len]`
   - Taper-Out: `[end - taper_out_len, end]`

3. **Place Warning Signs** (Warning Zone):
   - Type: `"work_zone_sign"`
   - Spacing: 15m (default)
   - Lateral Position: Right edge + 0.5m outside
   - Static: Speed = 0, no lane changes

4. **Place Cones/Barriers** (Taper-In, Work, Taper-Out):
   - **Taper-In**: Linear interpolation from right edge to work offset
     ```
     lateral(t) = right_edge + t × (work_offset - right_edge)
     where t = (pos - taper_in_start) / taper_in_length
     ```
   - **Work Zone**: Fixed at `work_zone_offset`
   - **Taper-Out**: Linear interpolation from work offset back to right edge

5. **Object Properties**:
   - `traci.vehicle.add()`: Create as vehicle object
   - `traci.vehicle.moveTo()`: Position on lane
   - `traci.vehicle.changeSublane()`: Lateral offset
   - `traci.vehicle.setSpeed(0)`: Make static
   - `setSpeedMode(0)`, `setLaneChangeMode(0)`: Disable behaviors

**Coordinate System**:
- Longitudinal: Meter position along lane (0 = lane start)
- Lateral: Meters from lane center (negative = right, positive = left)
- Lane width typically 3.5m
- Right edge = `-(lane_width/2 - 0.3)` = -1.45m (0.3m safety margin)

---

## Testing & Validation

### Automated Tests

```bash
# Run connection manager tests
python test_connection_manager.py --mode workflow

# List junctions in network
python test_connection_manager.py --mode junctions

# Analyze specific edge
python test_connection_manager.py --mode edge
```

### Manual Verification

**1. Verify Enhanced Network in SUMO-GUI**:

```bash
sumo-gui -n scenes/ann_arbor_whole/map_enhanced.net.xml
```

In GUI:
- View → Junctions → Show lane-to-lane connections
- Check for extra connections (emergency alternatives)
- Verify they're initially disabled (no traffic flow)

**2. Compare Networks**:

```bash
# Export both networks to PlainXML
netconvert -s map.net.xml --plain-output-prefix orig
netconvert -s map_enhanced.net.xml --plain-output-prefix enhanced

# Compare connection files
diff orig.con.xml enhanced.con.xml
```

Look for new `<connection>` elements with `disallow="all"`.

**3. Runtime Verification**:

Check simulation logs for:
```
INFO: Connection management initialized for 3 junctions
INFO: Initialized workzones on 1 lanes, total objects: 45
INFO: Applying rerouting for junction junction_456:
INFO:   Disabled: :junction_456_0_0 (r turn)
INFO:   Enabled emergency: :junction_456_1_0
INFO: Construction zone connection rerouting applied successfully
```

**4. Test Vehicle Behavior**:

In SUMO-GUI with simulation running:
- Watch vehicles approach construction zone
- Verify vehicles see warning signs 50m ahead
- Confirm vehicles change lanes before taper-in
- Check vehicles can turn from emergency lanes at intersections
- Ensure no vehicles stuck or making illegal turns

### Debug Connection Management

```python
import traci

traci.start(["sumo", "-c", "simulation.sumocfg"])

# Check internal lane permissions
internal_lane = ":junction_456_0_0"
allowed = traci.lane.getAllowed(internal_lane)
disallowed = traci.lane.getDisallowed(internal_lane)

print(f"Allowed: {allowed}")        # Should be [] for blocked
print(f"Disallowed: {disallowed}")  # Should be ["all"] for blocked

# For emergency lanes, should be ["passenger", "bus", "truck"] allowed
```

---

## Troubleshooting

### Common Issues

**Q: No eligible edges found?**

A: No multi-lane roads in the scene (need at least 2 lanes), or road length insufficient (<20m).

**Solution**:
- Verify scene has roads with 2+ lanes: `python test_connection_manager.py --mode junctions`
- Increase search area: `--size 1000`
- Check road segment lengths in SUMO-GUI

---

**Q: Coordinate matching failed?**

A: Coordinates are more than 100m from nearest road. Ensure coordinates are within scene bounds.

**Solution**:
- View coordinates in Google Maps to verify they're on roads
- Use `--size` larger than distance to coordinates
- Check if OSM data includes those roads: view in SUMO-GUI

---

**Q: TomTom returns empty results?**

A: No construction zones in the area, or API key issue. Try other cities like Denver or Los Angeles.

**Solution**:
- Verify API key: `curl "https://api.tomtom.com/traffic/services/4/incidentDetails?key=YOUR_KEY&bbox=..."`
- Try larger search area: `--size 2000`
- Test with major cities known to have construction

---

**Q: Cones not showing in simulation?**

A: Check for `Skip {edge}: lane_count < 2` warnings in logs, indicating insufficient lane count.

**Solution**:
- Verify lane count: `python test_connection_manager.py --mode edge`
- Some road types may not support construction (single-lane, ramps)

---

**Q: Emergency connections not working?**

A: Internal lane ID query failed, or TraCI permission modification failed.

**Solution**:
- Check logs for "Failed to find internal lane" messages
- Verify enhanced network loaded: `grep "sumo_net_file" config.yaml`
- Test TraCI connection: Run simulation with `--verbose` flag

---

**Q: Vehicles ignore warning signs?**

A: Warning signs are visual only, vehicles react based on static objects (cones) ahead.

**Behavior**: This is expected - warning signs inform but don't force behavior changes. Vehicles react to taper-in cones.

---

**Q: BRT lanes selected as emergency lanes?**

A: Should not happen with v1.1+ (lane filtering). If it does, network may not have correct lane permissions.

**Solution**:
- Verify network has lane permissions: Check `.net.xml` for `allow`/`disallow` attributes
- Update OSM data with correct lane tags
- Manually specify allowed lanes (advanced usage)

---

### Debug Tips

**Enable Verbose Logging**:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

**Inspect Generated Config**:

```bash
cat outputs/config_manual.yaml | grep -A 20 "connection_management"
```

**Check Enhanced Network**:

```bash
# Count emergency connections
grep -c 'disallow="all"' scenes/ann_arbor_whole/map_enhanced.net.xml
```

**Visualize with Labels**:

Use visualizer with `--mode interactive` to see cone positions and IDs.

---

## Technical Reference

### System Requirements

- SUMO 1.23.1+ (installed automatically by TeraSim)
- Python 3.8+
- SUMO's `netconvert` tool in PATH

### Dependencies

**Core**:
```bash
pip install sumolib pyyaml matplotlib folium
```

**Optional** (for specific features):
```bash
pip install osmnx      # Auto-create scenes from OSM
pip install requests   # TomTom API calls
```

### Constraints

| Constraint | Value | Description |
|------------|-------|-------------|
| Minimum zone length | 20m | Ensure space for cone placement |
| Boundary margin | 5m | Safety distance from lane boundaries |
| Minimum lane count | 2 | Need at least one passable lane |
| Coordinate matching distance | 100m | Max distance from lat/lon to road |
| Cone spacing | 2m | Default spacing between cones in work zone |
| Warning sign spacing | 15m | Default spacing between warning signs |
| Internal lane query radius | 50m | TraCI search radius for connections |

### SUMO Integration

**Network Format**:
- `.net.xml`: SUMO network file (topology)
- `.nod.xml`: Plain XML nodes (intermediate)
- `.edg.xml`: Plain XML edges (intermediate)
- `.con.xml`: Plain XML connections (we modify this)

**TraCI APIs Used**:
- `traci.lane.setDisallowed(lane_id, vclasses)`: Disable lane for vehicle classes
- `traci.lane.setAllowed(lane_id, vclasses)`: Enable lane for vehicle classes
- `traci.lane.getLinks(lane_id)`: Query outgoing connections
- `traci.vehicle.add()`: Create static construction objects
- `traci.vehicle.moveTo()`: Position objects on lanes
- `traci.vehicle.changeSublane()`: Set lateral position
- `traci.vehicle.setSpeed(0)`: Make objects static

**Internal Lanes**:
- Format: `:junction_id_#_#` (e.g., `:cluster_123_0_0`)
- Represent connections inside junctions
- Must modify these (not regular lanes) to control turns
- Automatically generated by SUMO

### File Structure

```
outputs/
└── zone_manual_1737545678/
    ├── fcd_all.xml                    # Trajectory data
    ├── construction_zone.png          # Static visualization
    ├── construction_zone.html         # Interactive map
    ├── waymo_scenario.pb              # Waymo format (if converted)
    └── simulation.log                 # Simulation log

scenes/
└── ann_arbor_whole/
    ├── map.net.xml                    # Original network
    ├── map_enhanced.net.xml           # Enhanced with emergency connections
    ├── map.osm                        # OSM source data
    └── simulation_no_traffic.sumocfg  # SUMO configuration

configs/
├── base.yaml                          # Base configuration template
├── environment.yaml                   # Environment template
└── adversities/
    └── construction_zone.yaml         # Adversity template
```

### Version History

**v2.0** (Current):
- Full automatic integration (no manual steps)
- Connection management enabled by default
- Seamless Phase 1 + Phase 2 workflow

**v1.1**:
- Lane type filtering (BRT/bus-only lanes excluded)
- Enhanced `_is_lane_compatible()` method
- Multi-lane closure deduplication
- Support for various vehicle class checks

**v1.0**:
- Basic two-phase architecture
- Single lane closure support
- Automatic emergency connection generation

### Future Enhancements

- [ ] Pre-compute internal lane IDs (avoid runtime queries)
- [ ] Visualization tool for emergency connections
- [ ] Enhanced network caching mechanism
- [ ] Multi-vehicle-type support (truck + passenger simultaneously)
- [ ] Temporary traffic signal adjustments
- [ ] Support for partial lane closures (e.g., shoulder use)

---

## References

### SUMO Documentation

- [PlainXML Format](https://sumo.dlr.de/docs/Networks/PlainXML.html)
- [TraCI Lane API](https://sumo.dlr.de/docs/TraCI/Change_Lane_State.html)
- [Rerouter](https://sumo.dlr.de/docs/Simulation/Rerouter.html)
- [Vehicle Types](https://sumo.dlr.de/docs/Definition_of_Vehicles,_Vehicle_Types,_and_Routes.html)

### Related TeraSim Documentation

- Main TeraSim README: `/README.md`
- NADE environments: `packages/terasim-nde-nade/README.md`
- Adversity system: `packages/terasim-nde-nade/terasim_nde_nade/adversity/README.md`

### External Resources

- TomTom Traffic API: https://developer.tomtom.com/traffic-api
- OpenStreetMap: https://www.openstreetmap.org
- SUMO User Documentation: https://sumo.dlr.de/docs/

---

## Support

For issues or questions:
- Check the [Troubleshooting](#troubleshooting) section
- Review archived documentation: `*.v1.md` files
- Submit issues to TeraSim GitHub repository

---

*This documentation consolidates and refines content from README.md, CONNECTION_MANAGER_README.md, INTEGRATION_GUIDE.md, and SYSTEM_OVERVIEW.md. Previous versions archived with .v1 suffix.*
