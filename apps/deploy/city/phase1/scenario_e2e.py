#!/usr/bin/env python3
"""Scenario self-test: a complete SUMO scenario (net, routes, signal programs) through the REST API.

Registers the demo scenario baked into the image (examples/corridor_demo, at
/data/input/demo_scenario), runs ONE asynchronous optimization over it with the
switches such a scenario needs, and checks the result:

    python scenario_e2e.py --service http://127.0.0.1:8100 [--timeout 900]

Under test: the demand and the turning counts come from the scenario's own route
files (flow_source / counts_source = scenario_routes), the scenario's own signal
programs are the baseline (seed_from_scenario_programs), per-approach stages, the
joint bayes_corridor search with a cycle search, and both delay metrics (the
all-demand MOE and the per-junction edge-based delay).

Exit code 0 means every check passed (the last line is "=== SCENARIO PASS ===").
Any failure prints the last reply of the service and exits 1. Needs only the
standard library, so it runs inside the terasim-metropolis image next to
phase1_e2e.py: see apps/deploy/city/README.md.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import sys
import time
import urllib.error
import urllib.request
from typing import NoReturn

SUMOCFG = "/data/input/demo_scenario/demo_scenario.sumocfg"
PLACEHOLDER_TMC = "/data/input/demo_scenario/placeholder_tmc.json"
SIGNALS = ["J1", "J2", "J3"]
REQUEST = {
    "tmc_json": PLACEHOLDER_TMC,
    "base_sumocfg": SUMOCFG,
    "corridor": {"tls_ids": SIGNALS},
    "optimizer": "bayes_corridor",
    "search_cycle": True,
    "cycle_min_s": 45,
    "cycle_max_s": 180,
    "stage_mode": "per_approach",
    "flow_source": "scenario_routes",
    "counts_source": "scenario_routes",
    "seed_from_scenario_programs": True,
    "max_trials": 6,
    "patience": 4,
    "n_workers": 2,
    "sim_end": 600,
}
#: Request fields the result's ``config`` must echo unchanged: the REST switches reached the run.
ECHOED = (
    "optimizer", "search_cycle", "cycle_min_s", "cycle_max_s", "stage_mode", "flow_source",
    "counts_source", "seed_from_scenario_programs", "max_trials", "patience", "n_workers", "sim_end",
)
POLL_INTERVAL_S = 5.0
REQUEST_TIMEOUT_S = 60.0

# The service is an in-cluster / compose address: never route it through a proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
#: The most recent reply of the service, printed when the run fails.
_last_reply: dict = {"note": "no reply yet"}


def stage(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def fail(message: str) -> NoReturn:
    print(f"\n=== SCENARIO FAIL: {message} ===", file=sys.stderr, flush=True)
    print(f"last status: {json.dumps(_last_reply, default=str)}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def check(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def call(method: str, url: str, payload: dict | None = None) -> tuple[int, object]:
    """``(http status, decoded JSON body)``; status 0 when the service did not answer."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=REQUEST_TIMEOUT_S) as reply:
            status, body = reply.status, reply.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        _last_reply.clear()
        _last_reply.update({"method": method, "url": url, "error": f"{type(exc).__name__}: {exc}"})
        return 0, None
    try:
        decoded: object = json.loads(body.decode("utf-8")) if body else None
    except ValueError:
        decoded = body.decode("utf-8", errors="replace")
    _last_reply.clear()
    _last_reply.update({"method": method, "url": url, "http": status, "body": decoded})
    return status, decoded


def wait_for_service(service: str, deadline: float) -> None:
    while True:
        status, _ = call("GET", f"{service}/openapi.json")
        if 0 < status < 500:
            return
        if time.monotonic() >= deadline:
            fail(f"the service at {service} did not answer in time")
        time.sleep(2.0)


def submit(service: str, deadline: float) -> str:
    while True:
        status, reply = call("POST", f"{service}/v1/city/optimizations", REQUEST)
        if status == 202 and isinstance(reply, dict) and reply.get("run_id"):
            return str(reply["run_id"])
        # 429: every concurrency slot is taken (another run is in flight); try again.
        if status == 429 and time.monotonic() < deadline:
            print("all worker slots are busy; retrying", flush=True)
            time.sleep(POLL_INTERVAL_S)
            continue
        fail(f"POST /v1/city/optimizations answered {status}")


def wait_for_run(service: str, run_id: str, started: float, deadline: float) -> None:
    status_url = f"{service}/v1/city/optimizations/{run_id}/status"
    while True:
        status, reply = call("GET", status_url)
        if status == 200 and isinstance(reply, dict):
            progress = reply.get("progress") or {}
            print(
                f"[{time.monotonic() - started:5.0f} s] {reply.get('status')} "
                f"{progress.get('done')}/{progress.get('total')} best_score={reply.get('best_score')}",
                flush=True,
            )
            if reply.get("status") == "completed":
                return
            if reply.get("status") == "failed":
                # /status carries no reason; the 409 of /result does.
                _, why = call("GET", f"{service}/v1/city/optimizations/{run_id}/result")
                fail(f"the optimization failed: {why}")
        elif status != 0:
            fail(f"GET {status_url} answered {status}")
        if time.monotonic() >= deadline:
            fail("timed out waiting for the optimization to complete")
        time.sleep(POLL_INTERVAL_S)


