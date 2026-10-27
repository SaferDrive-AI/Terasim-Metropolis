"""Deployment assets must stay consistent with the sources the tests exercise.

* The deployed corridor spec is byte-identical to the fixture the tests use, and the
  corridor configuration names that spec's corridor on the map the image bakes.
* The image bakes exactly the four demo-map files and the five demo-scenario files
  (no scripts, READMEs or snapshots under /data/input) and the release's license texts,
  makes them read-only, and carries the self-tests and the behavior stub, whose map
  and scenario paths name only files the image bakes.
* Compose, the Helm chart and its default values name the same image tag.
* Every file the chart embeds (``.Files.Get``) is in the tree, tracked and not
  git-ignored (``helm package`` from a checkout would ship it empty otherwise), and
  the chart renders its scheduler / simulated-metrics / audit-log / OpenStreetMap
  download settings into the service env.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CITY_DEPLOY = REPO_ROOT / "apps" / "deploy" / "city"
DEPLOYED = CITY_DEPLOY / "phase1" / "Metadata.json"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "tmc_service" / "demo_arterial_metadata.json"
CORRIDOR_CONFIG = CITY_DEPLOY / "phase1" / "scheduled_config.yaml"
DOCKERFILE = CITY_DEPLOY / "Dockerfile"
SCENARIO_SELF_TEST = CITY_DEPLOY / "phase1" / "scenario_e2e.py"
SCENARIO_SELF_TEST_IN_IMAGE = "apps/deploy/city/phase1/scenario_e2e.py"
STACK_SELF_TEST = CITY_DEPLOY / "phase1" / "phase1_e2e.py"
BEHAVIOR_STUB_IN_IMAGE = "apps/deploy/city/phase1/stub_behavior_server.py"
COMPOSE = CITY_DEPLOY / "phase1" / "compose.yml"
CHART_DIR = CITY_DEPLOY / "helm" / "terasim-phase1"
CHART = CHART_DIR / "Chart.yaml"
CHART_VALUES = CHART_DIR / "values.yaml"
#: The values ``helm template`` / ``lint`` require (see the chart's validate helper).
CHART_REQUIRED_SETS = (
    "--set", "tmc.vssBehaviorUrl=http://vss-video-analytics-api.vss.svc:8081",
    "--set", "global.externalHost=terasim.apps.example.test",
)
DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
MAP_IN_IMAGE = "/data/input/map"
SCENARIO_IN_IMAGE = "/data/input/demo_scenario"
INPUT_ROOT_IN_IMAGE = "/data/input"
LICENSES_IN_IMAGE = "/licenses"
SMOKE = CITY_DEPLOY / "smoke.sh"
# The demo corridor's map as the image bakes it: the sumocfg and the three files it
# names (an empty route file: the demand comes from the TMC snapshots).
MAP_FILES = ("demo.sumocfg", "demo.net.xml", "demo.tls.xml", "demo.rou.xml")
# The demo scenario as the image bakes it: the sumocfg, the three files it names and
# the placeholder TMC file. Nothing else from examples/corridor_demo (dev scripts,
# README, snapshots) may reach the delivered image.
SCENARIO_FILES = (
    "demo.net.xml",
    "demo.tls.xml",
    "demo_routes.rou.xml",
    "demo_scenario.sumocfg",
    "placeholder_tmc.json",
)
LICENSE_FILES = ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md")
# License texts NOTICE names for individual files, under the same relative path as in the
# repository.
LICENSE_TEXTS = ("LICENSES/EPL-2.0.txt",)


def _dockerfile_instructions() -> list[list[str]]:
    """Every Dockerfile instruction as its words, with ``\\`` line continuations joined."""
    instructions: list[list[str]] = []
    pending = ""
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        instructions.append((pending + stripped).split())
        pending = ""
    if pending:
        instructions.append(pending.split())
    return instructions


def _dockerfile_copies() -> list[tuple[str, str]]:
    """``(source, destination)`` of every COPY from the build context (not from another stage).

    A COPY with several sources yields one pair per source.
    """
    copies = []
    for words in _dockerfile_instructions():
        if words[:1] != ["COPY"] or any(word.startswith("--from=") for word in words):
            continue
        arguments = [word for word in words[1:] if not word.startswith("--")]
        assert len(arguments) >= 2, f"unparsed COPY: {' '.join(words)}"
        copies.extend((source, arguments[-1]) for source in arguments[:-1])
    return copies


def _under(destination: str, root: str) -> bool:
    return destination.rstrip("/") == root or destination.startswith(root + "/")


def _baked_input_paths() -> list[Path]:
    """Every build-context path (files and directories) the Dockerfile copies under /data/input."""
    paths: list[Path] = []
    for source, destination in _dockerfile_copies():
        if not _under(destination, INPUT_ROOT_IN_IMAGE):
            continue
        matches = sorted(REPO_ROOT.glob(source)) if any(c in source for c in "*?[") else [REPO_ROOT / source]
        assert matches and all(match.exists() for match in matches), f"COPY source {source} does not exist"
        for match in matches:
            paths.append(match)
            if match.is_dir():
                paths.extend(sorted(match.rglob("*")))
    return paths


def _sumocfg_files(path: Path) -> set[str]:
    """The files a sumocfg names (net, routes, additionals), as written in it."""
    root = ET.parse(path).getroot()
    return {
        name.strip()
        for tag in ("net-file", "route-files", "additional-files")
        for element in root.iter(tag)
        for name in element.get("value", "").split(",")
        if name.strip()
    }


def _final_self_check() -> str:
    """The last RUN of the Dockerfile: the runtime self-check, as one line."""
    runs = [words for words in _dockerfile_instructions() if words[:1] == ["RUN"]]
    return " ".join(runs[-1])


def test_phase1_metadata_matches_test_fixture():
    assert DEPLOYED.read_bytes() == FIXTURE.read_bytes()


def test_the_corridor_config_names_the_baked_map_and_the_spec_corridor():
    # Compose mounts phase1/scheduled_config.yaml, the chart embeds its own copy.
    assert (CHART_DIR / "configs" / "scheduled_config.yaml").read_bytes() == CORRIDOR_CONFIG.read_bytes()
    config = yaml.safe_load(CORRIDOR_CONFIG.read_text(encoding="utf-8"))["corridors"]
    spec = json.loads(DEPLOYED.read_text(encoding="utf-8"))
    assert set(config) == {spec["corridor_id"]} == {"demo_arterial"}
    entry = config["demo_arterial"]
    assert entry["base_sumocfg"] == f"{MAP_IN_IMAGE}/demo.sumocfg"
    assert ("examples/corridor_demo/demo.sumocfg", entry["base_sumocfg"]) in _dockerfile_copies()
    signals = {
        element.get("id") for element in ET.parse(DEMO_DIR / "demo.net.xml").getroot().iter("tlLogic")
    }
    assert set(entry["corridor"]["tls_ids"]) == signals == {"J1", "J2", "J3"}
    # the contract's 30-minute optimization run; a budget the search can finish
    assert entry["sim_end"] == 1800
    assert entry["patience"] > 10, "patience must outlast the sampler's 10 random start-up trials"
    assert 1 <= entry["n_workers"] <= 8 and entry["max_trials"] >= entry["patience"]


def test_image_bakes_the_four_demo_map_files_and_the_five_demo_scenario_files():
    copies = _dockerfile_copies()
    map_copies = sorted(
        (source, destination) for source, destination in copies if _under(destination, MAP_IN_IMAGE)
    )
    assert map_copies == sorted(
        (f"examples/corridor_demo/{name}", f"{MAP_IN_IMAGE}/{name}") for name in MAP_FILES
    )
    scenario_copies = sorted(
        (source, destination) for source, destination in copies if _under(destination, SCENARIO_IN_IMAGE)
    )
    assert scenario_copies == sorted(
        (f"examples/corridor_demo/{name}", f"{SCENARIO_IN_IMAGE}/{name}") for name in SCENARIO_FILES
    )
    for name in MAP_FILES + SCENARIO_FILES:
        assert (DEMO_DIR / name).is_file()


def test_the_baked_map_is_self_contained():
    """Every file demo.sumocfg names is baked next to it (relative paths)."""
    named = _sumocfg_files(DEMO_DIR / "demo.sumocfg")
    assert named == {"demo.net.xml", "demo.rou.xml", "demo.tls.xml"}
    assert named <= set(MAP_FILES)


def test_the_baked_scenario_is_self_contained():
    """Every file demo_scenario.sumocfg names is among the files the image bakes."""
    named = _sumocfg_files(DEMO_DIR / "demo_scenario.sumocfg")
    assert named == {"demo.net.xml", "demo_routes.rou.xml", "demo.tls.xml"}
    assert named <= set(SCENARIO_FILES)


def test_image_bakes_no_scripts_readmes_or_snapshots_under_data_input():
    offending = sorted(
        {
            path.relative_to(REPO_ROOT).as_posix()
            for path in _baked_input_paths()
            if path.suffix == ".py"
            or path.name.startswith("README")
            or "snapshots" in path.relative_to(REPO_ROOT).parts
        }
    )
    assert not offending, f"the Dockerfile copies development material under /data/input: {offending}"


def test_baked_inputs_are_made_read_only_after_the_last_input_copy():
    """uid 1001 runs in group 0, so files baked group-writable would be writable at run time."""
    instructions = _dockerfile_instructions()

    def last_copy_into(root: str) -> int:
        lines = [
            index
            for index, words in enumerate(instructions)
            if words[:1] == ["COPY"]
            and not any(word.startswith("--from=") for word in words)
            and _under([word for word in words[1:] if not word.startswith("--")][-1], root)
        ]
        assert lines, f"nothing is copied into {root}"
        return max(lines)

    for root in (INPUT_ROOT_IN_IMAGE, LICENSES_IN_IMAGE):
        chmod_lines = [
            index
            for index, words in enumerate(instructions)
            if words[:1] == ["RUN"]
            and "chmod" in words
            and "go-w" in words
            and root in words
        ]
        assert chmod_lines and max(chmod_lines) > last_copy_into(root), root


def test_image_ships_the_license_texts_and_checks_them():
    copies = _dockerfile_copies()
    for name in LICENSE_FILES:
        assert (REPO_ROOT / name).is_file(), f"{name} is missing at the repository root"
        assert (name, f"{LICENSES_IN_IMAGE}/") in copies
    check = _final_self_check()
    for name in LICENSE_FILES:
        assert f"test -f {LICENSES_IN_IMAGE}/{name}" in check
    assert ("LICENSES", f"{LICENSES_IN_IMAGE}/LICENSES") in copies
    for name in LICENSE_TEXTS:
        assert (REPO_ROOT / name).is_file(), f"{name} is missing in the repository"
        assert f"test -f {LICENSES_IN_IMAGE}/{name}" in check
    assert f"test ! -w {LICENSES_IN_IMAGE}" in check


def test_the_self_check_loads_the_baked_map():
    check = _final_self_check()
    assert f"sumo -c {MAP_IN_IMAGE}/demo.sumocfg" in check
    assert f"test ! -w {MAP_IN_IMAGE}/demo.net.xml" in check


def test_scenario_self_test_ships_in_the_image_and_names_only_files_the_image_bakes():
    assert SCENARIO_SELF_TEST.is_file()
    # Next to phase1_e2e.py: the builder stage copies the script to the same path
    # under /app/TeraSim, the image's working directory.
    assert (SCENARIO_SELF_TEST_IN_IMAGE, SCENARIO_SELF_TEST_IN_IMAGE) in _dockerfile_copies()
    named = set(re.findall(rf"{re.escape(SCENARIO_IN_IMAGE)}/([\w.\-]+)", SCENARIO_SELF_TEST.read_text(encoding="utf-8")))
    assert {"demo_scenario.sumocfg", "placeholder_tmc.json"} <= named
    for name in sorted(named):
        assert name in SCENARIO_FILES, (
            f"scenario_e2e.py names {SCENARIO_IN_IMAGE}/{name}, which the image does not bake"
        )


def test_the_stack_self_test_registers_the_baked_map():
    named = set(re.findall(rf"{re.escape(MAP_IN_IMAGE)}/([\w.\-]+)", STACK_SELF_TEST.read_text(encoding="utf-8")))
    assert named == {"demo.sumocfg"}


def test_the_behavior_stub_ships_in_the_image_and_compose_runs_it_in_the_stub_profile():
    assert (BEHAVIOR_STUB_IN_IMAGE, BEHAVIOR_STUB_IN_IMAGE) in _dockerfile_copies()
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    stub = services["vss-behavior-stub"]
    assert stub["profiles"] == ["stub"]
    assert BEHAVIOR_STUB_IN_IMAGE in stub["entrypoint"]
    # without a VSS_BEHAVIOR_URL the TMC collector pulls the stub
    assert services["terasim-tmc"]["environment"]["VSS_BEHAVIOR_URL"].endswith(
        "http://vss-behavior-stub:8081}"
    )
    assert all("profiles" not in service for name, service in services.items() if name != "vss-behavior-stub")


def test_compose_chart_and_values_name_the_same_image_tag():
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    images = {service["image"] for service in services.values()}
    assert len(images) == 1, f"one image runs every container, compose names {sorted(images)}"
    repository, _, tag = images.pop().partition(":")
    chart = yaml.safe_load(CHART.read_text(encoding="utf-8"))
    values = yaml.safe_load(CHART_VALUES.read_text(encoding="utf-8"))
    assert repository == values["image"]["repository"] == "terasim-metropolis"
    assert tag == values["image"]["tag"] == chart["appVersion"] == "0.1.0"


def test_smoke_registers_the_map_the_image_bakes_and_counts_every_corridor_signal():
    """The smoke registers the baked map (no staged copy) and its snapshot carries the
    intersections of the deployed spec at the spec's own coordinates."""
    script = SMOKE.read_text(encoding="utf-8")
    registered = re.findall(r'"path": "([^"]+)"', script)
    assert registered == [f"{MAP_IN_IMAGE}/demo.sumocfg"]
    assert ("examples/corridor_demo/demo.sumocfg", registered[0]) in _dockerfile_copies()
    assert "docker cp" not in script
    spec = json.loads(DEPLOYED.read_text(encoding="utf-8"))
    located = {
        item["intersection_id"]: (item["lat"], item["lon"]) for item in spec["intersections"]
    }
    targets = {
        intersection_id: (float(lat), float(lon))
        for intersection_id, lat, lon in re.findall(
            r'"intersection_id": "([^"]+)", "name": "[^"]+", "lat": ([-\d.]+), "lon": ([-\d.]+)', script
        )
    }
    assert targets == located
    assert f'"corridor_id": "{spec["corridor_id"]}"' in script


