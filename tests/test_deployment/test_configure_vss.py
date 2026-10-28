"""deployment/brev/configure-vss.py on a minimal VSS 3.3.0 tree: every edit lands at its anchor,
a second run changes nothing, and the agent texts come from docs/deployment/vss-integration.md."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deployment" / "brev" / "configure-vss.py"
URL = "http://10.0.0.5:9911/mcp"

AGENT_CONFIG = """\
function_groups:
  video_analytics_mcp:
    _type: mcp_client
    include:
    - video_analytics__get_places

functions:
  video_understanding:
    _type: video_understanding

workflow:
  _type: top_agent
  max_iterations: 15
  planning_enabled: true
  tool_names:
  - get_fov_counts_with_chart
  - vst_picture_url
  subagent_names:
  - report_agent
  prompt: |
    PRIORITY ACTIONS:
    1. If user asks about available sensors -> DIRECTLY CALL a tool
    7. For direct video analytics queries -> use video_analytics_mcp tools

    GET PLACES TOOL:
    Use video_analytics_mcp.video_analytics__get_places.

    RESPONSE FORMAT (CRITICAL):
    1. After your analysis, provide ONLY the clean, formatted answer for the user
    3. Present information directly and professionally

    Example for sensor query:
eval:
  general: {}
"""

PROFILE_COMPOSE = """\
services:

  vss-behavior-analytics-alerts:
    profiles: ["vss-behavior-analytics-alerts"]
    volumes:
      - $VSS_APPS_DIR/a.json:/resources/a.json
    container_name: vss-behavior-analytics
"""

HAPROXY = """\
defaults
    mode http
    timeout connect 10s
    timeout client 120s
    timeout server 120s

backend bk_vss_ui
    timeout server 3600s

frontend fe_http
    acl p_phoenix path /phoenix
    use_backend bk_phoenix_strip if h_main p_phoenix
"""

KAFKA_INPUT = "\tkafka {\n\t\tconsumer_threads => 2\n\t}\n"
LOGSTASH = "input {\n" + KAFKA_INPUT * 2 + "}\n"


@pytest.fixture
def tree(tmp_path):
    vss, data = tmp_path / "vss", tmp_path / "sj-reference-corridor-0.2.0"
    (vss / "release_metadata.yaml").parent.mkdir(parents=True)
    (vss / "release_metadata.yaml").write_text("tag: 3.3.0\n")
    docker = vss / "deploy" / "docker"
    profile = docker / "developer-profiles" / "dev-profile-alerts"
    files = {
        profile / "deepstream" / "configs" / "rtdetr-960x544-labels.txt": "background\nVehicle\n",
        profile / "vss-agent" / "configs" / "config.yml": AGENT_CONFIG,
        profile / "compose.yml": PROFILE_COMPOSE,
        profile
        / "vss-behavior-analytics"
        / "configs"
        / "vss-behavior-analytics-kafka-config.json": json.dumps(
            {
                "coordinateReferenceSystem": {"roadNetwork": {"graph": {"osmType": "drive"}}},
                "app": [{"name": "mapMatchingClasses", "value": '["Car","Vehicle"]'}],
            }
        ),
        docker / "services" / "infra" / "haproxy" / "haproxy.cfg.template": HAPROXY,
        docker
        / "services"
        / "infra"
        / "elk"
        / "logstash"
        / "pipelines"
        / "kafka"
        / "mdx-logstash.conf": LOGSTASH,
        data / "vss" / "behavior-analytics-graph.json": json.dumps(
            {"osmLoadMethod": "from_polygon"}
        ),
        data / "vss" / "osm-cache" / "polygon.json": "{}",
    }
    files.update({data / "videos" / f"cam{n}.mp4": f"video {n}" for n in range(1, 5)})
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (profile / "smc-app").mkdir()
    return vss, data, docker, profile


def run(vss, data, url=URL):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(vss), str(data), url], capture_output=True, text=True
    )


def test_edits_land_once(tree):
    vss, data, docker, profile = tree
    first = run(vss, data)
    assert first.returncode == 0, first.stderr
    assert first.stdout.count("changed") == 8 and "unchanged" not in first.stdout
    second = run(vss, data)
    assert second.returncode == 0, second.stderr
    assert second.stdout.count("unchanged") == 8

    assert (
        docker / "vss-nvstreamer-video-dir" / "videos" / "dev-profile-alerts" / "cam4.mp4"
    ).read_text() == "video 4"
    assert (profile / "vss-behavior-analytics" / "osm-cache" / "polygon.json").is_file()
    config = json.loads(
        (
            profile
            / "vss-behavior-analytics"
            / "configs"
            / "vss-behavior-analytics-kafka-config.json"
        ).read_text()
    )
    assert (
        config["coordinateReferenceSystem"]["roadNetwork"]["graph"]["osmLoadMethod"]
        == "from_polygon"
    )
    assert json.loads(config["app"][0]["value"]) == ["Car", "Vehicle", "Person", "two_wheeler"]
    logstash = (
        docker
        / "services"
        / "infra"
        / "elk"
        / "logstash"
        / "pipelines"
        / "kafka"
        / "mdx-logstash.conf"
    )
    assert logstash.read_text().count("\t\treceive_buffer_bytes => 8388608\n") == 2
    compose = (profile / "compose.yml").read_text()
    cache = "/vss-behavior-analytics/osm-cache:/behavior-analytics/cache"
    assert (
        f"    volumes:\n      - $VSS_APPS_DIR/developer-profiles/dev-profile-alerts{cache}\n"
        in compose
    )
    haproxy = (docker / "services" / "infra" / "haproxy" / "haproxy.cfg.template").read_text()
    assert "    timeout client 2100s\n    timeout server 2100s\n" in haproxy
    assert "    timeout server 3600s\n" in haproxy
    assert haproxy.index("backend bk_terasim_files") < haproxy.index("frontend fe_http")
    assert haproxy.index("acl p_terasim path_beg /terasim/") < haproxy.index("acl p_phoenix")


def test_agent_config(tree):
    vss, data, _, profile = tree
    assert run(vss, data).returncode == 0
    path = profile / "vss-agent" / "configs" / "config.yml"
    text = path.read_text()
    assert path.with_name("config.yml.bak").read_text() == AGENT_CONFIG
    assert f"      url: {URL}\n    tool_call_timeout: 1800\n" in text
    assert text.index("  terasim_mcp:") < text.index("\nfunctions:")
    assert "  - vst_picture_url\n  - terasim_mcp.list_intersections\n" in text
    assert (
        text.index("    7. For direct")
        < text.index("    8. For SIGNAL TIMING")
        < text.index("    SIGNAL TIMING OPTIMIZATION (TeraSim):")
        < text.index("    GET PLACES TOOL:")
    )
    assert (
        text.index("    3. Present information")
        < text.index("    4. If a tool result begins")
        < text.index("    Example for sensor query:")
    )
    assert "  max_iterations: 25\n" in text and "  planning_enabled: false\n" in text

    # A new address updates the existing tool group and nothing else.
    again = run(vss, data, "http://10.0.0.6:9911/mcp")
    assert again.returncode == 0 and "changed    VSS agent config.yml" in again.stdout
    assert path.read_text() == text.replace(URL, "http://10.0.0.6:9911/mcp")


def test_rejects_loopback_and_missing_overlay(tree):
    vss, data, _, profile = tree
    assert run(vss, data, "http://127.0.0.1:9911/mcp").returncode != 0
    (profile / "smc-app").rmdir()
    result = run(vss, data)
    assert result.returncode != 0 and "smart-cities overlay" in result.stderr