def as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def is_delay(value: object) -> bool:
    """A delay in s/veh: a finite number >= 0 (a bool is not a number here)."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def number(value: object, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "n/a"
    return f"{value:.{digits}f}"


def cycles(block: dict) -> str:
    timing = as_dict(block.get("timing"))
    return " ".join(f"{tls}={number(cycle, 1)}" for tls, cycle in as_dict(timing.get("cycle_s")).items())


def check_result(result: dict, config: dict) -> None:
    baseline, best = as_dict(result.get("baseline")), as_dict(result.get("best"))
    check(
        as_dict(result.get("seed")).get("source") == "sumo_tls",
        f"seed.source is {as_dict(result.get('seed')).get('source')!r}, not 'sumo_tls': "
        "the scenario's own programs were not the baseline",
    )
    baseline_cycles = as_dict(as_dict(baseline.get("timing")).get("cycle_s"))
    check(
        set(baseline_cycles) == set(SIGNALS),
        f"baseline.timing.cycle_s lists {sorted(baseline_cycles)}, expected {SIGNALS}",
    )
    best_cycles = as_dict(as_dict(best.get("timing")).get("cycle_s"))
    check(bool(best.get("timing")) and set(best_cycles) == set(SIGNALS), "best.timing is missing or incomplete")
    junction_delays = as_dict(as_dict(best.get("moe")).get("junction_delay_s"))
    check(
        set(junction_delays) == set(SIGNALS),
        f"best.moe.junction_delay_s lists {sorted(junction_delays)}, expected {SIGNALS}",
    )
    # Both delay metrics are numbers on both sides: the all-demand MOE and the
    # per-junction edge-based average.
    for side, block in (("baseline", baseline), ("best", best)):
        moe = as_dict(block.get("moe"))
        for key in ("control_delay_s_per_veh", "corridor_junction_delay_s"):
            check(is_delay(moe.get(key)), f"{side}.moe.{key} is {moe.get(key)!r}, not a delay in s/veh")
    check(
        config.get("counts_source") == "scenario_routes",
        f"config.counts_source is {config.get('counts_source')!r}",
    )
    check(
        str(config.get("tmc_json", "")).endswith("derived_tmc.json"),
        f"config.tmc_json is {config.get('tmc_json')!r}: the counts were not derived from the routes",
    )
    for key in ECHOED:
        check(config.get(key) == REQUEST[key], f"config.{key} is {config.get(key)!r}, the request said {REQUEST[key]!r}")
    search, nema_export = as_dict(result.get("search")), as_dict(result.get("nema_export"))
    check(search.get("ran") is True, f"the Bayesian search did not run: {search}")
    check(nema_export.get("exported") is True, f"no NEMA plan was exported: {nema_export}")


def print_summary(result: dict, config: dict) -> None:
    baseline, best = as_dict(result.get("baseline")), as_dict(result.get("best"))
    sides = {"baseline": as_dict(baseline.get("moe")), "best": as_dict(best.get("moe"))}
    stage("result")
    print(f"seed             {as_dict(result.get('seed')).get('source')}  (plan {baseline.get('plan_id')})")
    print(f"best plan        {best.get('plan_id')}  (score {number(best.get('score'), 3)}, {result.get('n_trials')} trials)")
    print(f"search           {as_dict(result.get('search'))}")
    print(f"nema_export      {as_dict(result.get('nema_export'))}")
    print(f"counts from      {config.get('tmc_json')}")
    print(f"{'':28}{'baseline':>12}{'best':>12}")
    for label, key in (
        ("control delay (s/veh)", "control_delay_s_per_veh"),
        ("junction delay (s/veh)", "corridor_junction_delay_s"),
    ):
        print(f"{label:28}{number(sides['baseline'].get(key)):>12}{number(sides['best'].get(key)):>12}")
    for tls_id in SIGNALS:
        delays = [number(as_dict(side.get("junction_delay_s")).get(tls_id), 1) for side in sides.values()]
        print(f"{'  ' + tls_id + ' delay (s/veh)':28}{delays[0]:>12}{delays[1]:>12}")
    print(f"cycles (s)       baseline  {cycles(baseline)}")
    print(f"                 best      {cycles(best)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--service", default="http://127.0.0.1:8100", help="base URL of terasim-service")
    parser.add_argument(
        "--timeout", type=float, default=900.0, help="seconds for the whole run, the optimization included"
    )
    args = parser.parse_args(argv)
    service = args.service.rstrip("/")
    started = time.monotonic()
    deadline = started + args.timeout

    stage("wait for the service")
    wait_for_service(service, deadline)
    print(f"{service} is up")

    stage("register the bundled scenario (POST /v1/city/maps)")
    status, reply = call("POST", f"{service}/v1/city/maps", {"path": SUMOCFG})
    check(status == 201 and isinstance(reply, dict) and "map_id" in reply, f"POST /v1/city/maps answered {status}")
    print(f"map_id {as_dict(reply)['map_id']}  duplicate={as_dict(reply).get('duplicate')}")

    stage("submit the optimization (POST /v1/city/optimizations)")
    run_id = submit(service, deadline)
    print(f"run_id {run_id}")

    stage("wait for the run (GET .../status)")
    wait_for_run(service, run_id, started, deadline)

    stage("check the result (GET .../result)")
    status, result = call("GET", f"{service}/v1/city/optimizations/{run_id}/result")
    check(status == 200 and isinstance(result, dict), f"GET .../result answered {status}")
    config = as_dict(as_dict(result).get("config"))
    check_result(as_dict(result), config)
    status, nema = call("GET", f"{service}/v1/city/optimizations/{run_id}/nema")
    check(status == 200 and isinstance(nema, dict), f"GET .../nema answered {status}")
    print_summary(as_dict(result), config)
    print(f"NEMA plan        {len(as_dict(nema).get('intersections', []))} intersection(s)")

    print("\n=== SCENARIO PASS ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
