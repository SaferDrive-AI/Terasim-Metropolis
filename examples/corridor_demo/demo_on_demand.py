"""On-demand mode demo: an agent-style tool call against the /v1 city API.

Queue-first flow: starts a local terasim-service, pushes the six 10-minute
TMC snapshots to /v1/city/snapshots (the periodic-injection demand path — the
same queue every trigger mode reads), then submits a corridor optimization by
``corridor_id`` ("optimize traffic from i1 to i3"): the service aggregates the
buffered last-hour snapshots into the demand file server-side. Polls to
completion and prints the resolved corridor, the ranking, and the NEMA plan —
exactly the round-trip the TeraSim Agent Skill (MCP) performs.

Run from the repo root:  python examples/corridor_demo/demo_on_demand.py
Options: --port 8000  --max-trials 8  --sim-end 300
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = Path(__file__).resolve().parent


def start_service(port: int, output_root: Path, extra_env: dict | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "TERASIM_ENABLE_CITY_API": "1",
            "TERASIM_CITY_OUTPUT_ROOT": str(output_root),
            "TERASIM_CITY_INPUT_ROOT": str(REPO_ROOT / "examples"),
            "TERASIM_REDIS_HOST": "127.0.0.1",
        }
    )
    env.update(extra_env or {})
    proc = subprocess.Popen(
        [sys.executable, "-m", "terasim_service"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 60
    url = f"http://127.0.0.1:{port}/openapi.json"
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"service exited early with code {proc.returncode}")
        try:
            if httpx.get(url, timeout=2.0).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.5)
    proc.terminate()
    raise TimeoutError("service did not become ready within 60s")


def push_snapshots(base: str, snapshot_dir: Path) -> int:
    """Push every snap_*.json in ``snapshot_dir`` to /v1/city/snapshots.

    Rewrites each ``timestamp_start`` to a recent UTC time (10 minutes apart,
    newest = now) so the snapshots land inside the service's lookback window.
    The ingest contract is UTC: emit tz-aware timestamps so the isoformat
    carries the +00:00 offset explicitly. Returns the number pushed.
    """
    paths = sorted(snapshot_dir.glob("snap_*.json"))
    if not paths:
        raise FileNotFoundError(f"no snap_*.json under {snapshot_dir}")
    now = dt.datetime.now(dt.timezone.utc)
    print(f"[demo] TMC pushes {len(paths)} x 10-minute snapshots ...")
    for index, path in enumerate(paths):
        payload = json.loads(path.read_text(encoding="utf-8"))
        stamp = now - dt.timedelta(minutes=10 * (len(paths) - 1 - index))
        payload["timestamp_start"] = stamp.isoformat(timespec="seconds")
        reply = httpx.post(f"{base}/v1/city/snapshots", json=payload, timeout=10.0)
        if reply.status_code != 202:
            raise RuntimeError(f"snapshot push failed: {reply.status_code} {reply.text}")
        body = reply.json()
        print(
            f"[demo]   pushed {path.name} ({payload['timestamp_start']}) "
            f"-> buffered {body['buffered']}"
        )
    return len(paths)


def stop_service(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def print_result(result: dict) -> None:
    print("\n== resolved corridor ==")
    for item in result["resolved_corridor"]:
        print(
            f"  {item['ref']:>12}  ->  node {item['node_id']:<4} tls {item['tls_id']:<4} "
            f"(match {item['match_distance_m']:.1f} m)"
        )
    baseline, best = result["baseline"], result["best"]
    print("\n== ranking ==")
    print(f"  baseline  {baseline['plan_id']:<14} score {baseline['score']:.4f}")
    print(
        f"  best      {best['plan_id']:<14} score {best['score']:.4f} "
        f"(source {best['source']})"
    )
    if best.get("offsets"):
        offsets = ", ".join(f"{k}={v:.0f}s" for k, v in best["offsets"].items())
        print(f"  best offsets: {offsets}")
    improvement = (1.0 - best["score"] / baseline["score"]) * 100.0
    print(f"  weighted-KPI improvement vs baseline: {improvement:.1f}%")
    trials = [t for t in result["trials"] if t["valid"]]
    trials.sort(key=lambda t: t["score"])
    print("\n== top trials ==")
    for t in trials[:5]:
        offsets = ", ".join(f"{k}={v:.0f}" for k, v in (t.get("offsets") or {}).items())
        print(f"  {t['plan_id']:<14} score {t['score']:.4f}  [{t['source']}]  {offsets}")
    moe = best["moe"]
    print("\n== best-plan MOEs ==")
    print(f"  control delay : {moe['control_delay_s_per_veh']:.2f} s/veh")
    print(f"  stops         : {moe['stops_per_veh']:.2f} /veh")
    print(
        f"  vehicles      : {moe['vehicles_total']} total, "
        f"{moe['vehicles_completed']} completed, {moe['vehicles_unfinished']} unfinished"
    )


def print_nema(nema: dict) -> None:
    coord = nema["coordination"]
    print("\n== NEMA timing plan ==")
    print(
        f"  corridor {nema['corridor_id']} | cycle {coord['cycle_length_s']:.0f} s | "
        f"reference {coord['reference_intersection']} | handoff_ready {nema['handoff_ready']}"
    )
    for intersection in nema["intersections"]:
        print(f"  {intersection['tls_id']}  offset {intersection['offset_s']:.0f} s")
        for row in intersection["phases"]:
            phase = row["nema_phase"] if row["nema_phase"] is not None else "-"
            movements = ",".join(row["movements"]) or "-"
            print(
                f"    phase {phase!s:>2}  split {row['split_s']:5.1f}s "
                f"(G {row['green_s']:.0f} Y {row['yellow_s']:.0f} AR {row['all_red_s']:.0f})  {movements}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--max-trials", type=int, default=12)
    parser.add_argument("--sim-end", type=float, default=600.0)
    args = parser.parse_args()

    output_root = Path(tempfile.mkdtemp(prefix="terasim_city_demo_"))
    base = f"http://127.0.0.1:{args.port}"
    print(f"[demo] starting terasim-service on :{args.port} (output {output_root})")
    proc = start_service(args.port, output_root)
    try:
        pushed = push_snapshots(base, DEMO_DIR / "snapshots")
        body = {
            "corridor_id": "demo_arterial",
            "base_sumocfg": str(DEMO_DIR / "demo.sumocfg"),
            "corridor": {"from": "i1", "to": "i3"},
            "baseline_sheet": str(DEMO_DIR / "baseline_sheet_uncoordinated.json"),
            "optimizer": "bayes_offsets",
            "max_trials": args.max_trials,
            "sim_end": args.sim_end,
            "n_workers": 2,
        }
        print(f"[demo] demand = last-hour queue aggregation ({pushed} snapshots)")
        print('[demo] agent asks: "optimize the corridor from i1 to i3"')
        print(
            f"[demo] POST /v1/city/optimizations  "
            f"(corridor_id=demo_arterial, bayes_offsets, {args.max_trials} trials)"
        )
        reply = httpx.post(f"{base}/v1/city/optimizations", json=body, timeout=30.0)
        if reply.status_code != 202:
            raise RuntimeError(f"submit failed: {reply.status_code} {reply.text}")
        run_id = reply.json()["run_id"]
        print(f"[demo] accepted run_id={run_id}")

        deadline = time.time() + 900
        status = {}
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
            raise RuntimeError(f"run did not complete: {status}")

        result = httpx.get(
            f"{base}/v1/city/optimizations/{run_id}/result", timeout=10.0
        ).json()
        print(
            f"\n[demo] optimization ran on the aggregated hour: "
            f"{result['config']['tmc_json']}"
        )
        print_result(result)
        nema = httpx.get(
            f"{base}/v1/city/optimizations/{run_id}/nema", timeout=10.0
        ).json()
        print_nema(nema)

        print("\n[demo] on-demand mode OK")
        return 0
    finally:
        stop_service(proc)


if __name__ == "__main__":
    sys.exit(main())
