#!/usr/bin/env python3
"""TeraSim's edits of a VSS 3.3.0 checkout (README.md, step 4).

Run after NVIDIA's smart-cities overlay and before the first start of VSS:

    python3 configure-vss.py <VSS repository> <sample data folder> <MCP URL>

<sample data folder> is sj-reference-corridor-0.2.0 with the four recordings in videos/;
<MCP URL> is http://<instance private IP>:9911/mcp (the VSS 3.3 agent runs on its own Docker
network, so the host's 127.0.0.1 is not the host for it).

Each edit is made once: running the script again changes nothing. When a VSS file does not
hold the text an edit is anchored on, the script stops and names the file. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Callable, NoReturn

AGENT_DOC = Path(__file__).resolve().parents[2] / "docs" / "deployment" / "vss-integration.md"
CAMERAS = [f"cam{n}.mp4" for n in range(1, 5)]
LABELS = "background\ntwo_wheeler\nVehicle\nPerson\nroad_sign\n"
OSM_CACHE_VOLUME = (
    "      - $VSS_APPS_DIR/developer-profiles/dev-profile-alerts/vss-behavior-analytics/osm-cache"
    ":/behavior-analytics/cache"
)
RECEIVE_BUFFER = "receive_buffer_bytes => 8388608"
FILES_BACKEND = 'backend bk_terasim_files\n    server s1 "${HOST_IP}:13002" check\n\n'
FILES_RULE = (
    "    acl p_terasim path_beg /terasim/\n    use_backend bk_terasim_files if h_main p_terasim\n\n"
)
SIGNAL_SECTION = "    SIGNAL TIMING OPTIMIZATION (TeraSim):"


def fail(path: Path, message: str) -> NoReturn:
    sys.exit(f"{path}: {message}")


def write(path: Path, text: str) -> str:
    """Write ``text`` in place (same inode, same owner) when it differs from the file."""
    if path.read_text() == text:
        return "unchanged"
    with open(path, "r+") as f:
        f.write(text)
        f.truncate()
    return "changed"


def find(lines: list[str], line: str, path: Path, start: int = 0) -> int:
    try:
        return lines.index(line, start)
    except ValueError:
        fail(path, f"no line {line.strip()!r}")


def block_end(lines: list[str], start: int) -> int:
    """Index of the first line after ``lines[start]`` that is not indented deeper than it."""
    depth = len(lines[start]) - len(lines[start].lstrip())
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= depth:
            return i
    return len(lines)


def copy_videos(docker: Path, videos: Path) -> str:
    target = docker / "vss-nvstreamer-video-dir" / "videos" / "dev-profile-alerts"
    target.mkdir(parents=True, exist_ok=True)
    result = "unchanged"
    for name in CAMERAS:
        src, dst = videos / name, target / name
        if not src.is_file():
            fail(src, "missing: the four recordings go into the sample data folder's videos/")
        if not dst.is_file() or dst.stat().st_size != src.stat().st_size:
            shutil.copyfile(src, dst)
            result = "changed"
    others = sorted(p.name for p in target.iterdir() if p.name not in CAMERAS)
    if others:
        print(f"note: {target} also holds {', '.join(others)}; VSS streams only cam1 to cam4 here")
    return result


def write_labels(profile: Path) -> str:
    path = profile / "deepstream" / "configs" / "rtdetr-960x544-labels.txt"
    if not path.is_file():
        fail(path, "missing")
    return write(path, LABELS)


def copy_osm_cache(profile: Path, data: Path) -> str:
    src, dst = data / "vss" / "osm-cache", profile / "vss-behavior-analytics" / "osm-cache"
    names = sorted(p.name for p in src.iterdir())
    if dst.is_dir() and all((dst / n).is_file() for n in names):
        return "unchanged"
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return "changed"


def edit_behavior_analytics(profile: Path, data: Path) -> str:
    path = profile / "vss-behavior-analytics/configs/vss-behavior-analytics-kafka-config.json"
    if not path.is_file():
        fail(path, "missing: run the smart-cities overlay first")
    config = json.loads(path.read_text())
    graph = json.loads((data / "vss" / "behavior-analytics-graph.json").read_text())
    try:
        config["coordinateReferenceSystem"]["roadNetwork"]["graph"].update(graph)
        item = next(i for i in config["app"] if i["name"] == "mapMatchingClasses")
    except (KeyError, StopIteration):
        fail(path, "no roadNetwork graph or mapMatchingClasses setting")
    classes = json.loads(item["value"])
    classes += [c for c in ("Vehicle", "Person", "two_wheeler") if c not in classes]
    item["value"] = json.dumps(classes, separators=(",", ":"))
    return write(path, json.dumps(config, indent=2))


def edit_logstash(docker: Path) -> str:
    """Kafka input receive buffer of 8 MB: the default 32 KB starves partitions under load."""
    path = docker / "services/infra/elk/logstash/pipelines/kafka/mdx-logstash.conf"
    lines = path.read_text().split("\n")
    out, inputs = [], 0
    for i, line in enumerate(lines):
        out.append(line)
        match = re.fullmatch(r"(\s*)consumer_threads => \d+", line)
        if match:
            inputs += 1
            if i + 1 == len(lines) or "receive_buffer_bytes" not in lines[i + 1]:
                out.append(match.group(1) + RECEIVE_BUFFER)
    if not inputs:
        fail(path, "no 'consumer_threads => <n>' line")
    return write(path, "\n".join(out))


def edit_profile_compose(profile: Path) -> str:
    """Mount the San José OpenStreetMap cache into behavior analytics (no Overpass download)."""
    path = profile / "compose.yml"
    lines = path.read_text().split("\n")
    start = find(lines, "  vss-behavior-analytics-alerts:", path)
    end = block_end(lines, start)
    if any("/behavior-analytics/cache" in line for line in lines[start:end]):
        return "unchanged"
    volumes = find(lines[:end], "    volumes:", path, start)
    lines.insert(volumes + 1, OSM_CACHE_VOLUME)
    return write(path, "\n".join(lines))


def edit_haproxy(docker: Path, timeout: str) -> str:
    """Idle timeouts longer than a tool call; /terasim/ to the download links (README, step 3)."""
    path = docker / "services" / "infra" / "haproxy" / "haproxy.cfg.template"
    lines = path.read_text().split("\n")
    start = find(lines, "defaults", path)
    edited = 0
    for i in range(start + 1, block_end(lines, start)):
        match = re.fullmatch(r"(\s+timeout (?:client|server)) \S+", lines[i])
        if match:
            lines[i] = f"{match.group(1)} {timeout}"
            edited += 1
    if edited != 2:
        fail(path, "the defaults section does not set timeout client and timeout server")
    text = "\n".join(lines)
    if "backend bk_terasim_files" not in text:
        if text.count("\nfrontend fe_http\n") != 1:
            fail(path, "no frontend fe_http")
        text = text.replace("\nfrontend fe_http\n", "\n" + FILES_BACKEND + "frontend fe_http\n")
    if "acl p_terasim " not in text:
        anchor = "    acl p_phoenix path /phoenix\n"
        if text.count(anchor) != 1:
            fail(path, "no /phoenix rule in frontend fe_http")
        text = text.replace(anchor, FILES_RULE + anchor)
    return write(path, text)


def agent_blocks() -> list[str]:
    """Items 1 to 4 of "VSS agent configuration" in docs/deployment/vss-integration.md."""
    doc = AGENT_DOC.read_text()
    section = doc[doc.index("## VSS agent configuration") :]
    section = section[: section.index("\n## ", 1)]
    blocks = re.findall(r"```yaml\n(.*?)```", section, flags=re.S)[:4]
    if len(blocks) != 4 or SIGNAL_SECTION not in blocks[2]:
        fail(AGENT_DOC, "section 'VSS agent configuration' does not hold the four YAML blocks")
    return [block.rstrip("\n") for block in blocks]


def list_end(lines: list[str], start: int) -> int:
    """Index of the first blank line after ``lines[start]``: the end of a prompt list."""
    return next((i for i in range(start + 1, len(lines)) if not lines[i].strip()), len(lines))


def add_tool_group(lines: list[str], path: Path, group: str, url: str, timeout: int) -> None:
    """Item 1: the terasim_mcp group at the end of function_groups; URL and timeout kept current."""
    if "  terasim_mcp:" in lines:
        start = lines.index("  terasim_mcp:")
        end = block_end(lines, start)
        group = "\n".join(lines[start:end])
    else:
        start = end = find(lines, "functions:", path)
        if "function_groups:" not in lines[:start]:
            fail(path, "function_groups: does not come before functions:")
        group += "\n"
    group = re.sub(r"(?m)^(\s*url: ).*$", lambda m: m.group(1) + url, group)
    group = re.sub(r"(?m)^(\s*tool_call_timeout: ).*$", lambda m: m.group(1) + str(timeout), group)
    lines[start:end] = group.split("\n")


def add_tool_names(lines: list[str], path: Path, names: str) -> None:
    """Item 2: the tools after the last workflow.tool_names entry."""
    if "  - terasim_mcp.list_intersections" in lines:
        return
    at = find(lines, "  tool_names:", path, find(lines, "workflow:", path)) + 1
    while at < len(lines) and lines[at].startswith("  - "):
        at += 1
    lines[at:at] = names.split("\n")


def add_prompt(lines: list[str], path: Path, priority: str, response: str) -> None:
    """Items 3 and 4: PRIORITY ACTIONS item 8, the SIGNAL TIMING OPTIMIZATION section before
    GET PLACES TOOL, and RESPONSE FORMAT item 4 after item 3."""
    if SIGNAL_SECTION not in lines:
        prio = priority.split("\n")
        k = prio.index(SIGNAL_SECTION)
        start = find(lines, "    PRIORITY ACTIONS:", path)
        end = list_end(lines, start)
        if not any(line.startswith("    7. ") for line in lines[start:end]):
            fail(path, "the PRIORITY ACTIONS list does not end with item 7")
        lines[end:end] = [line for line in prio[:k] if line.strip()]
        at = find(lines, "    GET PLACES TOOL:", path)
        lines[at:at] = prio[k:] + [""]
    item4 = response.split("\n")
    if item4[0] in lines:
        return
    start = find(lines, "    RESPONSE FORMAT (CRITICAL):", path)
    end = list_end(lines, start)
    three = [i for i in range(start + 1, end) if lines[i].startswith("    3. ")]
    if not three:
        fail(path, "the RESPONSE FORMAT (CRITICAL) list has no item 3")
    at = three[0] + 1
    while at < end and lines[at].startswith("     "):
        at += 1
    lines[at:at] = item4


def edit_agent_config(profile: Path, mcp_url: str, tool_call_timeout: int) -> str:
    """Items 1 to 6 of "VSS agent configuration", with this deployment's URL and timeout."""
    path = profile / "vss-agent" / "configs" / "config.yml"
    group, names, priority, response = agent_blocks()
    text = path.read_text()
    backup = path.with_name(path.name + ".bak")
    if "\n  terasim_mcp:\n" not in text and not backup.exists():
        shutil.copy2(path, backup)
    lines = text.split("\n")
    add_tool_group(lines, path, group, mcp_url, tool_call_timeout)
    add_tool_names(lines, path, names)
    add_prompt(lines, path, priority, response)
    text = "\n".join(lines)
    # 5. and 6.
    text = re.sub(r"(?m)^  max_iterations: 15$", "  max_iterations: 25", text)
    text = re.sub(r"(?m)^  planning_enabled: true$", "  planning_enabled: false", text)
    return write(path, text)


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "vss", type=Path, help="the VSS repository (video-search-and-summarization)"
    )
    parser.add_argument(
        "data", type=Path, help="the sample data folder (sj-reference-corridor-0.2.0)"
    )
    parser.add_argument(
        "mcp_url", help="the TeraSim MCP server: http://<instance private IP>:9911/mcp"
    )
    parser.add_argument(
        "--tool-call-timeout",
        type=int,
        default=1800,
        help="seconds the agent waits for one optimization (default 1800)",
    )
    parser.add_argument(
        "--proxy-timeout",
        default="2100s",
        help="HAProxy client and server idle timeout (default 2100s)",
    )
    args = parser.parse_args()

    if re.search(r"//(127\.|localhost|0\.0\.0\.0)", args.mcp_url):
        sys.exit("MCP URL: the VSS 3.3 agent cannot reach the host's loopback; use the private IP")
    docker = args.vss / "deploy" / "docker"
    profile = docker / "developer-profiles" / "dev-profile-alerts"
    if not (profile / "smc-app").is_dir():
        sys.exit(
            f"{profile}: no smc-app folder; run NVIDIA's smart-cities overlay first"
            " (README, step 4)"
        )
    metadata = args.vss / "release_metadata.yaml"
    tag = metadata.read_text().strip() if metadata.is_file() else "unknown"
    if tag != "tag: 3.3.0":
        print(f"note: written for VSS 3.3.0; {metadata} reads {tag!r}")

    edits: list[tuple[str, Callable[[], str]]] = [
        ("recordings cam1 to cam4", lambda: copy_videos(docker, args.data / "videos")),
        ("detector labels", lambda: write_labels(profile)),
        ("OpenStreetMap cache", lambda: copy_osm_cache(profile, args.data)),
        (
            "behavior analytics road network and map matching",
            lambda: edit_behavior_analytics(profile, args.data),
        ),
        ("Logstash Kafka receive buffer", lambda: edit_logstash(docker)),
        ("profile compose.yml: OpenStreetMap cache volume", lambda: edit_profile_compose(profile)),
        ("HAProxy timeouts and /terasim/ route", lambda: edit_haproxy(docker, args.proxy_timeout)),
        (
            "VSS agent config.yml: terasim_mcp tools and prompt",
            lambda: edit_agent_config(profile, args.mcp_url, args.tool_call_timeout),
        ),
    ]
    for name, edit in edits:
        print(f"{edit():9}  {name}")


if __name__ == "__main__":
    main()
