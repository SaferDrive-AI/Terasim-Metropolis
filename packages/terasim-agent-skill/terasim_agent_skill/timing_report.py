"""Render a signal plan back into a signal-controller timing-report style PDF.

A city's signal timing arrives as a controller timing report; converted into a
``terasim.nema_plan.v1`` JSON document it is uploaded (``upload_signal_plan``)
and optimized by TeraSim, and this module prints the result in the same
document format — same section headings, same phase-column layout, same
footer — so a traffic engineer can read the optimized plan without learning
the JSON.

Two modes:

* ``--plan`` only              -> "SIGNAL TIMING REPORT - CURRENT PLAN"
* ``--plan`` + ``--optimized`` -> "SIGNAL TIMING REPORT - OPTIMIZED BY TERASIM";
  the coordination-pattern page of the optimized pattern grows
  ``Split (current)`` / ``Split (optimized)`` / ``Green (optimized)`` rows and a
  KPI comparison page is appended.

Pages: cover, ``(1-1) PHASE TIMING``, one ``(3-2) COORDINATION PATTERNS n`` per
pattern, ``(5-1) TOD PATTERNS``, and (optimized mode) ``KPI COMPARISON``. An
optimized plan without NEMA phase numbers or a pattern of the city's (a static
plan TeraSim generated, or the scenario's own programs) gets an ``OPTIMIZED
TIMING (STAGES)`` page instead: its stages in running order (S1, S2...) with the
movements each serves.

Requirements:
    pip install reportlab        # the only non-stdlib dependency

Usage:
    python -m terasim_agent_skill.timing_report --plan PLAN.json --out OUT.pdf
    python -m terasim_agent_skill.timing_report --plan PLAN.json --optimized RUN.json \\
        --out OUT.pdf --intersection-name "1ST AVE @ MAIN ST"

``--optimized`` accepts either an optimization *run* JSON (with
``optimized_nema_plan`` + ``result``) or a bare ``terasim.nema_timing.v1``
hand-off JSON (with ``kpis`` / ``demand`` / ``optimizer``).

A current plan that was not read from a controller report (e.g. the scenario's own
signal programs) says so in its ``provenance``: ``current_plan_note`` replaces the
cover's "read from the city's controller timing report" sentence (and drops the
cover's mention of the source report) and ``current_plan_kpi_note`` (read from the
run JSON's ``input_plan``) the KPI page's closing line. Without them the report reads
as before.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

# --------------------------------------------------------------------------
# Page geometry (US Letter, mirroring the source report's margins)
# --------------------------------------------------------------------------
PAGE_W, PAGE_H = letter          # 612 x 792 pt
LEFT = 24.0
RIGHT = PAGE_W - 24.0
CENTER = PAGE_W / 2.0
TITLE_Y = PAGE_H - 46.0
BODY_TOP = PAGE_H - 74.0
FOOTER_Y = 40.0
ROW_H = 12.0

# Right-align anchors for the eight NEMA phase columns.
PHASE_X = [130.0 + 62.0 * i for i in range(8)]

FONT = "Helvetica"
FONT_B = "Helvetica-Bold"
SZ_TITLE = 11.0
SZ_BODY = 7.0
SZ_FOOT = 7.5

DOW_TOKENS = [
    ("Sun", "SU"), ("Mon", "M"), ("Tue", "T"), ("Wed", "W"),
    ("Thu", "TR"), ("Fri", "F"), ("Sat", "SA"),
]
STREET_SUFFIXES = ("STREET", "ST", "AVENUE", "AVE", "BOULEVARD", "BLVD",
                   "ROAD", "RD", "DRIVE", "DR", "WAY", "LANE", "LN")

# What the report says about the cycle when the optimizer did not move it (or the
# run does not say). Cycles closer than the tolerance are the same cycle; signals
# further apart run different cycles.
CYCLE_KEPT = "Cycle length and phase structure were kept"
CYCLE_TOLERANCE_S = 0.5


# --------------------------------------------------------------------------
# Small formatting helpers
# --------------------------------------------------------------------------
def fmt_num(value: Any, decimals: int = 1) -> str:
    """Format a number the way the controller report does (ints stay ints)."""
    if value is None or value == "":
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if num.is_integer():
        return str(int(num))
    return f"{num:.{decimals}f}"


def _finite(value: Any) -> float | None:
    """``value`` as a float when it is a finite number (a bool is not), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _fmt_delay(value: Any, decimals: int = 2) -> str:
    """A delay in s/veh, or ``n/a`` when there is no usable value."""
    number = _finite(value)
    return "n/a" if number is None else f"{number:.{decimals}f}"


def _cycle_span(timing: Any) -> tuple[float, float] | None:
    """The shortest and longest per-signal cycle of a ``timing`` block, or None.

    ``timing["cycle_s"]`` maps each signal to its cycle; entries that are not
    finite numbers are ignored.
    """
    cycles = timing.get("cycle_s") if isinstance(timing, dict) else None
    if not isinstance(cycles, dict):
        return None
    values = [cycle for cycle in map(_finite, cycles.values()) if cycle is not None]
    return (min(values), max(values)) if values else None


def _cycle_mixed(span: tuple[float, float]) -> bool:
    """True when one side's signals do not share a cycle."""
    return span[1] - span[0] > CYCLE_TOLERANCE_S


def _cycle_text(span: tuple[float, float]) -> str:
    """One side's cycle: ``140 s``, or ``60–280 s (per signal)`` for mixed ones."""
    if _cycle_mixed(span):
        return f"{span[0]:g}–{span[1]:g} s (per signal)"
    return f"{span[1]:g} s"


def fmt_fixed(value: Any, decimals: int = 1) -> str:
    """Format with a fixed decimal count — change intervals print as ``3.0``."""
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def phase_mask(phases: Sequence[int]) -> str:
    """Render a phase set as the report's ``-2---6--`` bit mask."""
    marked = {int(p) for p in phases or []}
    return "".join(str(p) if p in marked else "-" for p in range(1, 9))


def dow_mask(days: Sequence[str]) -> str:
    """Render weekdays as the report's ``-- M T W TR F --`` mask."""
    active = {str(d)[:3].title() for d in days or []}
    out = [tok if name in active else "-" * len(tok) for name, tok in DOW_TOKENS]
    return " ".join(out)


