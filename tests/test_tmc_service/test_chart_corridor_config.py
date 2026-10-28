"""terasim-phase1 chart: the corridor files come from values or a ConfigMap you manage.

* Without corridorConfig the chart mounts the files it ships (configs/: the demo corridor).
* corridorConfig.metadataJson / scheduledConfigYaml replace them (each on its own); the
  service and tmc pods carry a checksum of the two, so a change restarts them.
* corridorConfig.existingConfigMap mounts the operator's ConfigMap and renders none.
* What the chart mounts is what the tmc collector and the service accept at start-up
  (their own loaders), including the values file camcfg.py generates.
* Unusable content fails ``helm template`` with the setting's name.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART_DIR = REPO_ROOT / "apps" / "deploy" / "city" / "helm" / "terasim-phase1"
CAMCFG_DIR = REPO_ROOT / "apps" / "deploy" / "city" / "camera-config"
REQUIRED = (
    "--set",
    "tmc.vssBehaviorUrl=http://vss-video-analytics-api.vss.svc:8081",
    "--set",
    "global.externalHost=terasim.apps.example.test",
)


def _helm(*args: str) -> subprocess.CompletedProcess:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")
    return subprocess.run([helm, *args], capture_output=True, text=True)


def _render(*sets: str) -> list[dict]:
    result = _helm("template", "t", str(CHART_DIR), *REQUIRED, *sets)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _configmaps(docs: list[dict]) -> list[dict]:
    return [doc for doc in docs if doc["kind"] == "ConfigMap"]


def _pod(docs: list[dict], component: str) -> dict:
    (deployment,) = [
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith(f"-{component}")
    ]
    return deployment["spec"]["template"]


def _mounted_configmap(pod: dict) -> str:
    (volume,) = [v for v in pod["spec"]["volumes"] if v["name"] == "config"]
    return volume["configMap"]["name"]


def _checksum(pod: dict) -> str:
    return pod["metadata"]["annotations"]["checksum/corridor-config"]


def test_default_mounts_the_shipped_files():
    docs = _render()
    (configmap,) = _configmaps(docs)
    assert configmap["metadata"]["name"] == "t-terasim-phase1-config"
    assert json.loads(configmap["data"]["Metadata.json"]) == json.loads(
        (CHART_DIR / "configs" / "Metadata.json").read_text(encoding="utf-8")
    )
    assert configmap["data"]["scheduled_config.yaml"].strip() == (
        (CHART_DIR / "configs" / "scheduled_config.yaml").read_text(encoding="utf-8").strip()
    )
    service, tmc = _pod(docs, "service"), _pod(docs, "tmc")
    assert _mounted_configmap(service) == _mounted_configmap(tmc) == "t-terasim-phase1-config"
    assert _checksum(service) == _checksum(tmc)


def test_inline_values_replace_the_files_and_change_the_checksum(tmp_path):
    spec = json.loads((CHART_DIR / "configs" / "Metadata.json").read_text(encoding="utf-8"))
    spec["intersections"][0]["Assignments"]["South"] = "i1-cam9"
    spec_file = tmp_path / "Metadata.json"
    spec_file.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    default_checksum = _checksum(_pod(_render(), "tmc"))

    docs = _render("--set-file", f"corridorConfig.metadataJson={spec_file}")
    (configmap,) = _configmaps(docs)
    assert json.loads(configmap["data"]["Metadata.json"]) == spec
    # the scheduled configuration was not given: still the shipped one
    assert yaml.safe_load(configmap["data"]["scheduled_config.yaml"]) == yaml.safe_load(
        (CHART_DIR / "configs" / "scheduled_config.yaml").read_text(encoding="utf-8")
    )
    assert _checksum(_pod(docs, "tmc")) == _checksum(_pod(docs, "service")) != default_checksum


def test_existing_configmap_is_mounted_and_none_is_rendered():
    docs = _render("--set", "corridorConfig.existingConfigMap=my-corridor")
    assert _configmaps(docs) == []
    assert _mounted_configmap(_pod(docs, "service")) == "my-corridor"
    assert _mounted_configmap(_pod(docs, "tmc")) == "my-corridor"


def test_generated_values_render_files_the_collector_and_the_service_accept(tmp_path):
    """camcfg.py's values-corridor.yaml -> chart ConfigMap -> the products' own loaders."""
    pytest.importorskip("sumolib")
    from terasim_service.city.scheduler import load_scheduled_config
    from terasim_tmc_service.spec import CorridorSpec

    out = tmp_path / "out"
    generated = subprocess.run(
        [
            sys.executable,
            str(CAMCFG_DIR / "camcfg.py"),
            "generate",
            str(CAMCFG_DIR / "examples" / "demo_arterial.yaml"),
            "-o",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert generated.returncode == 0, generated.stderr
    docs = _render("-f", str(out / "terasim" / "values-corridor.yaml"))
    (configmap,) = _configmaps(docs)
    # the literal block starts with an empty line, as the shipped files always did
    assert (
        configmap["data"]["Metadata.json"].strip()
        == (out / "terasim" / "Metadata.json").read_text(encoding="utf-8").strip()
    )

    spec_file = tmp_path / "Metadata.json"
    spec_file.write_text(configmap["data"]["Metadata.json"], encoding="utf-8")
    spec = CorridorSpec.from_json(spec_file)
    assert spec.corridor_id == "demo_arterial"
    assert spec.ids() == ["i1", "i2", "i3"]
    assert sorted(spec.get("i1").sensors) == ["i1-cam1", "i1-cam2", "i1-cam3", "i1-cam4"]

    scheduled_file = tmp_path / "scheduled_config.yaml"
    scheduled_file.write_text(configmap["data"]["scheduled_config.yaml"], encoding="utf-8")
    corridors = load_scheduled_config(scheduled_file)
    shipped = load_scheduled_config(CHART_DIR / "configs" / "scheduled_config.yaml")
    for key in ("base_sumocfg", "corridor", "sim_end", "max_trials", "n_workers", "patience"):
        assert corridors["demo_arterial"][key] == shipped["demo_arterial"][key], key


def test_the_32_camera_corridor_renders_from_its_generated_values(tmp_path):
    """Eight signals and 32 cameras through the same values file: no new chart, no new image."""
    pytest.importorskip("sumolib")
    if shutil.which("netconvert") is None:
        pytest.skip("netconvert is needed to convert the example's OpenStreetMap extract")
    out = tmp_path / "out"
    generated = subprocess.run(
        [
            sys.executable,
            str(CAMCFG_DIR / "camcfg.py"),
            "generate",
            str(CAMCFG_DIR / "examples" / "arterial_32.yaml"),
            "-o",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert generated.returncode == 0, generated.stderr
    default_checksum = _checksum(_pod(_render(), "service"))
    docs = _render("-f", str(out / "terasim" / "values-corridor.yaml"))
    (configmap,) = _configmaps(docs)
    spec = json.loads(configmap["data"]["Metadata.json"])
    assert spec["corridor_id"] == "arterial_blvd" and len(spec["intersections"]) == 8
    cameras = {cam for it in spec["intersections"] for cam in it["Assignments"].values()}
    assert len(cameras) == 26  # the counting cameras of the 32
    scheduled = yaml.safe_load(configmap["data"]["scheduled_config.yaml"])["corridors"]
    assert len(scheduled["arterial_blvd"]["corridor"]["tls_ids"]) == 8
    assert _checksum(_pod(docs, "service")) == _checksum(_pod(docs, "tmc")) != default_checksum


_BAD_SPEC_NO_ID = {"observation_interval": "10m", "intersections": []}
_BAD_SPEC_EMPTY = {"corridor_id": "c", "observation_interval": "10m", "intersections": []}
_BAD_SPEC_UNCOVERED = {
    "corridor_id": "c",
    "observation_interval": "10m",
    "intersections": [
        {
            "intersection_id": "a",
            "lat": 1,
            "lon": 2,
            "Headings": {"1-2": "South"},
            "Movements": {"South - South": "Straight"},
            "Assignments": {"South": "cam1", "North": "cam2"},
        }
    ],
}
_BAD_SPEC_HEADINGS = {
    "corridor_id": "c",
    "observation_interval": "10m",
    "intersections": [
        {
            "intersection_id": "a",
            "lat": 1,
            "lon": 2,
            "Headings": {},
            "Movements": {"South - South": "Straight"},
            "Assignments": {"South": "cam1"},
        }
    ],
}


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {"existingConfigMap": "x", "metadataJson": "{}"},
            "set either existingConfigMap or metadataJson",
        ),
        ({"metadataJson": "not json"}, "corridorConfig.metadataJson is not a JSON object"),
        (
            {"metadataJson": json.dumps(_BAD_SPEC_NO_ID)},
            "corridorConfig.metadataJson needs a non-empty corridor_id",
        ),
        ({"metadataJson": json.dumps(_BAD_SPEC_EMPTY)}, "needs a non-empty intersections list"),
        (
            {"metadataJson": json.dumps(_BAD_SPEC_HEADINGS)},
            "intersection a: Headings must be a non-empty object",
        ),
        (
            {"metadataJson": json.dumps(_BAD_SPEC_UNCOVERED)},
            "intersection a: Assignments heading North has no Movements entry",
        ),
        ({"metadataJson": ["a", "list"]}, "corridorConfig.metadataJson must be the file's text"),
        (
            {"scheduledConfigYaml": "corridors: {}"},
            "corridorConfig.scheduledConfigYaml needs a non-empty corridors mapping",
        ),
        (
            {"scheduledConfigYaml": "corridors: {c: {corridor: {tls_ids: [a]}}}"},
            "corridor c is missing base_sumocfg",
        ),
    ],
    ids=[
        "both",
        "not_json",
        "no_corridor_id",
        "no_intersections",
        "empty_headings",
        "assignment_without_movements",
        "not_text",
        "no_corridors",
        "no_map_path",
    ],
)
def test_unusable_corridor_files_fail_with_the_setting_named(tmp_path, values, message):
    values_file = tmp_path / "values.yaml"
    values_file.write_text(yaml.safe_dump({"corridorConfig": values}), encoding="utf-8")
    result = _helm("template", "t", str(CHART_DIR), *REQUIRED, "-f", str(values_file))
    assert result.returncode != 0
    assert message in result.stderr


def test_chart_version_and_corridor_defaults():
    chart = yaml.safe_load((CHART_DIR / "Chart.yaml").read_text(encoding="utf-8"))
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    assert chart["version"] == "0.6.1"
    assert chart["appVersion"] == values["image"]["tag"] == "0.2.0"
    assert values["corridorConfig"] == {
        "metadataJson": "",
        "scheduledConfigYaml": "",
        "existingConfigMap": "",
    }
