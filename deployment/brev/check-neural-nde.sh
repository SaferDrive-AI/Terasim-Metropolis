#!/usr/bin/env bash
# Neural NDE check of a GPU host, independent of the running TeraSim stack.
#
# Starts one throw-away container of the Neural NDE image on GPU 0 (service only, embedded
# Redis, port 127.0.0.1:18100), with the checkpoint and the demo corridor's map package
# (demo-map-package/, which carries its Neural NDE map artifacts) mounted read-only. Then:
#   1. the service's runtime check reports nothing missing;
#   2. a synchronous optimization of the demo corridor with traffic_model="cnde" runs every
#      candidate under Neural NDE and is answered and listed with traffic_model "cnde";
#   3. the same request without traffic_model runs under SUMO and names no traffic model.
# Neural NDE fast mode, as compose.brev.yml sets it. The script fails unless the check prints
# PASS. The container is removed at the end. About 5 minutes, depending on the GPU.
#
#   ./check-neural-nde.sh <path of the checkpoint> [image]
#   image: default terasim-metropolis:0.2.0-open-nnde (apps/deploy/city/Dockerfile.open-nnde, MTR
#   checkpoint); the Neural NDE image built from Dockerfile.cnde works the same way.
# Environment (optional): CNDE_GPU (0), CHECK_PORT (18100).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKPOINT="$(realpath "${1:?usage: $0 <path of the checkpoint> [image]}")"
IMAGE="${2:-terasim-metropolis:0.2.0-open-nnde}"
GPU="${CNDE_GPU:-0}"
PORT="${CHECK_PORT:-18100}"
NAME="terasim-neural-nde-check"

[[ -f "$CHECKPOINT" ]] || { echo "checkpoint not found: $CHECKPOINT" >&2; exit 1; }
docker rm -f "$NAME" >/dev/null 2>&1 || true
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

docker run -d --name "$NAME" --gpus "device=$GPU" -p "127.0.0.1:${PORT}:8000" \
  -v "$CHECKPOINT:/app/TeraSim/packages/cnde/assets/nn_weights.pth:ro" \
  -v "$HERE/demo-map-package:/data/input/cnde_demo:ro" \
  -v "$HERE/check:/config:ro" \
  -e TERASIM_CITY_SCHEDULED_CONFIG=/config/scheduled_config.check.yaml \
  -e TERASIM_CITY_OSM_DOWNLOAD=0 \
  -e TERASIM_CITY_MAX_CONCURRENCY=8 \
  -e TERASIM_CITY_CNDE_MAX_WORKERS=2 \
  -e TERASIM_CITY_CNDE_MAX_TRIALS=2 \
  -e TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY=1 \
  -e OMP_NUM_THREADS=2 -e MKL_NUM_THREADS=2 -e OPENBLAS_NUM_THREADS=2 \
  "$IMAGE" >/dev/null

docker exec -i "$NAME" python -u - <<'PY' | tee /dev/stderr | grep -qx PASS
import json, sys, time, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone

BASE = "http://127.0.0.1:8000"
CORRIDOR = "neural_nde_check"
FACTS = "Traffic model: CNDE"


def call(method, path, body=None, timeout=3600):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return reply.status, json.loads(reply.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:500]


for _ in range(120):
    try:
        if call("GET", "/openapi.json", timeout=5)[0] == 200:
            break
    except OSError:
        pass
    time.sleep(2)
else:
    sys.exit("FAIL: the service did not start")

from terasim_city.factories.cnde_behavior import missing_cnde_runtime
from terasim_city.maps.cnde_artifacts import missing_cnde_map_artifacts

missing = missing_cnde_runtime() + missing_cnde_map_artifacts("/data/input/cnde_demo/base.sumocfg")
print("1. runtime and map artifacts:", missing or "nothing missing")
if missing:
    sys.exit("FAIL: " + "; ".join(missing))

snapshot = json.load(open("/config/demo_snapshot.json"))
snapshot["timestamp_start"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime(
    "%Y-%m-%dT%H:%M:%SZ")
status, body = call("POST", "/v1/city/snapshots", snapshot)
if status != 202:
    sys.exit(f"FAIL: snapshot answered {status}: {body}")

ends = {i["intersection_id"]: {"intersection_id": i["intersection_id"], "lat": i["lat"],
                               "lon": i["lon"], "name": "demo " + i["intersection_id"]}
        for i in snapshot["intersections"]}
request = {"type": "corridor", "corridor_id": CORRIDOR,
           "corridor": {"from": ends["i1"], "to": ends["i3"]}}
results = {}
for label, extra in (("cnde", {"traffic_model": "cnde"}), ("default", {})):
    started = time.monotonic()
    status, body = call("POST", "/v1/city/optimize", {**request, **extra})
    seconds = round(time.monotonic() - started)
    if status != 200:
        sys.exit(f"FAIL: {label} request answered {status}: {body}")
    kpis = body["kpis"]
    delays = [kpis[side]["metrics"]["control_delay_s_per_veh"] for side in ("baseline", "optimized")]
    results[label] = body
    print(f"{'2' if label == 'cnde' else '3'}. {label} request: {seconds} s, traffic_model="
          f"{body.get('traffic_model')}, control delay {delays[0]:.2f} -> {delays[1]:.2f} s/veh")
status, runs = call("GET", f"/v1/city/optimizations?corridor_id={CORRIDOR}&limit=5")
listed = [(run.get("origin"), run.get("traffic_model")) for run in runs]
print("   run listing (newest first):", listed)
ok = (results["cnde"].get("traffic_model") == "cnde" and "traffic_model" not in results["default"]
      and listed[:2] == [("direct", "sumo"), ("direct", "cnde")])
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
PY
