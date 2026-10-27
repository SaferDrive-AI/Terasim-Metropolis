#!/usr/bin/env python3
# Vendored verbatim from the reference script NVIDIA shared for the TeraSim x
# Metropolis Phase 1 work (generate_tmc.py, received 2026-09-04). Do not edit:
# terasim_tmc_service.generator imports the pure counting helpers from here so
# the microservice reproduces NVIDIA's TMC semantics exactly. To take a newer
# upstream version, replace this file and re-run tests/test_tmc_service.
"""Load MDX/API.json and aggregate movement counts."""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen


DEFAULT_TMC_PATH = Path("TMC.json")
MAX_RESULT_SIZE = 10000
BEHAVIOR_PORT = 8081
VST_PORT = 30888
#: Detector labels (behavior ``object.type``) counted as traffic. Override with the environment
#: variable ``TMC_COUNTED_OBJECT_TYPES`` (comma-separated, e.g. ``car,bicycle``) or the
#: ``--counted-object-types`` option. Matching ignores case and treats "-" and "_" alike, so
#: ``two-wheeler``, ``Two_Wheeler`` and ``two_wheeler`` are the same label.
DEFAULT_COUNTED_OBJECT_TYPES = ("Vehicle", "two_wheeler")
COUNTED_OBJECT_TYPES_ENV = "TMC_COUNTED_OBJECT_TYPES"


def normalize_object_type(value: Any) -> str | None:
    """Canonical form of one label: stripped, lower case, "-" read as "_"."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_")
    return normalized or None


def parse_counted_object_types(value: str | Iterable[str] | None) -> frozenset[str]:
    """The counted label set from a comma-separated string or a list; None gives the default."""
    if value is None:
        names: Iterable[Any] = DEFAULT_COUNTED_OBJECT_TYPES
    elif isinstance(value, str):
        names = value.split(",")
    else:
        names = value
    counted = frozenset(
        normalized for normalized in (normalize_object_type(name) for name in names) if normalized
    )
    if not counted:
        raise ValueError(
            f"{COUNTED_OBJECT_TYPES_ENV} must name at least one object type, got {value!r}"
        )
    return counted


def counted_object_types_from_env(environ: Any = None) -> frozenset[str]:
    """The counted label set from ``TMC_COUNTED_OBJECT_TYPES``, else the default."""
    environ = os.environ if environ is None else environ
    value = environ.get(COUNTED_OBJECT_TYPES_ENV)
    if value is not None and not value.strip():
        value = None
    return parse_counted_object_types(value)


def is_counted_object_type(value: Any, counted_object_types: frozenset[str]) -> bool:
    """True when the behavior's ``object.type`` is one of the counted labels."""
    normalized = normalize_object_type(value)
    return normalized is not None and normalized in counted_object_types


#: The default counted set in canonical form (kept for callers of the former constant).
COUNTED_OBJECT_TYPES = parse_counted_object_types(None)


def load_json(path: Path) -> Any:
    """Load JSON from disk."""
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def build_behavior_endpoint(vss_host: str) -> str:
    """Build the behavior endpoint URL from VSS host and the fixed behavior port."""
    return f"http://{vss_host}:{BEHAVIOR_PORT}/behavior"


def download_vst_video(
    vss_host: str, sensor_name: str, timestamp_start: str, timestamp_end: str
) -> str:
    """Download a VST replay video for one sensor and return its file name."""
    base_url = f"http://{vss_host}:{VST_PORT}/vst/api/v1/storage/file/{sensor_name}/url"
    configuration = {
        "overlay": {
            "bbox": {
                "showAll": True,
                "objectId": [],
                "showObjId": True,
                "objIdPosition": 0,
            },
            "tripwire": {
                "showAll": True,
                "objectId": [],
            },
            "roi": {
                "showAll": True,
                "objectId": [],
            },
            "color": "red",
            "thickness": 4,
            "opacity": 255,
            "debug": False,
        },
        "container": "mp4",
    }
    params = {
        "startTime": timestamp_start,
        "endTime": timestamp_end,
        "configuration": json.dumps(configuration),
    }
    url = f"{base_url}?{urlencode(params)}"

    with urlopen(url, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))

    video_url = result["videoUrl"]
    filename = Path(urlparse(video_url).path).name

    with urlopen(video_url, timeout=120) as response:
        Path(filename).write_bytes(response.read())

    return filename