# --- the Helm chart: embedded config files and service settings -----------------------


def _files_the_chart_embeds() -> list[str]:
    """Every chart-relative path a template reads with ``.Files.Get``."""
    names: set[str] = set()
    for template in (CHART_DIR / "templates").rglob("*"):
        if template.is_file():
            names.update(re.findall(r'\.Files\.Get\s+"([^"]+)"', template.read_text(encoding="utf-8")))
    return sorted(names)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)


def test_the_config_files_the_chart_embeds_are_in_the_tree():
    names = _files_the_chart_embeds()
    assert {"configs/Metadata.json", "configs/scheduled_config.yaml"} <= set(names)
    for name in names:
        path = CHART_DIR / name
        assert path.is_file() and path.stat().st_size > 0, f"chart file {name} is missing or empty"
    if shutil.which("git") is None or _git("rev-parse", "--is-inside-work-tree").returncode != 0:
        pytest.skip("not a git checkout: tracking cannot be checked")
    for name in names:
        relative = str((CHART_DIR / name).relative_to(REPO_ROOT))
        assert _git("ls-files", "--error-unmatch", relative).returncode == 0, (
            f"{relative} is not tracked: a chart packaged from a checkout would embed it empty"
        )
        assert _git("check-ignore", "-q", "--no-index", relative).returncode == 1, (
            f"{relative} is matched by a .gitignore rule"
        )


