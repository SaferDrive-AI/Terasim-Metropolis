"""Scheduled mode demo: TMC pushes snapshots, TeraSim optimizes on a cadence.

Starts a local terasim-service with the scheduler enabled (demo cadence 30 s),
pushes the six 10-minute TMC snapshots to /v1/city/snapshots (timestamps
rewritten to the last hour so the aggregation window catches them), waits for
the scheduler tick to auto-submit an optimization over the aggregated hour,
and prints the resulting best plan — no explicit optimization request is made.

Run from the repo root:  python examples/corridor_demo/demo_scheduled.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx

from demo_on_demand import DEMO_DIR, print_nema, print_result, start_service, stop_service

import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--cadence-s", type=int, default=30)
    args = parser.parse_args()

    output_root = Path(tempfile.mkdtemp(prefix="terasim_city_sched_"))
    base = f"http://127.0.0.1:{args.port}"
    print(
        f"[demo] starting terasim-service with scheduler "
        f"(cadence {args.cadence_s}s, window 2h, output {output_root})"
    )
    proc = start_service(
        args.port,
        output_root,
        extra_env={
            "TERASIM_CITY_SCHEDULER_ENABLED": "1",
            "TERASIM_CITY_SCHEDULED_CONFIG": str(DEMO_DIR / "scheduled_config.yaml"),
            "TERASIM_CITY_CADENCE_S": str(args.cadence_s),
            "TERASIM_CITY_WINDOW_S": "7200",
        },
    )
    try:
        # The latest-run key survives in Redis across demo runs; remember the
        # stale value so we only accept a run submitted by THIS demo's tick.
        stale_run_id = httpx.get(
            f"{base}/v1/city/scheduled/demo_arterial/latest", timeout=10.0
        ).json().get("run_id")

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

        print(f"[demo] waiting for the scheduler tick (<= {args.cadence_s}s) ...")
        run_id = None
        deadline = time.time() + args.cadence_s * 4 + 30
        while time.time() < deadline:
            reply = httpx.get(
                f"{base}/v1/city/scheduled/demo_arterial/latest", timeout=10.0
            ).json()
            if reply.get("run_id") and reply["run_id"] != stale_run_id:
                run_id = reply["run_id"]
                break
            time.sleep(2)
        if run_id is None:
            raise TimeoutError("scheduler never submitted a run")
        print(f"[demo] scheduler auto-submitted run_id={run_id}")

        status = {}
        deadline = time.time() + 900
        while time.time() < deadline:
            status = httpx.get(
                f"{base}/v1/city/optimizations/{run_id}/status", timeout=10.0
            ).json()
            progress = status.get("progress") or {}
            print(
                f"[demo] status={status['status']:<9} "
                f"progress {progress.get('done', 0)}/{progress.get('total', 0)}"
            )
            if status["status"] in {"completed", "failed"}:
                break
            time.sleep(3)
        if status.get("status") != "completed":
            raise RuntimeError(f"scheduled run did not complete: {status}")

        result = httpx.get(
            f"{base}/v1/city/optimizations/{run_id}/result", timeout=10.0
        ).json()
        aggregated = result["config"]["tmc_json"]
        print(f"\n[demo] optimization ran on the aggregated hour: {aggregated}")
        print_result(result)
        nema = httpx.get(
            f"{base}/v1/city/optimizations/{run_id}/nema", timeout=10.0
        ).json()
        print_nema(nema)
        print("\n[demo] scheduled mode OK")
        return 0
    finally:
        stop_service(proc)


if __name__ == "__main__":
    sys.exit(main())
