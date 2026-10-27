#!/usr/bin/env python3
"""Stand-in for the VSS behavior API (GET /behavior) so the whole stack runs offline.

Serves synthetic behavior objects for the twelve sensors of the demo corridor spec
(``Metadata.json``: intersections i1, i2 and i3 of ``demo_arterial``, sensors
``<intersection>-cam1`` .. ``cam4`` for the southbound, westbound, northbound and
eastbound approach). Every sensor answers with a fixed 10-minute demand, whatever window
is asked for: the hourly turning counts of the demo corridor divided by six, an E-W
arterial with lighter N-S cross traffic. Each object enters on the approach edge of its
sensor and leaves on the exit edge of its movement, using the edge ids of the spec.

Only the standard library is used, so it runs on any host or in the TeraSim image:

    python3 stub_behavior_server.py --port 8081
    curl 'http://127.0.0.1:8081/behavior?sensorId=i1-cam4&queryString=end:[2026-01-01T00:00:00.000Z%20TO%202026-01-01T00:09:59.000Z]'
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# intersection id, signal node, west neighbour, east neighbour (node ids of the demo network)
JUNCTIONS = (
    ("i1", "j1", "w1", "j2"),
    ("i2", "j2", "j1", "j3"),
    ("i3", "j3", "j2", "e1"),
)
# Vehicles per 10 minutes by direction of travel and turn.
COUNTS = {
    "i1": {"South": {"Straight": 18, "Right": 10, "Left": 7},
           "West": {"Straight": 67, "Right": 13, "Left": 8},
           "North": {"Straight": 20, "Right": 12, "Left": 8},
           "East": {"Straight": 93, "Right": 15, "Left": 10}},
    "i2": {"South": {"Straight": 22, "Right": 11, "Left": 8},
           "West": {"Straight": 72, "Right": 12, "Left": 9},
           "North": {"Straight": 23, "Right": 13, "Left": 8},
           "East": {"Straight": 98, "Right": 13, "Left": 12}},
    "i3": {"South": {"Straight": 25, "Right": 12, "Left": 8},
           "West": {"Straight": 63, "Right": 15, "Left": 10},
           "North": {"Straight": 17, "Right": 10, "Left": 7},
           "East": {"Straight": 87, "Right": 17, "Left": 9}},
}
DEFAULT_END = "2026-01-01T00:00:00.000Z"


def _demand() -> dict[str, tuple[str, dict[str, str], dict[str, int]]]:
    """sensor -> (entry edge, {movement: exit edge}, {movement: vehicles per 10 min})."""
    demand = {}
    for iid, node, west, east in JUNCTIONS:
        north, south = f"{node}n1", f"{node}s1"
        leave = {
            "South": f"{node}-{south}", "West": f"{node}-{west}",
            "North": f"{node}-{north}", "East": f"{node}-{east}",
        }
        approaches = (
            ("cam1", "South", f"{north}-{node}", ("West", "South", "East")),
            ("cam2", "West", f"{east}-{node}", ("North", "West", "South")),
            ("cam3", "North", f"{south}-{node}", ("East", "North", "West")),
            ("cam4", "East", f"{west}-{node}", ("South", "East", "North")),
        )
        for cam, heading, entry, (right, straight, left) in approaches:
            exits = {"Right": leave[right], "Straight": leave[straight], "Left": leave[left]}
            demand[f"{iid}-{cam}"] = (entry, exits, COUNTS[iid][heading])
    return demand


DEMAND = _demand()


def behaviors_for(sensor: str, end: str) -> list[dict]:
    """The behavior objects of one sensor, all ending at ``end``."""
    if sensor not in DEMAND:
        return []
    entry, exits, counts = DEMAND[sensor]
    objects = []
    object_id = 1
    for movement, number in counts.items():
        for _ in range(number):
            objects.append({
                "sensor": {"id": sensor},
                "object": {"id": f"{sensor}-{object_id}", "type": "Vehicle"},
                "edges": [entry, exits[movement]],
                "end": end,
            })
            object_id += 1
    return objects


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        url = urlparse(self.path)
        if url.path != "/behavior":
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(url.query)
        sensor = params.get("sensorId", [""])[0]
        query = params.get("queryString", [""])[0]
        end = query.split(" TO ")[-1].rstrip("]") if " TO " in query else DEFAULT_END
        body = json.dumps({"behaviors": behaviors_for(sensor, end)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - http.server API
        print("[stub-behavior]", format % args, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    print(f"[stub-behavior] serving GET /behavior for {len(DEMAND)} sensors on {args.host}:{args.port}",
          flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
