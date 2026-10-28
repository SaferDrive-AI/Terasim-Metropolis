#!/usr/bin/env bash
# One-shot smoke test of the VSS-facing flow against the image:
#   start container -> register the baked demo map through the asset endpoint
#   -> post a 10-minute TMC snapshot -> synchronously optimize a corridor
#   -> verify NEMA + KPI output (optimized no worse than baseline) -> clean up.
#
# The image bakes the synthetic demo corridor's map at /data/input/map
# (demo.sumocfg: three signals, J1 to J3, about 300 m apart). The script registers
# it via POST /v1/city/maps, the same path an operator uses for any map, posts one
# snapshot with the counts of all three intersections and optimizes the corridor
# from i1 (J1) to i3 (J3) of demo_arterial. Fully offline: the registered map covers
# the request, so no OpenStreetMap download is triggered.
#
#   IMAGE=terasim-metropolis:0.2.0 bash apps/deploy/city/smoke.sh
set -euo pipefail

IMAGE="${IMAGE:-terasim-metropolis:0.2.0}"
PORT="${PORT:-8000}"
NAME="terasim-metropolis-smoke"

docker rm -f "$NAME" >/dev/null 2>&1 || true
cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d -p "${PORT}:8000" --name "$NAME" "$IMAGE" >/dev/null
echo "started ${NAME} from ${IMAGE} on :${PORT}"

BASE="http://localhost:${PORT}"
python3 - "$BASE" <<'PY'
import datetime, json, sys, time, urllib.error, urllib.request
base = sys.argv[1]
def http(m, u, p=None):
    d = json.dumps(p).encode() if p is not None else None
    h = {"Content-Type": "application/json"} if d else {}
    r = urllib.request.Request(u, data=d, headers=h, method=m)
    try:
        with urllib.request.urlopen(r, timeout=1800) as x:
            return x.status, json.loads(x.read())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read())
        except Exception: return e.code, {}
for _ in range(120):
    try:
        urllib.request.urlopen(base + "/openapi.json", timeout=2); break
    except Exception: time.sleep(1)
else:
    print("service not ready"); sys.exit(2)

# Register the baked demo map through the asset endpoint.
st, registered = http("POST", base + "/v1/city/maps", {"path": "/data/input/map/demo.sumocfg"})
print("MAP ->", st, registered)
if st != 201: sys.exit(1)

# 10-minute counts by direction of travel: an E-W arterial with lighter cross streets,
# enough demand that a degraded optimization would visibly worsen the KPIs.
def counts(eb, wb, nb, sb):
    return {heading: {"Left": left, "Straight": straight, "Right": right}
            for heading, (left, straight, right) in
            (("East", eb), ("West", wb), ("North", nb), ("South", sb))}
i1 = {"intersection_id": "i1", "name": "Arterial Ave & 1st Cross St", "lat": 37.335, "lon": -121.90339}
i2 = {"intersection_id": "i2", "name": "Arterial Ave & 2nd Cross St", "lat": 37.335, "lon": -121.9}
i3 = {"intersection_id": "i3", "name": "Arterial Ave & 3rd Cross St", "lat": 37.335, "lon": -121.89661}
# Every signal between the corridor's two ends needs counts too.
snapshot = {
    "corridor_id": "demo_arterial",
    "observation_interval": "10m",
    "timestamp_start": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "intersections": [
        {**i1, "counts": counts((10, 93, 15), (8, 67, 13), (8, 20, 12), (7, 18, 10))},
        {**i2, "counts": counts((12, 98, 13), (9, 72, 12), (8, 23, 13), (8, 22, 11))},
        {**i3, "counts": counts((9, 87, 17), (10, 63, 15), (7, 17, 10), (8, 25, 12))},
    ],
}
st, buffered = http("POST", base + "/v1/city/snapshots", snapshot)
print("SNAPSHOT ->", st, buffered)
if st != 202: sys.exit(1)
request = {
    "type": "corridor",
    "corridor_id": "demo_arterial",
    "corridor": {"from": i1, "to": i3},
    "cost": {"weights": {"control_delay": 0.7, "stops": 0.3}},
}
st, result = http("POST", base + "/v1/city/optimize", request)
print("OPTIMIZE ->", st)
kpis = (result or {}).get("kpis") or {}
baseline = (kpis.get("baseline") or {}).get("cost")
optimized = (kpis.get("optimized") or {}).get("cost")
ok = (
    st == 200
    # the contract keys, plus the two the service adds only when they apply
    and set(result or {}) - {"unserved_movements", "config"} == {"nema_plan", "kpis"}
    and set(kpis) == {"cost_weights", "baseline", "optimized"}
    and "run_id" not in result
    # The winner is the argmin over baseline + trials: worse-than-baseline
    # output means the selection itself broke.
    and isinstance(baseline, (int, float))
    and isinstance(optimized, (int, float))
    and optimized <= baseline + 1e-9
)
print(f"KPI baseline={baseline} optimized={optimized}")
print(">>>", "DIRECT NEMA + KPI OK" if ok else json.dumps(result, indent=2))
sys.exit(0 if ok else 1)
PY