def street_short(name: str) -> str:
    """``MAIN ST`` -> ``MAIN`` (the report drops the suffix)."""
    parts = name.upper().replace(".", "").split()
    while parts and parts[-1] in STREET_SUFFIXES:
        parts.pop()
    return " ".join(parts) if parts else name.upper()


def split_intersection_name(name: str) -> tuple[str, str]:
    """Split ``A ST @ B ST`` (or ``A & B`` / ``A / B``) into its two streets."""
    parts = re.split(r"\s*[@&/]\s*|\s+at\s+|\s+AT\s+", name or "", maxsplit=1)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return (parts[0] if parts else name or ""), (parts[0] if parts else name or "")


def movement_direction(movements: Sequence[str]) -> str:
    """``['EB-left']`` -> ``EBLT``; ``['WB-through','WB-right']`` -> ``WB``."""
    dirs, turns = [], set()
    for mv in movements or []:
        head, _, tail = str(mv).partition("-")
        head = head.strip().upper()
        if head and head not in dirs:
            dirs.append(head)
        if tail:
            turns.add(tail.strip().lower())
    if not dirs:
        return ""
    base = dirs[0] if len(dirs) == 1 else "/".join(dirs)
    if "through" in turns or not turns:
        return base
    if turns == {"right"}:
        return base + "RT"
    if "left" in turns:
        return base + "LT"
    return base


def is_through_phase(movements: Sequence[str]) -> bool:
    """True when the phase serves a through movement (gets a street name)."""
    return any("through" in str(mv).lower() for mv in movements or [])


def _nema_number(value: Any) -> int | None:
    """A row's ``nema_phase`` as a NEMA phase number 1-8, else None (a stage without one)."""
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 8 else None