def write_timestamp_json(path: Path, timestamp_start: str, timestamp_end: str) -> None:
    """Write timestamp window JSON for one intersection and sensor."""
    payload = {
        "timestamp_start": timestamp_start,
        "timestamp_end": timestamp_end,
        "objects": [],
        "objects_without_edges": [],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def append_object_without_edges(path: Path, object_id: Any) -> None:
    """Append one object id without edges to a timestamp JSON file."""
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise TypeError(f"Expected timestamp JSON file to contain an object: {path}")

    objects_without_edges = payload.setdefault("objects_without_edges", [])
    if not isinstance(objects_without_edges, list):
        raise TypeError(
            f"Expected timestamp JSON objects_without_edges field to be a list: {path}"
        )

    objects_without_edges.append(object_id)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def append_object_movement(
    path: Path, object_id: Any, first_heading: str, movement: str
) -> None:
    """Append one object movement entry to a timestamp JSON file."""
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise TypeError(f"Expected timestamp JSON file to contain an object: {path}")

    objects = payload.setdefault("objects", [])
    if not isinstance(objects, list):
        raise TypeError(f"Expected timestamp JSON objects field to be a list: {path}")

    objects.append(
        {
            "objectID": object_id,
            first_heading: movement,
        }
    )
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def format_utc_seconds(value: datetime) -> str:
    """Format a UTC datetime with seconds resolution and Z suffix."""
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_endpoint_timestamp(value: str) -> str:
    """Format a seconds-resolution Z timestamp for the endpoint query."""
    if not value.endswith("Z"):
        raise ValueError(f"Expected UTC timestamp to end with Z: {value}")

    return f"{value[:-1]}.000Z"


def parse_utc_timestamp(value: Any) -> datetime:
    """Parse a UTC timestamp with Z suffix."""
    if not isinstance(value, str):
        raise TypeError(f"Expected timestamp to be a string, got {type(value).__name__}")
    if not value.endswith("Z"):
        raise ValueError(f"Expected UTC timestamp to end with Z: {value}")

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_duration_hms(value: timedelta) -> str:
    """Format a duration as h m s, omitting zero values."""
    total_seconds = int(value.total_seconds() + 0.5)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []

    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds:
        parts.append(f"{seconds}s")

    return " ".join(parts) if parts else "0s"


def get_behavior_objects_from_file(path: Path) -> list[Any]:
    """Return behavior objects from one behavior JSON file."""
    data = load_json(path)
    if not isinstance(data, dict):
        raise TypeError(f"Expected a JSON object in {path}, got {type(data).__name__}")

    behaviors = data.get("behaviors")
    if not isinstance(behaviors, list):
        raise TypeError(f"Expected behaviors to be a list in {path}")

    return behaviors


def get_behavior_objects_from_files(paths: list[Path]) -> list[Any]:
    """Return behavior objects from all provided behavior JSON files."""
    if not paths:
        raise ValueError("Expected at least one behavior file")

    behaviors: list[Any] = []
    for path in paths:
        behaviors.extend(get_behavior_objects_from_file(path))

    return behaviors


def get_behavior_time_window(behavior_paths: list[Path]) -> tuple[str, str, str]:
    """Return global timestamps and the largest per-file end-time interval."""

    end_values: list[tuple[datetime, str]] = []
    observation_intervals: list[timedelta] = []
    for source in behavior_paths:
        source_end_values: list[tuple[datetime, str]] = []
        for index, behavior in enumerate(
            get_behavior_objects_from_file(source), start=1
        ):
            if not isinstance(behavior, dict):
                raise TypeError(f"Expected behavior {index} in {source} to be an object")

            end_value = behavior.get("end")
            source_end_values.append((parse_utc_timestamp(end_value), end_value))

        if not source_end_values:
            raise ValueError(
                f"Expected at least one behavior with an end timestamp in {source}"
            )

        min_source_end, _ = min(source_end_values, key=lambda item: item[0])
        max_source_end, _ = max(source_end_values, key=lambda item: item[0])
        observation_intervals.append(max_source_end - min_source_end)
        end_values.extend(source_end_values)

    if not end_values:
        raise ValueError(
            "Expected at least one behavior with an end timestamp in behavior files"
        )

    _, min_end_text = min(end_values, key=lambda item: item[0])
    _, max_end_text = max(end_values, key=lambda item: item[0])
    observation_interval = format_duration_hms(max(observation_intervals))
    return min_end_text, max_end_text, observation_interval


def parse_observation_interval(value: Any) -> timedelta:
    """Parse compact observation intervals such as 30s, 5m, 1h, or 1d."""
    if not isinstance(value, str):
        raise TypeError("Expected observation_interval to be a string")

    interval = value.strip()
    if len(interval) < 2:
        raise ValueError("Expected observation_interval to include a value and unit")

    amount_text = interval[:-1]
    unit = interval[-1]

    if not amount_text.isdigit():
        raise ValueError("Expected observation_interval value to be an integer")

    amount = int(amount_text)
    if unit == "s":
        return timedelta(seconds=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)

    raise ValueError(f"Unsupported observation_interval unit: {unit}")


def get_assignment_sensor(value: Any) -> str | None:
    """Return the sensor name from one assignment value."""
    if not isinstance(value, str):
        return None

    _, separator, sensor = value.partition(":")
    sensor_name = sensor.strip() if separator else value.strip()
    return sensor_name or None


def get_assignment_sensors(assignments: dict[str, Any]) -> list[str]:
    """Return distinct sensor names from assignment values, preserving order."""
    sensors: list[str] = []

    for value in assignments.values():
        sensor_name = get_assignment_sensor(value)
        if sensor_name and sensor_name not in sensors:
            sensors.append(sensor_name)

    if not sensors:
        raise TypeError("Expected Assignments to contain at least one sensor name")

    return sensors


def build_heading_counts(movements: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Build heading and movement count buckets from movement metadata."""
    heading_counts: dict[str, dict[str, int]] = {}

    for path, movement in movements.items():
        if not isinstance(path, str) or not isinstance(movement, str):
            continue

        initial_heading, _, _ = path.partition(" - ")
        if not initial_heading:
            continue

        heading_counts.setdefault(initial_heading, {})[movement] = 0

    return heading_counts


def build_tmc(
    sensor_names: list[str], movements: dict[str, Any]
) -> dict[str, dict[str, dict[str, int]]]:
    """Build a TMC object grouped by sensor, heading, then movement."""
    tmc: dict[str, dict[str, dict[str, int]]] = {}

    for sensor_name in sensor_names:
        tmc[sensor_name] = build_heading_counts(movements)

    return tmc


def get_intersections(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Return intersection objects from metadata."""
    intersections = metadata.get("intersections")

    if not isinstance(intersections, list) or not intersections:
        raise TypeError("Expected Metadata.json intersections to be a non-empty list")

    for index, intersection in enumerate(intersections, start=1):
        if not isinstance(intersection, dict):
            raise TypeError(
                f"Expected Metadata.json intersection {index} to be an object"
            )

    return intersections


def get_behaviors(
    behavior_paths: list[Path] | None,
    behavior_endpoint: str | None,
    sensor_name: str,
    timestamp_start: str,
    timestamp_end: str,
    log_endpoint_behaviors: bool,
    counted_object_types: frozenset[str] | None = None,
) -> list[Any]:
    """Return the counted behavior objects for one sensor (``counted_object_types``: from env)."""
    if counted_object_types is None:
        counted_object_types = counted_object_types_from_env()
    if behavior_paths is None:
        if behavior_endpoint is None:
            raise ValueError(
                "Expected behavior endpoint when behavior_path is not provided"
            )

        query_start = format_endpoint_timestamp(timestamp_start)
        query_end = format_endpoint_timestamp(timestamp_end)
        params = {
            "sensorId": sensor_name,
            "queryString": f"end:[{query_start} TO {query_end}]",
            "maxResultSize": MAX_RESULT_SIZE,
        }
        url = f"{behavior_endpoint}?{urlencode(params)}"
        with urlopen(url, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))

        if not isinstance(data, dict):
            raise TypeError(
                f"Expected a JSON object in {behavior_endpoint}, got {type(data).__name__}"
            )

        behaviors = data.get("behaviors")
        if not isinstance(behaviors, list):
            raise TypeError(f"Expected behaviors to be a list in {behavior_endpoint}")

        if log_endpoint_behaviors:
            behavior_log = Path(__file__).with_name(f"{sensor_name}_behavior.json")
            behavior_log.write_text(json.dumps(behaviors, indent=2), encoding="utf-8")
    else:
        behaviors = get_behavior_objects_from_files(behavior_paths)

    matching_behaviors = []
    for item in behaviors:
        if not isinstance(item, dict):
            continue

        sensor = item.get("sensor", {})
        object_data = item.get("object", {})
        if (
            isinstance(sensor, dict)
            and sensor.get("id") == sensor_name
            and isinstance(object_data, dict)
            and is_counted_object_type(object_data.get("type"), counted_object_types)
        ):
            matching_behaviors.append(item)

    return matching_behaviors


def get_object_id(item: dict[str, Any]) -> Any:
    """Return object.id from one behavior item."""
    object_data = item.get("object", {})
    if not isinstance(object_data, dict):
        return None

    return object_data.get("id")


def increment_tmc(
    edges: list[Any],
    headings: dict[str, Any],
    movements: dict[str, Any],
    tmc: dict[str, dict[str, dict[str, int]]],
    sensor_name: str,
    object_id: Any,
    sensor_log: Path | None,
) -> bool:
    """Increment TMC for one record using its first and last edge."""
    first_heading = None
    for edge in edges:
        heading = headings.get(edge)
        if isinstance(heading, str):
            first_heading = heading
            break

    last_heading = None
    for edge in reversed(edges):
        heading = headings.get(edge)
        if isinstance(heading, str):
            last_heading = heading
            break

    if first_heading is None or last_heading is None:
        return False

    path = f"{first_heading} - {last_heading}"
    movement = movements.get(path)

    if not isinstance(movement, str):
        return False

    sensor_counts = tmc.setdefault(sensor_name, {})
    heading_counts = sensor_counts.setdefault(first_heading, {})
    heading_counts[movement] = heading_counts.get(movement, 0) + 1
    if sensor_log is not None:
        append_object_movement(sensor_log, object_id, first_heading, movement)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Load MDX/API.json")
    parser.add_argument(
        "metadata_path",
        type=Path,
        help="Path to the metadata JSON file.",
    )
    parser.add_argument(
        "--behavior-path",
        type=Path,
        nargs="+",
        help=(
            "Optional path to one or more behavior JSON files. "
            "If omitted, fetch from the endpoint."
        ),
    )
    parser.add_argument(
        "--log",
        action="store_true",
        help="Write per-sensor log JSON files.",
    )
    parser.add_argument(
        "--tmc-path",
        type=Path,
        default=DEFAULT_TMC_PATH,
        help=f"Path to write the final FTMC JSON file. Defaults to {DEFAULT_TMC_PATH}",
    )
    parser.add_argument(
        "--vss-host",
        help="VSS host.",
    )
    parser.add_argument(
        "--vst",
        action="store_true",
        help="Download VST replay videos for processed sensors.",
    )
    parser.add_argument(
        "--counted-object-types",
        default=None,
        help=(
            "Comma-separated detector labels to count, e.g. 'car,bicycle' "
            f"(case-insensitive; '-' and '_' are the same). Defaults to ${COUNTED_OBJECT_TYPES_ENV} "
            f"if set, else {','.join(DEFAULT_COUNTED_OBJECT_TYPES)}."
        ),
    )
    args = parser.parse_args()
    counted_object_types = (
        parse_counted_object_types(args.counted_object_types)
        if args.counted_object_types is not None
        else counted_object_types_from_env()
    )

    if args.behavior_path is None and args.vss_host is None:
        parser.error("Either --behavior-path or --vss-host should be provided.")

    if args.vst and args.vss_host is None:
        parser.error("--vst requires --vss-host.")

    metadata = load_json(args.metadata_path)

    if not isinstance(metadata, dict):
        raise TypeError(
            f"Expected a JSON object in {args.metadata_path}, got {type(metadata).__name__}"
        )

    behavior_endpoint = (
        build_behavior_endpoint(args.vss_host)
        if args.vss_host is not None
        else None
    )

    if args.behavior_path is not None:
        timestamp_start, timestamp_end, observation_interval_text = (
            get_behavior_time_window(args.behavior_path)
        )
    else:
        timestamp_end_datetime = datetime.now(timezone.utc).replace(
            microsecond=0
        ) - timedelta(seconds=1)
        observation_interval_text = metadata.get("observation_interval")
        observation_interval = parse_observation_interval(observation_interval_text)
        timestamp_start = format_utc_seconds(
            timestamp_end_datetime - observation_interval
        )
        timestamp_end = format_utc_seconds(timestamp_end_datetime)

    FTMC = {
        "corridor_id": metadata.get("corridor_id"),
        "observation_interval": observation_interval_text,
        "timestamp_start": timestamp_start,
        "intersections": [],
    }

    if args.behavior_path is None:
        print(f"Behavior source: {behavior_endpoint}")
    else:
        print(f"Loaded: {', '.join(str(path) for path in args.behavior_path)}")
    print(f"Loaded metadata: {args.metadata_path}")
    print(f"Counted object types: {', '.join(sorted(counted_object_types))}")
    print(f"Timestamp start: {timestamp_start}")
    print(f"Timestamp end: {timestamp_end}")
    sensor_logs: list[Path] = []
    processed_sensor_names: list[str] = []

    for intersection_index, intersection in enumerate(get_intersections(metadata), start=1):
        intersection_id = intersection.get("intersection_id", f"intersection_{intersection_index}")
        ftmc_intersection = {
            "intersection_id": intersection_id,
            "lat": intersection.get("lat"),
            "lon": intersection.get("lon"),
            "counts": {},
        }
        FTMC["intersections"].append(ftmc_intersection)
        headings = intersection.get("Headings", {})
        movements = intersection.get("Movements", {})
        assignments = intersection.get("Assignments", {})

        if not isinstance(headings, dict):
            raise TypeError(
                f"Expected intersection {intersection_index} Headings to be an object"
            )

        if not isinstance(movements, dict):
            raise TypeError(
                f"Expected intersection {intersection_index} Movements to be an object"
            )

        if not isinstance(assignments, dict):
            raise TypeError(
                f"Expected intersection {intersection_index} Assignments to be an object"
            )

        sensor_names = get_assignment_sensors(assignments)
        for sensor_name in sensor_names:
            if sensor_name not in processed_sensor_names:
                processed_sensor_names.append(sensor_name)

        tmc = build_tmc(sensor_names, movements)
        behavior_count = 0
        sensor_stats: dict[str, dict[str, int]] = {}

        for sensor_name in sensor_names:
            sensor_log = None
            if args.log:
                sensor_log = Path(__file__).with_name(f"{sensor_name}_log.json")
                write_timestamp_json(sensor_log, timestamp_start, timestamp_end)
                sensor_logs.append(sensor_log)

            behaviors = get_behaviors(
                args.behavior_path,
                behavior_endpoint,
                sensor_name,
                timestamp_start,
                timestamp_end,
                args.log,
                counted_object_types=counted_object_types,
            )
            behavior_count = len(behaviors)
            skipped_without_edges = 0
            skipped_without_mapping = 0
            processed_with_edges = 0

            for item in behaviors:
                if not isinstance(item, dict):
                    skipped_without_edges += 1
                    continue

                edges = item.get("edges", [])
                if not edges:
                    if sensor_log is not None:
                        append_object_without_edges(sensor_log, get_object_id(item))
                    skipped_without_edges += 1
                    continue

                processed_with_edges += 1
                object_id = get_object_id(item)
                if not increment_tmc(
                    edges,
                    headings,
                    movements,
                    tmc,
                    sensor_name,
                    object_id,
                    sensor_log,
                ):
                    skipped_without_mapping += 1

            sensor_stats[sensor_name] = {
                "processed_with_edges": processed_with_edges,
                "skipped_without_edges": skipped_without_edges,
                "skipped_without_mapping": skipped_without_mapping,
            }

        for heading, assignment in assignments.items():
            sensor_name = get_assignment_sensor(assignment)
            if not isinstance(heading, str) or sensor_name is None:
                continue

            ftmc_intersection["counts"][heading] = tmc[sensor_name][heading]


        print(f"Intersection {intersection_index}: {intersection_id}")
        print(f"Assignment sensors: {', '.join(sensor_names)}")
        print(f"Behavior objects: {behavior_count}")
        print("Per-sensor processing stats:")
        print(json.dumps(sensor_stats, indent=2))
        print("Final TMC:")
        print(json.dumps(tmc, indent=2))

    print("FTMC:")
    print(json.dumps(FTMC, indent=2))
    args.tmc_path.write_text(json.dumps(FTMC, indent=2), encoding="utf-8")
    if args.log:
        print("Sensor logs:")
        for sensor_log in sensor_logs:
            print(sensor_log)
            print(sensor_log.read_text(encoding="utf-8"))

    if args.vst:
        print("VST downloads:")
        for sensor_name in processed_sensor_names:
            filename = download_vst_video(
                args.vss_host, sensor_name, timestamp_start, timestamp_end
            )
            print(filename)


if __name__ == "__main__":
    main()
