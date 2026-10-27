"""Incident mode demo: a stalled-vehicle report from video analytics triggers optimization NOW.

Starts a local terasim-service with the corridor config loaded but the cadence
scheduler OFF (proving incident mode is independent of the periodic loop),
pushes the six 10-minute TMC snapshots to /v1/city/snapshots (timestamps
rewritten to the last hour), then POSTs the unified optimization request
(corridor_id + inline incident) to /v1/city/optimizations: the service
aggregates the buffered demand, places the stall on the arterial, and
immediately submits an expedited optimization run with trigger="incident".
The demo polls the run to completion and verifies the best trial's kpi.json
actually simulated the incident (diagnostics.incident.present with the mapped
arterial edge).

Run from the repo root:  python examples/corridor_demo/demo_incident.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx

from demo_on_demand import DEMO_DIR, print_nema, print_result, start_service, stop_service

# ~40 m west of J2 along the E-W arterial (1 deg lon ~= 88.6 km at lat 37.3).
# Verified empirically: this offset maps the stall onto the J1->J2 arterial
# edge (e.g. "E12" reversed edge "-E12" family) rather than off-network.
STALL_LON_OFFSET_DEG = -0.00045


def _stall_near_i2() -> dict:
    """One stalled vehicle on the arterial, ~40 m west of intersection i2 (J2)."""
    tmc = json.loads((DEMO_DIR / "demo_tmc.json").read_text(encoding="utf-8"))
    i2 = next(i for i in tmc["intersections"] if i["intersection_id"] == "i2")
    return {
        "latitude": float(i2["lat"]),
        "longitude": float(i2["lon"]) + STALL_LON_OFFSET_DEG,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    output_root = Path(tempfile.mkdtemp(prefix="terasim_city_incident_"))
    base = f"http://127.0.0.1:{args.port}"
    print(
        f"[demo] starting terasim-service with incident API "
        f"(scheduler OFF, window 2h, output {output_root})"
    )
    proc = start_service(
        args.port,
        output_root,
        extra_env={
            # Corridor config alone enables corridor_id requests on
            # /v1/city/optimizations; the cadence loop stays off — incident
            # mode must not depend on it.
            "TERASIM_CITY_SCHEDULER_ENABLED": "0",
            "TERASIM_CITY_SCHEDULED_CONFIG": str(DEMO_DIR / "scheduled_config.yaml"),
            "TERASIM_CITY_WINDOW_S": "7200",
        },
    )
    try:
        snapshots = sorted((DEMO_DIR / "snapshots").glob("snap_*.json"))
        # The ingest contract is UTC: emit tz-aware UTC timestamps so the
        # isoformat carries the +00:00 offset explicitly.
        now = dt.datetime.now(dt.timezone.utc)
        print(f"[demo] TMC pushes {len(snapshots)} x 10-minute snapshots ...")
        for index, path in enumerate(snapshots):
            payload = json.loads(path.read_text(encoding="utf-8"))
            stamp = now - dt.timedelta(minutes=10 * (len(snapshots) - index))
            payload["timestamp_start"] = stamp.isoformat(timespec="seconds")
            reply = httpx.post(f"{base}/v1/city/snapshots", json=payload, timeout=10.0)
            if reply.status_code != 202:
                raise RuntimeError(f"snapshot push failed: {reply.status_code} {reply.text}")
            body = reply.json()
            print(
                f"[demo]   pushed {path.name} ({payload['timestamp_start']}) "
                f"-> buffered {body['buffered']}"
            )

        stall = _stall_near_i2()
        print(
            f"[demo] video analytics report a stalled vehicle at "
            f"lat={stall['latitude']:.6f} lon={stall['longitude']:.6f} "
            f"(~40 m west of J2)"
        )
        print(
            "[demo] POST /v1/city/optimizations with corridor_id + inline "
            "incident (bypasses cadence and agent call)"
        )
        reply = httpx.post(
            f"{base}/v1/city/optimizations",
            json={
                "corridor_id": "demo_arterial",
                "incident": {"stalled_vehicles": [stall]},
            },
            timeout=30.0,
        )
        if reply.status_code != 202:
            raise RuntimeError(f"incident submit failed: {reply.status_code} {reply.text}")
        run_id = reply.json()["run_id"]
        print(
            f"[demo] accepted run_id={run_id} "
            f"(trigger=incident, expedited via emergency slots)"
        )

        status = {}
        deadline = time.time() + 900
        while time.time() < deadline:
            status = httpx.get(
                f"{base}/v1/city/optimizations/{run_id}/status", timeout=10.0
            ).json()
            progress = status.get("progress") or {}
            best = status.get("best_score")
            best_text = f"{best:.4f}" if isinstance(best, (int, float)) else "-"
            print(
                f"[demo] status={status['status']:<9} "
                f"progress {progress.get('done', 0)}/{progress.get('total', 0)} "
                f"best={best_text}"
            )
            if status["status"] in {"completed", "failed"}:
                break
            time.sleep(3)
        if status.get("status") != "completed":
            raise RuntimeError(f"incident run did not complete: {status}")

        result = httpx.get(
            f"{base}/v1/city/optimizations/{run_id}/result", timeout=10.0
        ).json()
        print(f"\n[demo] optimization ran on the aggregated hour: {result['config']['tmc_json']}")
        print(f"[demo] with stall data: {result['config']['stall_data']}")
        print_result(result)
        nema = httpx.get(
            f"{base}/v1/city/optimizations/{run_id}/nema", timeout=10.0
        ).json()
        print_nema(nema)

        # Empirical proof the incident was actually in the winning simulation:
        # the best trial's kpi.json must carry the placed stall edge.
        kpi_path = Path(result["best"]["trial_dir"]) / "kpi.json"
        kpi = json.loads(kpi_path.read_text(encoding="utf-8"))
        incident = kpi["diagnostics"]["incident"]
        print(f"\n[demo] best-trial kpi diagnostics.incident.present = {incident['present']}")
        if not incident["present"] or not incident["items"]:
            raise RuntimeError(f"stall was not placed in the best trial: {incident}")
        item = incident["items"][0]
        if not item.get("edge"):
            raise RuntimeError(f"stall mapped to an empty edge: {incident}")
        print(
            f"[demo] incident injected at edge={item['edge']} "
            f"lat={item['lat']:.6f} lon={item['lon']:.6f} (lane {item['lane']})"
        )
        print("\n[demo] incident mode OK")
        return 0
    finally:
        stop_service(proc)


if __name__ == "__main__":
    sys.exit(main())
