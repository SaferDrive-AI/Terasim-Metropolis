#!/usr/bin/env python3
"""Poll an asynchronous TeraSim city optimization run and save its NEMA plan + KPIs.

    python fetch_async_run.py --service http://terasim-service:8000 --run-id <id> \
        --plan /plans/i1.nema_plan.json --out /out/result.json

Companion to optimize_with_plan.py for runs submitted through the submit-and-poll API
(`POST /v1/city/optimizations`, which accepts max_trials / n_workers explicitly).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from terasim_agent_skill.client import CityOptimizationClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", default="http://127.0.0.1:8100")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--plan", default=None, help="input plan JSON to embed in the output")
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout-s", type=float, default=1800.0)
    args = parser.parse_args()

    client = CityOptimizationClient(args.service, timeout_s=120.0)
    started = time.monotonic()
    last = None
    while True:
        status = client.get_status(args.run_id)
        key = (status.get("status"), json.dumps(status.get("progress")), status.get("best_score"))
        if key != last:
            print(
                f"[{time.monotonic() - started:5.0f}s] status={status.get('status')} "
                f"progress={status.get('progress')} best={status.get('best_score')}",
                flush=True,
            )
            last = key
        if status.get("status") in ("completed", "failed"):
            break
        if time.monotonic() - started > args.timeout_s:
            raise SystemExit(f"timed out waiting for run {args.run_id}: {status}")
        time.sleep(5)
    if status.get("status") != "completed":
        raise SystemExit(f"run failed: {status}")

    nema = client.get_nema(args.run_id)
    result = client.get_result(args.run_id)
    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": args.run_id,
        "input_plan": json.loads(Path(args.plan).read_text(encoding="utf-8")) if args.plan else None,
        "result": result,
        "optimized_nema_plan": nema,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

    print("\n=== KPI baseline vs best ===")
    for label in ("baseline", "best"):
        entry = result.get(label, {}) or {}
        moe = entry.get("moe", {}) or {}
        print(
            f"  {label:<9} plan={entry.get('plan_id')} score={entry.get('score')} "
            f"delay={moe.get('control_delay_s_per_veh')} stops={moe.get('stops_per_veh')}"
        )
    print("\n=== optimized NEMA (per intersection) ===")
    for inter in nema.get("intersections", []):
        print(
            f"  tls={inter.get('tls_id')} pattern={inter.get('pattern_id')} "
            f"cycle={inter.get('cycle_s')} offset={inter.get('offset_s')}"
        )
        phases = inter.get("phases", [])
        print("   splits:", " ".join(f"{p.get('nema_phase')}:{p.get('split_s')}" for p in phases))
        print("   greens:", " ".join(f"{p.get('nema_phase')}:{p.get('green_s')}" for p in phases))
    print(f"saved -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