def test_the_chart_ships_the_deployed_tmc_metadata():
    assert (CHART_DIR / "configs" / "Metadata.json").read_bytes() == DEPLOYED.read_bytes()


def test_scheduler_is_off_and_simulated_metrics_audit_log_and_osm_downloads_on_by_default():
    values = yaml.safe_load(CHART_VALUES.read_text(encoding="utf-8"))
    # off by default; when enabled it runs hourly, the service's own default cadence
    assert values["scheduler"] == {"enabled": False, "cadenceSeconds": 3600}
    assert values["simMetrics"] == {"enabled": True}
    assert values["audit"] == {"enabled": True}
    assert values["service"]["osmDownload"] == {"enabled": True}


def _helm(*args: str) -> subprocess.CompletedProcess:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")
    return subprocess.run([helm, *args], capture_output=True, text=True)


def _rendered(*sets: str) -> list[dict]:
    result = _helm("template", "t", str(CHART_DIR), *CHART_REQUIRED_SETS, *sets)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _service_env(docs: list[dict]) -> dict:
    (deployment,) = [
        doc for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith("-service")
    ]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    return {entry["name"]: entry.get("value") for entry in container["env"]}


@pytest.mark.parametrize(
    ("sets", "expected"),
    [
        ((), {"TERASIM_CITY_SCHEDULER_ENABLED": "false", "TERASIM_CITY_CADENCE_S": "3600",
              "TERASIM_CITY_SIM_METRICS": "1", "TERASIM_CITY_AUDIT_ENABLED": "1",
              "TERASIM_CITY_OSM_DOWNLOAD": "1"}),
        (("--set", "scheduler.enabled=true", "--set", "scheduler.cadenceSeconds=900",
          "--set", "simMetrics.enabled=false", "--set", "audit.enabled=false",
          "--set", "service.osmDownload.enabled=false"),
         {"TERASIM_CITY_SCHEDULER_ENABLED": "true", "TERASIM_CITY_CADENCE_S": "900",
          "TERASIM_CITY_SIM_METRICS": "0", "TERASIM_CITY_AUDIT_ENABLED": "0",
          "TERASIM_CITY_OSM_DOWNLOAD": "0"}),
        # A values file without service.osmDownload (written for an older chart):
        # downloads stay on.
        (("--set", "service.osmDownload=null"), {"TERASIM_CITY_OSM_DOWNLOAD": "1"}),
    ],
    ids=["defaults", "scheduler_on_metrics_audit_and_osm_downloads_off", "values_without_osm_download"],
)
def test_the_chart_renders_the_scheduler_metrics_and_audit_settings(sets, expected):
    env = _service_env(_rendered(*sets))
    assert {name: env.get(name) for name in expected} == expected
    assert env["TERASIM_CITY_SCHEDULED_CONFIG"] == "/config/scheduled_config.yaml"


def test_the_chart_configmap_carries_the_tmc_metadata():
    (configmap,) = [doc for doc in _rendered() if doc["kind"] == "ConfigMap"]
    assert json.loads(configmap["data"]["Metadata.json"]) == json.loads(
        DEPLOYED.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    ("sets", "named"),
    [
        (("--set-string", "scheduler.enabled=false"), "scheduler.enabled"),
        (("--set-string", "simMetrics.enabled=yes"), "simMetrics.enabled"),
        (("--set-string", "audit.enabled=true"), "audit.enabled"),
        (("--set-string", "service.osmDownload.enabled=false"), "service.osmDownload.enabled"),
        (("--set", "scheduler.cadenceSeconds=0"), "scheduler.cadenceSeconds"),
        (("--set-string", "scheduler.cadenceSeconds=3600"), "scheduler.cadenceSeconds"),
    ],
)
def test_the_chart_rejects_settings_of_the_wrong_kind(sets, named):
    result = _helm("template", "t", str(CHART_DIR), *CHART_REQUIRED_SETS, *sets)
    assert result.returncode != 0
    assert named in result.stderr
