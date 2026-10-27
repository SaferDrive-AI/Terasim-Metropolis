#!/usr/bin/env python3
"""End-to-end check of the stack: register -> inject one TMC window -> register the map over
MCP -> optimize over MCP.

    python phase1_e2e.py --service http://127.0.0.1:8100 --tmc http://127.0.0.1:8200 \
        --mcp http://127.0.0.1:9911/mcp [--with-plan plan.nema_plan.json]

It runs on the synthetic demo corridor of the deployment: intersection i1 of
``demo_arterial`` (TMC spec ``Metadata.json``) on the map the image bakes at
``/data/input/map/demo.sumocfg`` (``--map`` names another server-side map file).
Exit code 0 means every stage passed. Runs inside the terasim-metropolis image too
(all dependencies are installed there): see apps/deploy/city/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import httpx

CORRIDOR_ID = "demo_arterial"
INTERSECTION = {
    "intersection_id": "i1",
    "name": "Arterial Ave & 1st Cross St",
    "lat": 37.335,
    "lon": -121.90339,
}
DEFAULT_MAP = "/data/input/map/demo.sumocfg"
EXPECTED_TOOLS = {
    "upload_traffic_snapshot", "upload_signal_plan", "upload_map",
    "optimize_intersection", "optimize_corridor", "register_intersection", "list_intersections",
    "optimize_intersection_by_id", "optimize_registered_intersection",
}


def wait_http(url: str, *, timeout_s: float = 300.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=5.0).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2.0)
    raise SystemExit(f"timed out waiting for {url}")


def stage(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def tool_result_json(result) -> dict:
    """The optimize payload: an optimize tool answers with the chat markdown block
    first and the JSON payload (nema_plan/kpis/...) as a later text block."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured.get("result", structured)
    for item in result.content:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            continue  # the markdown block
        if isinstance(document, dict):
            return document
    raise SystemExit(f"tool returned no JSON content: {result!r}")


async def mcp_stage(mcp_url: str, plan_path: str | None, map_path: str = DEFAULT_MAP) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            missing = EXPECTED_TOOLS - tools
            if missing:
                raise SystemExit(f"MCP server is missing tools: {sorted(missing)}")
            print("MCP tools:", sorted(tools))

            listing = tool_result_json(await session.call_tool("list_intersections", {}))
            print("list_intersections ->", json.dumps(listing)[:300])

            registered_map = tool_result_json(
                await session.call_tool("upload_map", {"path": map_path})
            )
            print("upload_map ->", registered_map)
            if "map_id" not in registered_map:
                raise SystemExit(f"upload_map returned an unexpected shape: {registered_map}")

            if plan_path:
                plan = json.loads(open(plan_path, encoding="utf-8").read())
                stored = tool_result_json(
                    await session.call_tool(
                        "upload_signal_plan", {"plan": plan, "corridor_id": CORRIDOR_ID}
                    )
                )
                print("upload_signal_plan ->", stored)

            stage("optimize_intersection over MCP (synchronous; may take minutes)")
            started = time.monotonic()
            result = tool_result_json(
                await session.call_tool(
                    "optimize_intersection",
                    {"corridor_id": CORRIDOR_ID, "intersection": INTERSECTION},
                )
            )
            print(f"optimize_intersection finished in {time.monotonic() - started:.0f}s")
            return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", default="http://127.0.0.1:8100")
    parser.add_argument("--tmc", default="http://127.0.0.1:8200")
    parser.add_argument("--mcp", default="http://127.0.0.1:9911/mcp")
    parser.add_argument("--with-plan", default=None,
                        help="terasim.nema_plan.v1 JSON to upload first")
    parser.add_argument("--map", default=DEFAULT_MAP,
                        help=f"server-side map file to register with upload_map (default {DEFAULT_MAP})")
    args = parser.parse_args()

    stage("wait for services")
    wait_http(f"{args.service}/openapi.json")
    wait_http(f"{args.tmc}/v1/tmc/health")
    print("service + tmc healthy")

    stage("register intersection (skill 1)")
    reply = httpx.post(f"{args.tmc}/v1/tmc/intersections", json=INTERSECTION, timeout=30.0)
    if reply.status_code != 201:
        raise SystemExit(f"register failed: {reply.status_code} {reply.text}")
    print("registered ->", reply.json())

    stage("run-once: fetch behaviors -> TMC -> inject into TeraSim")
    reply = httpx.post(f"{args.tmc}/v1/tmc/run-once", timeout=120.0)
    if reply.status_code != 200:
        raise SystemExit(f"run-once failed: {reply.status_code} {reply.text}")
    tick = reply.json()
    print("tick ->", json.dumps({k: v for k, v in tick.items() if k != "stats"}, indent=2))
    if tick.get("status") not in {"injected", "skipped"}:
        raise SystemExit(f"unexpected tick status: {tick}")
    if tick.get("status") == "injected" and sum(tick["behaviors"].values()) == 0:
        raise SystemExit("no behavior objects were returned for any camera")

    result = asyncio.run(mcp_stage(args.mcp, args.with_plan, args.map))
    if "nema_plan" not in result or "kpis" not in result:
        raise SystemExit(f"optimize returned an unexpected shape: {result}")
    kpis = result["kpis"]
    print("KPIs:", json.dumps(kpis, indent=2))
    print("NEMA plan intersections:", len(result["nema_plan"].get("intersections", [])))
    if kpis["optimized"]["cost"] > kpis["baseline"]["cost"] + 1e-9:
        print("WARNING: optimized cost is worse than baseline", file=sys.stderr)
    stage("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