def optimized_phase_rows(
    opt_entry: dict[str, Any] | None,
) -> tuple[dict[int, dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    """Split an optimized intersection's rows into NEMA-numbered ones and stages.

    A static plan's export carries ``nema_phase: null``: such rows are stages,
    labelled ``S1``, ``S2``... in running order. Returns ``({phase number: row},
    [(label, row), ...])``.
    """
    numbered: dict[int, dict[str, Any]] = {}
    stages: list[tuple[str, dict[str, Any]]] = []
    for row in (opt_entry or {}).get("phases") or []:
        number = _nema_number(row.get("nema_phase"))
        if number is None:
            stages.append((f"S{len(stages) + 1}", row))
        else:
            numbered[number] = row
    return numbered, stages


def _running_order_rows(opt_entry: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every row in running order: ``Ph n`` for a numbered one, ``S1``... for a stage."""
    labelled, stage = [], 0
    for row in opt_entry.get("phases") or []:
        number = _nema_number(row.get("nema_phase"))
        if number is None:
            stage += 1
            labelled.append((f"S{stage}", row))
        else:
            labelled.append((f"Ph {number}", row))
    return labelled


def movement_labels(movements: Sequence[str]) -> str:
    """``['WB-right', 'WB-through', 'EB-left']`` -> ``WB-right/through, EB-left``."""
    turns_by_approach: dict[str, list[str]] = {}
    for movement in movements or []:
        head, _, tail = str(movement).partition("-")
        turns = turns_by_approach.setdefault(head.strip(), [])
        if tail.strip() and tail.strip() not in turns:
            turns.append(tail.strip())
    return ", ".join(
        f"{approach}-{'/'.join(turns)}" if turns else approach
        for approach, turns in turns_by_approach.items()
    )


STAGE_HEADERS = ("Stage", "Movements", "Split", "Green", "Yellow", "Red Clear")


def stage_table_rows(stages: Sequence[tuple[str, dict[str, Any]]]) -> list[list[str]]:
    """One row of :data:`STAGE_HEADERS` cells per ``(label, row)``."""
    return [
        [
            label,
            movement_labels(row.get("movements") or []) or "-",
            fmt_num(row.get("split_s")),
            fmt_num(row.get("green_s")),
            fmt_fixed(row.get("yellow_s")),
            fmt_fixed(row.get("all_red_s")),
        ]
        for label, row in stages
    ]


def wrap_text(canvas: Canvas, text: str, width: float, font: str,
              size: float) -> list[str]:
    """Greedy word wrap against reportlab's own string metrics."""
    lines: list[str] = []
    current = ""
    for word in (text or "").split():
        probe = f"{current} {word}".strip()
        if current and canvas.stringWidth(probe, font, size) > width:
            lines.append(current)
            current = word
        else:
            current = probe
    if current:
        lines.append(current)
    return lines


# --------------------------------------------------------------------------
# Optimized-result normalization
# --------------------------------------------------------------------------
class OptimizedResult:
    """Normalized view over a run JSON or a bare nema_timing.v1 hand-off JSON."""

    def __init__(self, doc: dict[str, Any], source: str = "",
                 demand_window: str | None = None) -> None:
        self.source = source
        self.raw = doc
        if doc.get("schema") == "terasim.nema_timing.v1":
            timing, run = doc, {}
        else:
            timing = doc.get("optimized_nema_plan") or {}
            run = doc
        self.timing = timing
        self.intersections = list(timing.get("intersections") or [])
        result = run.get("result") or {}
        kpis = doc.get("kpis") or {}
        self.baseline_moe = (result.get("baseline") or {}).get("moe") or \
            kpis.get("baseline") or {}
        self.optimized_moe = (result.get("best") or {}).get("moe") or \
            kpis.get("optimized") or {}
        self.baseline_score = (result.get("baseline") or {}).get("score")
        self.optimized_score = (result.get("best") or {}).get("score")
        if self.optimized_score is None:
            self.optimized_score = kpis.get("score_best")
        if self.baseline_score is None and self.optimized_score is not None:
            self.baseline_score = 1.0
        # Each side's cycles come from the plan timing the run carries: the MCP
        # result's ``kpis.<side>.timing`` (what the artifact store writes), else the
        # optimizer's own ``result.baseline.timing`` / ``result.best.timing``.
        self.baseline_cycles = self._side_span(kpis.get("baseline"),
                                               result.get("baseline"))
        self.optimized_cycles = self._side_span(kpis.get("optimized"),
                                                result.get("best"))
        config = result.get("config") or {}
        optimizer = doc.get("optimizer") or {}
        self.n_trials = config.get("max_trials") or optimizer.get("trials") or \
            result.get("n_trials")
        self.sim_end_s = config.get("sim_end") or optimizer.get("sim_end_s")
        self.kpi_weights = config.get("kpi_weights") or optimizer.get("kpi_weights")
        self.optimizer_mode = optimizer.get("mode") or config.get("optimizer")
        self.run_id = run.get("run_id") or optimizer.get("run_id")
        demand = doc.get("demand") or {}
        self.demand_window = demand_window or demand.get("tmc_windows_utc") or ""
        self.demand_source = demand.get("source") or ""
        self.tmc_input = config.get("tmc_json") or ""
        self.generated_at = timing.get("generated_at") or doc.get("generated_at") or ""
        self.handoff_ready = timing.get("handoff_ready")
        if self.handoff_ready is None:
            self.handoff_ready = (result.get("nema") or {}).get("handoff_ready")
        # What the KPI page says the "current" column is, when the current plan is
        # not the city's controller report (see the module docstring).
        input_plan = run.get("input_plan") if isinstance(run.get("input_plan"), dict) else {}
        provenance = input_plan.get("provenance")
        self.current_plan_kpi_note = (
            provenance.get("current_plan_kpi_note") if isinstance(provenance, dict) else None
        )
        # intersection_id <-> tls_id, so a corridor plan can be matched up.
        self.tls_by_intersection = {
            entry.get("intersection_id"): entry.get("tls_id")
            for entry in (result.get("resolved_corridor") or [])
            if entry.get("intersection_id")
        }

    @staticmethod
    def _side_span(*holders: Any) -> tuple[float, float] | None:
        """The cycle span of the first holder whose ``timing`` block carries one."""
        for holder in holders:
            span = _cycle_span(
                holder.get("timing") if isinstance(holder, dict) else None)
            if span is not None:
                return span
        return None

    @property
    def cycle_clause(self) -> str:
        """What the run did to the cycle length, as a clause of report prose.

        Signals within ``CYCLE_TOLERANCE_S`` of each other share one cycle (the
        longest shows). A side whose signals run different cycles reads as a
        range, never as its longest cycle alone. An unchanged cycle (or, for two
        ranges, both ends unchanged), or a run that reports none (the optimizer
        used to freeze it), reads as kept.
        """
        before, after = self.baseline_cycles, self.optimized_cycles
        if before is None or after is None:
            return CYCLE_KEPT
        if _cycle_mixed(before) and _cycle_mixed(after):
            was, now = (f"{span[0]:g}–{span[1]:g}" for span in (before, after))
            if all(abs(x - y) < CYCLE_TOLERANCE_S for x, y in zip(before, after)):
                return f"Cycle lengths were kept ({was} s per signal)"
            return f"Cycle lengths changed from {was} s to {now} s (per signal)"
        if (not _cycle_mixed(before) and not _cycle_mixed(after)
                and abs(before[1] - after[1]) < CYCLE_TOLERANCE_S):
            return CYCLE_KEPT
        return (f"Cycle length changed from {_cycle_text(before)} to "
                f"{_cycle_text(after)}")

    @property
    def junction_rows(self) -> list[tuple[str, str, str]]:
        """``(signal, current, optimized)`` edge-based delay cells, one row per signal.

        Read from each side's ``junction_delay_s`` and sorted by signal id, in
        s/veh to one decimal; a signal one side has no value for reads ``n/a``.
        Empty when neither side reports a table.
        """
        before, after = (
            moe.get("junction_delay_s") if isinstance(moe, dict) else None
            for moe in (self.baseline_moe, self.optimized_moe)
        )
        before = before if isinstance(before, dict) else {}
        after = after if isinstance(after, dict) else {}
        return [
            (str(tls_id), _fmt_delay(before.get(tls_id), 1),
             _fmt_delay(after.get(tls_id), 1))
            for tls_id in sorted(set(before) | set(after), key=str)
        ]

    def for_intersection(self, intersection: dict[str, Any],
                         index: int) -> dict[str, Any] | None:
        """Find the optimized entry belonging to one plan intersection."""
        if not self.intersections:
            return None
        iid = intersection.get("intersection_id")
        for entry in self.intersections:
            if entry.get("intersection_id") and entry["intersection_id"] == iid:
                return entry
        tls = self.tls_by_intersection.get(iid)
        if tls:
            for entry in self.intersections:
                if entry.get("tls_id") == tls:
                    return entry
        if len(self.intersections) == 1 and index == 0:
            return self.intersections[0]
        if index < len(self.intersections):
            return self.intersections[index]
        return None


# --------------------------------------------------------------------------
# Report writer
# --------------------------------------------------------------------------
class TimingReport:
    """Collects page painters, then renders them once the page count is known."""

    def __init__(self, footer_name: str, stamp: str, *, footer_start: int = 1,
                footer_total: int | None = None,
                footer_label: str | None = None) -> None:
        self.footer_name = footer_name
        self.stamp = stamp
        self.footer_start = footer_start
        self.footer_total = footer_total
        self.footer_label = footer_label
        self.pages: list[Callable[[Canvas], None]] = []

    # -- primitives --------------------------------------------------------
    @staticmethod
    def text(c: Canvas, x: float, y: float, s: str, *, font: str = FONT,
             size: float = SZ_BODY, align: str = "left") -> None:
        c.setFont(font, size)
        if align == "right":
            c.drawRightString(x, y, s)
        elif align == "center":
            c.drawCentredString(x, y, s)
        else:
            c.drawString(x, y, s)

    def title(self, c: Canvas, text: str) -> None:
        self.text(c, CENTER, TITLE_Y, text, font=FONT_B, size=SZ_TITLE,
                  align="center")

    def phase_row(self, c: Canvas, y: float, label: str,
                  values: Sequence[Any], *, bold: bool = False) -> float:
        """One label + eight right-aligned phase-column values."""
        font = FONT_B if bold else FONT
        self.text(c, LEFT, y, label, font=font)
        for idx, value in enumerate(values[:8]):
            if value not in (None, ""):
                self.text(c, PHASE_X[idx], y, str(value), font=font, align="right")
        return y - ROW_H

    def table(self, c: Canvas, y: float, headers: Sequence[str],
              rows: Sequence[Sequence[str]],
              cols: Sequence[tuple[float, str]]) -> float:
        """A bold header row, a rule, then the rows; returns the y below the last.

        ``cols`` is one ``(x, align)`` per column; a row or header shorter than
        ``cols`` leaves the remaining columns empty.
        """
        for (x, align), label in zip(cols, headers):
            self.text(c, x, y, label, font=FONT_B, size=8.0, align=align)
        y -= 6.0
        c.setLineWidth(0.5)
        c.line(LEFT + 8, y, RIGHT - 8, y)
        y -= 12.0
        for row in rows:
            for (x, align), value in zip(cols, row):
                self.text(c, x, y, value, size=8.0, align=align)
            y -= 13.0
        return y

    def stage_table(self, c: Canvas, y: float,
                    stages: Sequence[tuple[str, dict[str, Any]]]) -> float:
        """The :data:`STAGE_HEADERS` table; long movement lists wrap in their column."""
        cols = ((LEFT + 8, "left"), (LEFT + 52, "left"), (430.0, "right"),
                (480.0, "right"), (530.0, "right"), (RIGHT - 8, "right"))
        width = cols[2][0] - 40.0 - cols[1][0]
        for (x, align), label in zip(cols, STAGE_HEADERS):
            self.text(c, x, y, label, font=FONT_B, size=8.0, align=align)
        y -= 6.0
        c.setLineWidth(0.5)
        c.line(LEFT + 8, y, RIGHT - 8, y)
        y -= 12.0
        for cells in stage_table_rows(stages):
            lines = wrap_text(c, cells[1], width, FONT, 8.0) or [""]
            for (x, align), value in zip(cols, [cells[0], lines[0], *cells[2:]]):
                self.text(c, x, y, value, size=8.0, align=align)
            y -= 13.0
            for line in lines[1:]:
                self.text(c, cols[1][0], y, line, size=8.0)
                y -= 13.0
        return y

    def footer(self, c: Canvas, page: int, total: int) -> None:
        at_form = self.footer_name.replace(" @ ", " @")
        amp_form = self.footer_name.replace(" @ ", " & ").replace("@", "&")
        c.setFont(FONT, SZ_FOOT)
        c.drawString(LEFT, FOOTER_Y, self.stamp)
        c.drawCentredString(CENTER, FOOTER_Y, f"{at_form} / {amp_form} -")
        right = self.footer_label or f"Page {page} of {total}"
        c.drawRightString(RIGHT, FOOTER_Y, right)

    # -- output ------------------------------------------------------------
    def add(self, painter: Callable[[Canvas], None]) -> None:
        self.pages.append(painter)

    def save(self, out_path: Path, title: str) -> int:
        if not self.pages:
            raise SystemExit("nothing to render for the requested --sections "
                             "(the kpi section needs --optimized)")
        c = Canvas(str(out_path), pagesize=letter)
        c.setTitle(title)
        c.setAuthor("TeraSim")
        rendered = len(self.pages)
        total = self.footer_total if self.footer_total is not None else rendered
        for offset, painter in enumerate(self.pages):
            painter(c)
            self.footer(c, self.footer_start + offset, total)
            c.showPage()
        c.save()
        return rendered


# --------------------------------------------------------------------------
# Page painters
# --------------------------------------------------------------------------
def paint_cover(report: TimingReport, plan: dict[str, Any],
                intersection: dict[str, Any], name: str,
                optimized: OptimizedResult | None, plan_path: Path,
                opt_path: Path | None) -> Callable[[Canvas], None]:
    """Cover page: who/what/where and a short 'how this was produced'."""
    controller = intersection.get("controller") or {}
    provenance = plan.get("provenance") or {}
    sources = provenance.get("source_files") or []
    mode = "OPTIMIZED BY TERASIM" if optimized else "CURRENT PLAN"
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    facts: list[tuple[str, str]] = []
    if controller.get("id"):
        facts.append(("Controller", str(controller["id"])))
    if controller.get("channel"):
        facts.append(("Channel", str(controller["channel"])))
    if controller.get("system"):
        facts.append(("System", str(controller["system"])))
    if controller.get("type"):
        facts.append(("Controller type", str(controller["type"])))
    if controller.get("report_date"):
        facts.append(("Source report date", str(controller["report_date"])))
    if plan.get("corridor_id"):
        facts.append(("Corridor", str(plan["corridor_id"])))
    if intersection.get("intersection_id"):
        facts.append(("Intersection id", str(intersection["intersection_id"])))
    if intersection.get("lat") is not None and intersection.get("lon") is not None:
        facts.append(("Location", f"{intersection['lat']:.5f}, "
                                  f"{intersection['lon']:.5f}"))
    if sources:
        facts.append(("Source PDF", ", ".join(str(s) for s in sources)))
    facts.append(("Plan JSON", plan_path.name))
    if opt_path is not None:
        facts.append(("Optimization JSON", opt_path.name))
    facts.append(("Plan schema", str(plan.get("schema", ""))))
    if optimized is not None and optimized.timing.get("schema"):
        facts.append(("Timing schema", str(optimized.timing["schema"])))
    facts.append(("Generated", generated))

    story: list[str] = []
    if optimized is not None:
        bits = [provenance.get("current_plan_note")
                or "The current plan below was read from the city's controller "
                   "timing report and re-timed by TeraSim."]
        if optimized.demand_window:
            bits.append(f"Demand came from the TMC window {optimized.demand_window}.")
        if optimized.demand_source:
            bits.append(f"Counts source: {optimized.demand_source}.")
        if optimized.optimizer_mode:
            bits.append(f"Optimizer: {optimized.optimizer_mode}.")
        if optimized.n_trials:
            bits.append(f"Search budget: {optimized.n_trials} trials.")
        if optimized.sim_end_s:
            bits.append(f"Each trial simulated {fmt_num(optimized.sim_end_s)} s "
                        "in TeraSim/SUMO.")
        if optimized.kpi_weights:
            weights = ", ".join(f"{k} {v}" for k, v in optimized.kpi_weights.items())
            bits.append(f"Objective weights: {weights}.")
        if provenance.get("current_plan_note"):
            # Not a controller report: there is no source report to speak of.
            bits.append(f"{optimized.cycle_clause}; splits and offset were re-timed.")
        else:
            bits.append(f"{optimized.cycle_clause}; splits and offset were "
                        "re-timed. Every other controller parameter in the source "
                        "report is unchanged.")
        story = bits
    else:
        story = ["Rendered from the plan JSON extracted from the city's "
                 "controller timing report. No optimization was applied — this "
                 "is the current timing as read."]
    paragraph = " ".join(story)

    def painter(c: Canvas) -> None:
        y = PAGE_H - 150.0
        report.text(c, CENTER, y, name.upper(), font=FONT_B, size=16.0,
                    align="center")
        y -= 26.0
        report.text(c, CENTER, y, "SIGNAL TIMING REPORT", font=FONT_B,
                    size=12.0, align="center")
        y -= 18.0
        report.text(c, CENTER, y, mode, font=FONT_B, size=11.0, align="center")
        y -= 34.0
        c.setLineWidth(0.5)
        c.line(LEFT + 60, y, RIGHT - 60, y)
        y -= 24.0
        for label, value in facts:
            report.text(c, 250.0, y, label, size=8.0, align="right")
            report.text(c, 264.0, y, value, size=8.0)
            y -= 13.0
        y -= 18.0
        report.text(c, LEFT + 36, y, "How this was produced", font=FONT_B,
                    size=8.5)
        y -= 14.0
        for line in wrap_text(c, paragraph, RIGHT - LEFT - 72, FONT, 8.0):
            report.text(c, LEFT + 36, y, line, size=8.0)
            y -= 11.5

    return painter


def paint_phase_timing(report: TimingReport, intersection: dict[str, Any],
                       name: str) -> Callable[[Canvas], None]:
    """``(1-1) PHASE TIMING`` — the per-phase base timing table."""
    phases = {int(p["phase"]): p for p in intersection.get("phases") or []}
    rings = intersection.get("rings") or {}
    street_a, street_b = split_intersection_name(name)
    corridor_hint = street_short(street_b)
    cross_hint = street_short(street_a)

    def street_for(movements: Sequence[str]) -> str:
        if not is_through_phase(movements):
            return ""
        direction = movement_direction(movements)
        if direction.startswith(("EB", "WB")):
            return corridor_hint
        if direction.startswith(("NB", "SB")):
            return cross_hint
        return ""

    def col(key: Callable[[dict[str, Any]], Any]) -> list[str]:
        out = []
        for ph in range(1, 9):
            data = phases.get(ph)
            out.append("" if data is None else key(data))
        return out

    def painter(c: Canvas) -> None:
        report.title(c, "(1-1) PHASE TIMING")
        y = BODY_TOP
        y = report.phase_row(c, y, "Phase", [str(p) for p in range(1, 9)],
                             bold=True)
        y = report.phase_row(c, y, "Name",
                             col(lambda d: street_for(d.get("movements", []))))
        y = report.phase_row(c, y, "Direction",
                             col(lambda d: movement_direction(d.get("movements", []))))
        y = report.phase_row(c, y, "Min Green",
                             col(lambda d: fmt_num(d.get("min_green_s"))))
        y = report.phase_row(c, y, "Yellow",
                             col(lambda d: fmt_fixed(d.get("yellow_s"))))
        y = report.phase_row(c, y, "Red Clear",
                             col(lambda d: fmt_fixed(d.get("all_red_s"))))
        y -= ROW_H
        for label, value in (
            ("Coord Phs", phase_mask(rings.get("coordinated_phases"))),
            ("Ring 1", phase_mask(rings.get("ring1"))),
            ("Ring 2", phase_mask(rings.get("ring2"))),
            ("Barrier", phase_mask(rings.get("barrier_phases"))),
            ("Barrier 2", phase_mask(rings.get("barrier2_phases"))),
        ):
            report.text(c, LEFT, y, label)
            report.text(c, PHASE_X[1], y, value)
            y -= ROW_H
        y -= ROW_H
        report.text(c, LEFT, y, "Values read from the source controller report; "
                                "fields not modelled by terasim.nema_plan.v1 "
                                "(Veh Ext, Max 1/2/3, Walk, Ped Clear, ...) are "
                                "unchanged and omitted here.", size=6.5)

    return painter


def paint_pattern(report: TimingReport, intersection: dict[str, Any],
                  pattern: dict[str, Any], index: int, name: str,
                  opt_entry: dict[str, Any] | None,
                  optimized_mode: bool,
                  cycle_clause: str = CYCLE_KEPT) -> Callable[[Canvas], None]:
    """``(3-2) COORDINATION PATTERNS n`` — one page per coordination pattern.

    ``cycle_clause`` (``OptimizedResult.cycle_clause``) says what happened to
    the cycle length on the optimized pattern's closing note.
    """
    phases = {int(p["phase"]): p for p in intersection.get("phases") or []}
    rings = intersection.get("rings") or {}
    splits = pattern.get("splits") or {}
    street_a, street_b = split_intersection_name(name)
    corridor_hint = street_short(street_b)
    cross_hint = street_short(street_a)
    coord = pattern.get("coordinated_phases") or rings.get("coordinated_phases")
    description = (pattern.get("description")
                   or str(pattern.get("pattern_id", "")).upper())
    # Rows without a NEMA phase number cannot go into the phase columns: they
    # are listed as stages (S1, S2...) under them.
    opt_phases, opt_stages = optimized_phase_rows(opt_entry)
    is_optimized = bool(opt_entry)

    def street_for(movements: Sequence[str]) -> str:
        if not is_through_phase(movements):
            return ""
        direction = movement_direction(movements)
        if direction.startswith(("EB", "WB")):
            return corridor_hint
        if direction.startswith(("NB", "SB")):
            return cross_hint
        return ""

    def painter(c: Canvas) -> None:
        suffix = "  (OPTIMIZED)" if is_optimized else ""
        report.title(c, f"(3-2) COORDINATION PATTERNS {index}{suffix}")
        y = BODY_TOP
        report.text(c, LEFT, y, "Description")
        for line in wrap_text(c, description, RIGHT - 110.0, FONT, SZ_BODY):
            report.text(c, 100.0, y, line)
            y -= ROW_H
        report.text(c, LEFT, y, "Cycle")
        report.text(c, 130.0, y, fmt_num(pattern.get("cycle_s")), align="right")
        report.text(c, 170.0, y, "Ofs")
        report.text(c, 240.0, y, fmt_num(pattern.get("offset_s")), align="right")
        if is_optimized:
            opt_cycle = fmt_num(opt_entry.get("cycle_s"))
            opt_offset = fmt_num(opt_entry.get("offset_s"))
            report.text(c, 280.0, y, f"(optimized: Cycle {opt_cycle}   "
                                     f"Ofs {opt_offset})")
        y -= 2 * ROW_H

        y = report.phase_row(c, y, "Phase", [str(p) for p in range(1, 9)],
                             bold=True)
        names, dirs = [], []
        for ph in range(1, 9):
            movements = (phases.get(ph) or {}).get("movements", [])
            names.append(street_for(movements))
            dirs.append(movement_direction(movements))
        y = report.phase_row(c, y, "Name", names)
        y = report.phase_row(c, y, "Direction", dirs)

        cur = [fmt_num(splits.get(str(ph), splits.get(ph))) for ph in range(1, 9)]
        if is_optimized:
            y = report.phase_row(c, y, "Split (current)", cur)
            y = report.phase_row(c, y, "Split (optimized)",
                                 [fmt_num((opt_phases.get(ph) or {}).get("split_s"))
                                  for ph in range(1, 9)], bold=True)
            y = report.phase_row(c, y, "Green (optimized)",
                                 [fmt_num((opt_phases.get(ph) or {}).get("green_s"))
                                  for ph in range(1, 9)])
            y = report.phase_row(c, y, "Yellow",
                                 [fmt_fixed((opt_phases.get(ph) or {}).get("yellow_s")
                                            or (phases.get(ph) or {}).get("yellow_s"))
                                  for ph in range(1, 9)])
            y = report.phase_row(c, y, "Red Clear",
                                 [fmt_fixed((opt_phases.get(ph) or {}).get("all_red_s")
                                            or (phases.get(ph) or {}).get("all_red_s"))
                                  for ph in range(1, 9)])
        else:
            y = report.phase_row(c, y, "Split", cur)
        if is_optimized and opt_stages:
            y -= ROW_H
            report.text(c, LEFT, y, "Optimized stages without a NEMA phase number",
                        font=FONT_B)
            y -= ROW_H
            y = report.stage_table(c, y, opt_stages)

        y -= ROW_H
        report.text(c, LEFT, y, "Coord Phs")
        report.text(c, PHASE_X[1], y, phase_mask(coord))
        y -= 2 * ROW_H

        if is_optimized:
            windows = opt_entry.get("valid_windows") or []
            for window in windows:
                days = " ".join(str(d) for d in window.get("days") or [])
                report.text(c, LEFT, y, f"Valid window   {days}   "
                                        f"{window.get('start', '')}-"
                                        f"{window.get('end', '')}", size=6.5)
                y -= ROW_H
            ring1 = sum(float((opt_phases.get(p) or {}).get("split_s") or 0)
                        for p in (rings.get("ring1") or [1, 2, 3, 4]))
            ring2 = sum(float((opt_phases.get(p) or {}).get("split_s") or 0)
                        for p in (rings.get("ring2") or [5, 6, 7, 8]))
            report.text(c, LEFT, y, f"Optimized ring sums   ring 1 "
                                    f"{fmt_num(ring1)} s   ring 2 "
                                    f"{fmt_num(ring2)} s   cycle "
                                    f"{fmt_num(opt_entry.get('cycle_s'))} s",
                        size=6.5)
            y -= ROW_H
            report.text(c, LEFT, y, f"{cycle_clause}; splits and offset "
                                    "re-timed by TeraSim; rings and change "
                                    "intervals are unchanged.", size=6.5)
        elif optimized_mode:
            report.text(c, LEFT, y, "Pattern unchanged by this optimization run "
                                    "(printed as read from the source report).",
                        size=6.5)
        else:
            report.text(c, LEFT, y, "Printed as read from the source controller "
                                    "report.", size=6.5)

    return painter


def paint_optimized_stages(report: TimingReport, opt_entry: dict[str, Any],
                           cycle_s: Any,
                           cycle_clause: str = CYCLE_KEPT) -> Callable[[Canvas], None]:
    """``OPTIMIZED TIMING (STAGES)`` — an optimized plan without a pattern of the city's.

    A static plan (generated by TeraSim, or the scenario's own programs) is exported
    as stages: green intervals with the movements they serve, usually without NEMA
    phase numbers. They cannot go into the pattern pages' phase columns, so they are
    listed here in running order (``S1``, ``S2``... or ``Ph n`` when numbered), with
    the cycle and the offset.
    """
    rows = _running_order_rows(opt_entry)

    def painter(c: Canvas) -> None:
        report.title(c, "OPTIMIZED TIMING (STAGES)")
        y = BODY_TOP
        report.text(c, LEFT, y, "Description")
        report.text(c, 100.0, y, str(opt_entry.get("description") or ""))
        y -= ROW_H
        if opt_entry.get("tls_id"):
            report.text(c, LEFT, y, "Signal")
            report.text(c, 100.0, y, str(opt_entry["tls_id"]))
            y -= ROW_H
        report.text(c, LEFT, y, "Cycle")
        report.text(c, 130.0, y, fmt_num(cycle_s), align="right")
        report.text(c, 170.0, y, "Ofs")
        report.text(c, 240.0, y, fmt_num(opt_entry.get("offset_s")), align="right")
        y -= 2 * ROW_H
        y = report.stage_table(c, y, rows)
        y -= ROW_H
        for line in wrap_text(c, "Each stage is one green interval, in running order, "
                                 "with the movements it serves. Stages without a NEMA "
                                 "phase number must be mapped onto the controller's "
                                 "phases before deployment.",
                              RIGHT - LEFT, FONT, 6.5):
            report.text(c, LEFT, y, line, size=6.5)
            y -= ROW_H
        for window in opt_entry.get("valid_windows") or []:
            days = " ".join(str(d) for d in window.get("days") or [])
            report.text(c, LEFT, y, f"Valid window   {days}   "
                                    f"{window.get('start', '')}-"
                                    f"{window.get('end', '')}", size=6.5)
            y -= ROW_H
        report.text(c, LEFT, y, f"{cycle_clause}; stage splits and offset set by "
                                "TeraSim.", size=6.5)

    return painter


def paint_tod(report: TimingReport, intersection: dict[str, Any],
              pattern_index: dict[str, int]) -> Callable[[Canvas], None]:
    """``(5-1) TOD PATTERNS`` — the time-of-day schedule table."""
    schedule = list(intersection.get("schedule") or [])
    columns = [
        ("Event", LEFT + 12.0, "left"),
        ("Time", LEFT + 60.0, "left"),
        ("DOW", LEFT + 120.0, "left"),
        ("Pattern", LEFT + 300.0, "right"),
        ("Mode", LEFT + 380.0, "right"),
    ]

    def painter(c: Canvas) -> None:
        report.title(c, "(5-1) TOD PATTERNS")
        y = BODY_TOP
        for label, x, align in columns:
            report.text(c, x, y, label, font=FONT_B, align=align)
        y -= 1.5 * ROW_H
        for event, entry in enumerate(schedule, start=1):
            pid = str(entry.get("pattern_id", ""))
            index = pattern_index.get(pid)
            pattern_cell = str(index) if index else "Free"
            mode = "Sched" if index else "Free"
            cells = [str(event), str(entry.get("start", "")),
                     dow_mask(entry.get("days")), pattern_cell, mode]
            for (_, x, align), value in zip(columns, cells):
                report.text(c, x, y, value, align=align)
            y -= ROW_H
        y -= ROW_H
        report.text(c, LEFT, y, "Pattern numbers index the "
                                "(3-2) COORDINATION PATTERNS pages of this "
                                "report; Free means no coordination.", size=6.5)

    return painter


def paint_kpi(report: TimingReport,
              optimized: OptimizedResult) -> Callable[[Canvas], None]:
    """``KPI COMPARISON`` — baseline vs optimized, as simulated."""
    base = optimized.baseline_moe or {}
    best = optimized.optimized_moe or {}

    def delta(key: str, decimals: int = 2, percent: bool = True) -> str:
        b, o = base.get(key), best.get(key)
        if b in (None, "") or o in (None, ""):
            return ""
        diff = float(o) - float(b)
        text = f"{diff:+.{decimals}f}"
        if percent and float(b):
            text += f"  ({diff / float(b) * 100.0:+.1f}%)"
        return text

    # The corridor mean of the edge-based delay is None when no junction had data,
    # and absent from runs that predate it: either way the row says n/a.
    junction_delta = "n/a"
    if (_finite(base.get("corridor_junction_delay_s")) is not None
            and _finite(best.get("corridor_junction_delay_s")) is not None):
        junction_delta = delta("corridor_junction_delay_s")

    rows: list[tuple[str, str, str, str]] = [
        ("Control delay (s/veh)",
         f"{float(base.get('control_delay_s_per_veh', 0)):.2f}",
         f"{float(best.get('control_delay_s_per_veh', 0)):.2f}",
         delta("control_delay_s_per_veh")),
        ("Stops per vehicle",
         f"{float(base.get('stops_per_veh', 0)):.3f}",
         f"{float(best.get('stops_per_veh', 0)):.3f}",
         delta("stops_per_veh", 3)),
        ("Per-junction delay, edge-based (s/veh)",
         _fmt_delay(base.get("corridor_junction_delay_s")),
         _fmt_delay(best.get("corridor_junction_delay_s")),
         junction_delta),
        ("Vehicles total", str(base.get("vehicles_total", "")),
         str(best.get("vehicles_total", "")), delta("vehicles_total", 0)),
        ("Vehicles completed", str(base.get("vehicles_completed", "")),
         str(best.get("vehicles_completed", "")),
         delta("vehicles_completed", 0)),
        ("Total delay (s)",
         fmt_num(base.get("total_delay_s"), 1), fmt_num(best.get("total_delay_s"), 1),
         delta("total_delay_s", 1)),
        ("Mean time loss (s)",
         fmt_num(base.get("mean_time_loss_s"), 2),
         fmt_num(best.get("mean_time_loss_s"), 2),
         delta("mean_time_loss_s", 2)),
    ]
    if optimized.optimized_score is not None:
        base_score = optimized.baseline_score
        rows.append((
            "Cost score (baseline = 1.000)",
            "" if base_score is None else f"{float(base_score):.3f}",
            f"{float(optimized.optimized_score):.3f}",
            "" if base_score is None else
            f"{float(optimized.optimized_score) - float(base_score):+.3f}",
        ))

    notes: list[tuple[str, str]] = []
    if optimized.demand_window:
        notes.append(("Demand window", optimized.demand_window))
    if optimized.demand_source:
        notes.append(("Demand source", optimized.demand_source))
    if optimized.tmc_input:
        notes.append(("TMC input", str(optimized.tmc_input)))
    if optimized.n_trials:
        notes.append(("Trials", str(optimized.n_trials)))
    if optimized.sim_end_s:
        notes.append(("Simulated per trial", f"{fmt_num(optimized.sim_end_s)} s"))
    if optimized.kpi_weights:
        notes.append(("Objective weights", ", ".join(
            f"{k} {v}" for k, v in optimized.kpi_weights.items())))
    if optimized.optimizer_mode:
        notes.append(("Optimizer", optimized.optimizer_mode))
    if optimized.run_id:
        notes.append(("Run id", str(optimized.run_id)))
    if optimized.generated_at:
        notes.append(("Optimized at", str(optimized.generated_at)))
    notes.append(("handoff_ready", str(bool(optimized.handoff_ready)).lower()))

    cols = [(LEFT + 12.0, "left"), (330.0, "right"), (430.0, "right"),
            (RIGHT - 12.0, "right")]
    junction_rows = optimized.junction_rows

    def painter(c: Canvas) -> None:
        report.title(c, "KPI COMPARISON (TeraSim/SUMO)")
        y = report.table(c, BODY_TOP,
                         ["Measure", "Current", "Optimized", "Change"], rows, cols)
        if junction_rows:
            # Same data as the chat summary's per-signal table; omitted when the
            # run carries no per-signal delay on either side.
            y -= 14.0
            report.text(c, LEFT + 12.0, y, "Per-junction delay (edge-based, s/veh)",
                        font=FONT_B, size=8.0)
            y = report.table(c, y - 14.0, ["Signal", "Current", "Optimized"],
                             junction_rows, cols)
        y -= 14.0
        report.text(c, LEFT + 12.0, y, "Run parameters", font=FONT_B, size=8.0)
        y -= 14.0
        for label, value in notes:
            report.text(c, 200.0, y, label, size=7.5, align="right")
            for line in wrap_text(c, value, RIGHT - 224.0, FONT, 7.5):
                report.text(c, 214.0, y, line, size=7.5)
                y -= 11.0
        y -= 8.0
        report.text(c, LEFT, y, optimized.current_plan_kpi_note
                    or "Measured in TeraSim/SUMO on the demand window "
                       "above; the current plan is simulated with the "
                       "splits and offset read from the city's "
                       "controller report.", size=6.5)

    return painter


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------
SECTION_NAMES = ("cover", "phase", "patterns", "tod", "kpi")


def build_report(plan: dict[str, Any], plan_path: Path,
                 optimized: OptimizedResult | None, opt_path: Path | None,
                 out_path: Path, name_override: str | None,
                 report_date: str | None,
                 sections: Sequence[str] | None = None,
                 footer_start: int = 1, footer_total: int | None = None,
                 footer_label: str | None = None) -> tuple[int, Path]:
    """Assemble the requested sections and write the PDF.

    ``sections`` (default: every section) is any subset of ``SECTION_NAMES``;
    pages are always emitted in the canonical order regardless of the order
    the caller lists them in. Returns (page count, path).
    """
    active = set(sections) if sections is not None else set(SECTION_NAMES)
    unknown = active - set(SECTION_NAMES)
    if unknown:
        raise SystemExit(f"--sections: unknown section(s) {sorted(unknown)}; "
                         f"choose from {','.join(SECTION_NAMES)}")
    intersections = list(plan.get("intersections") or [])
    if not intersections:
        raise SystemExit("plan has no intersections")
    cycle_clause = optimized.cycle_clause if optimized else CYCLE_KEPT
    first = intersections[0]
    name = name_override or first.get("name") or plan.get("corridor_id") or ""
    stamp = _stamp(report_date)
    report = TimingReport(footer_name=name, stamp=stamp, footer_start=footer_start,
                          footer_total=footer_total, footer_label=footer_label)

    if "cover" in active:
        report.add(paint_cover(report, plan, first, name, optimized, plan_path,
                               opt_path))
    for index, intersection in enumerate(intersections):
        section_name = (name if index == 0
                        else intersection.get("name") or name)
        opt_entry = (optimized.for_intersection(intersection, index)
                     if optimized else None)
        opt_pattern_id = (opt_entry or {}).get("pattern_id")
        # A static plan's export names no pattern of the city's: it gets its own
        # stage page rather than matching (or being forced into) one of them.
        static_entry = bool(opt_entry) and opt_pattern_id is None
        if "phase" in active:
            report.add(paint_phase_timing(report, intersection, section_name))
        patterns = list(intersection.get("patterns") or [])
        pattern_index = {str(p.get("pattern_id")): i
                         for i, p in enumerate(patterns, start=1)}
        if "patterns" in active:
            for number, pattern in enumerate(patterns, start=1):
                match = (opt_entry
                         if opt_entry and not static_entry
                         and pattern.get("pattern_id") == opt_pattern_id
                         else None)
                report.add(paint_pattern(report, intersection, pattern, number,
                                         section_name, match,
                                         optimized is not None, cycle_clause))
            if static_entry:
                cycle_s = (opt_entry.get("cycle_s")
                           or (optimized.timing.get("coordination") or {})
                           .get("cycle_length_s"))
                report.add(paint_optimized_stages(report, opt_entry, cycle_s,
                                                  cycle_clause))
        if "tod" in active:
            report.add(paint_tod(report, intersection, pattern_index))
    if optimized is not None and "kpi" in active:
        report.add(paint_kpi(report, optimized))

    mode = "OPTIMIZED" if optimized else "CURRENT PLAN"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pages = report.save(out_path, f"{name} - Signal Timing Report ({mode})")
    return pages, out_path


def _stamp(report_date: str | None) -> str:
    """Footer date/time stamp in the source report's ``2/10/26, 11:36AM`` form."""
    now = dt.datetime.now()
    if report_date:
        try:
            parsed = dt.datetime.fromisoformat(report_date)
            now = parsed if parsed.hour or parsed.minute else parsed.replace(
                hour=now.hour, minute=now.minute)
        except ValueError:
            return report_date
    return (f"{now.month}/{now.day}/{now:%y}, "
            f"{now.hour % 12 or 12}:{now:%M}{now:%p}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a NEMA signal plan as a controller-style timing "
                    "report PDF.")
    parser.add_argument("--plan", required=True, type=Path,
                        help="terasim.nema_plan.v1 JSON")
    parser.add_argument("--optimized", type=Path,
                        help="optimization run JSON or terasim.nema_timing.v1 JSON")
    parser.add_argument("--out", required=True, type=Path, help="output PDF path")
    parser.add_argument("--intersection-name",
                        help="override the intersection name in headers/footers")
    parser.add_argument("--report-date",
                        help="footer date, ISO (2026-09-07) or a literal stamp")
    parser.add_argument("--demand-window",
                        help="TMC demand window text, when the run JSON does "
                             "not carry one (hand-off JSONs do)")
    parser.add_argument("--sections", default=",".join(SECTION_NAMES),
                        help="comma list of sections to render, always emitted "
                             f"in canonical order ({','.join(SECTION_NAMES)}); "
                             "default all")
    parser.add_argument("--footer-start", type=int, default=1,
                        help="first footer page number (default: 1)")
    parser.add_argument("--footer-total", type=int,
                        help="footer page total (default: number of pages "
                             "actually rendered)")
    parser.add_argument("--footer-label",
                        help="footer right-side text; replaces 'Page N of M' "
                             "(e.g. 'Supplement A - TeraSim optimization')")
    args = parser.parse_args(argv)

    sections = [s.strip() for s in args.sections.split(",") if s.strip()]
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if "input_plan" in plan and "intersections" not in plan:
        plan = plan["input_plan"]
    optimized = None
    if args.optimized:
        doc = json.loads(args.optimized.read_text(encoding="utf-8"))
        optimized = OptimizedResult(doc, source=args.optimized.name,
                                    demand_window=args.demand_window)
        if not optimized.intersections:
            raise SystemExit(f"{args.optimized}: no optimized intersections found")

    pages, path = build_report(plan, args.plan, optimized, args.optimized,
                               args.out, args.intersection_name,
                               args.report_date, sections=sections,
                               footer_start=args.footer_start,
                               footer_total=args.footer_total,
                               footer_label=args.footer_label)
    print(f"wrote {path}  ({pages} pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "OptimizedResult",
    "SECTION_NAMES",
    "TimingReport",
    "build_report",
    "dow_mask",
    "fmt_fixed",
    "fmt_num",
    "is_through_phase",
    "main",
    "movement_direction",
    "paint_cover",
    "paint_kpi",
    "paint_pattern",
    "paint_phase_timing",
    "paint_tod",
    "phase_mask",
    "split_intersection_name",
    "street_short",
    "wrap_text",
]
