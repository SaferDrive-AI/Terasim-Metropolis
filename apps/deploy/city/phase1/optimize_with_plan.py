#!/usr/bin/env python3
"""Skills 2 and 3 in one go: upload a NEMA plan, optimize against live TMC, save the new plan.

    python optimize_with_plan.py --mcp http://terasim-mcp:9911/mcp \
        --plan /plans/i1.nema_plan.json --out /out/i1_optimized.json

The target defaults to intersection i1 of the demo corridor (``demo_arterial``); the plan's
own ``corridor_id`` is used unless ``--corridor`` is given.

Steps (all over MCP, i.e. exactly what the VSS agent would call):
  1. upload_signal_plan(plan, corridor_id)      -> stored (corridor_id, intersection_id) keys
  2. optimize_intersection(corridor_id, target) -> {"nema_plan": ..., "kpis": ...}
The result file keeps the input plan, the store reply and the optimizer output side by side,
and the console prints a before/after comparison of cycle / offset / splits per pattern.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

DEFAULT_TARGET = {
    "intersection_id": "i1",
    "name": "Arterial Ave & 1st Cross St",
    "lat": 37.335,
    "lon": -121.90339,
}


def tool_result_json(result) -> dict:
    """The tool's JSON payload. An optimize tool answers with the chat markdown block
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


def describe_patterns(intersection: dict) -> list[str]:
    """One line per time-of-day pattern: cycle / offset / splits (tolerant to schema variants)."""
    lines = []
    for pattern in intersection.get("patterns", []) or []:
        splits = pattern.get("splits") or {}
        split_text = " ".join(f"{k}:{splits[k]}" for k in sorted(splits, key=lambda x: int(x)))
        lines.append(
            f"    {pattern.get('pattern_id', '?'):<10} cycle={pattern.get('cycle_s', '?')}s "
            f"offset={pattern.get('offset_s', '?')}s splits[{split_text}]"
        )
    if not lines and "phases" in intersection:
        # terasim.nema_timing.v1 output: cycle/offset at the intersection level,
        # per-phase split_s / green_s inside phases[].
        phases = intersection.get("phases") or []
        split_text = " ".join(
            f"{p.get('nema_phase', p.get('phase', '?'))}:{p.get('split_s', '?')}"
            for p in phases
        )
        green_text = " ".join(
            f"{p.get('nema_phase', p.get('phase', '?'))}:{p.get('green_s', '?')}" for p in phases
        )
        cycle = intersection.get("cycle_s", "?")
        offset = intersection.get("offset_s", "?")
        lines.append(
            f"    {intersection.get('pattern_id', '?'):<10} cycle={cycle}s offset={offset}s "
            f"splits[{split_text}]"
        )
        windows = intersection.get("valid_windows")
        lines.append(f"    {'':<10} greens[{green_text}] windows={windows}")
    return lines


async def run(args) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    corridor_id = args.corridor or plan.get("corridor_id")
    if not corridor_id:
        raise SystemExit("corridor_id missing: pass --corridor or put corridor_id in the plan")
    target = dict(DEFAULT_TARGET)
    if args.intersection_id:
        target["intersection_id"] = args.intersection_id
    if args.name:
        target["name"] = args.name

    async with streamablehttp_client(args.mcp) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            for needed in ("upload_signal_plan", "optimize_intersection"):
                if needed not in tools:
                    raise SystemExit(f"MCP server lacks tool {needed}; has {sorted(tools)}")

            print("=== 1. upload_signal_plan ===", flush=True)
            stored = tool_result_json(
                await session.call_tool(
                    "upload_signal_plan", {"plan": plan, "corridor_id": corridor_id}
                )
            )
            print(json.dumps(stored, indent=2))

            print("=== 2. optimize_intersection (synchronous; may take minutes) ===", flush=True)
            started = time.monotonic()
            result = tool_result_json(
                await session.call_tool(
                    "optimize_intersection",
                    {"corridor_id": corridor_id, "intersection": target},
                )
            )
            elapsed = time.monotonic() - started

    kpis = result.get("kpis", {})
    nema_plan = result.get("nema_plan", {})
    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "corridor_id": corridor_id,
        "target": target,
        "input_plan_file": str(args.plan),
        "input_plan": plan,
        "upload_reply": stored,
        "optimize_seconds": round(elapsed, 1),
        "kpis": kpis,
        "optimized_nema_plan": nema_plan,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

    print(f"\n=== optimize_intersection finished in {elapsed:.0f}s ===")
    print("KPIs:", json.dumps(kpis, indent=2))
    print("\n=== BEFORE (uploaded plan) ===")
    for inter in plan.get("intersections", []):
        print(f"  {inter.get('intersection_id')} ({inter.get('name', '')})")
        print("\n".join(describe_patterns(inter)) or "    (no patterns)")
    print("\n=== AFTER (optimized NEMA plan) ===")
    print(f"  schema={nema_plan.get('schema')} handoff_ready={nema_plan.get('handoff_ready')}")
    for inter in nema_plan.get("intersections", []):
        print(f"  {inter.get('intersection_id')} ({inter.get('name', '')})")
        print("\n".join(describe_patterns(inter)) or "    " + json.dumps(inter)[:400])
    print(f"\nsaved -> {out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp", default="http://127.0.0.1:9911/mcp")
    parser.add_argument("--plan", required=True, help="terasim.nema_plan.v1 JSON file")
    parser.add_argument("--out", required=True, help="where to write the result JSON")
    parser.add_argument("--corridor", default=None, help="corridor_id (default: from the plan)")
    parser.add_argument("--intersection-id", default=None, help="override target id (default i1)")
    parser.add_argument("--name", default=None, help="override target display name")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
