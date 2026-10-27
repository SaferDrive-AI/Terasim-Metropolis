"""Downloadable artifacts for optimization results.

A chat answer needs URLs, not a nested JSON blob. After an optimize tool
returns ``{"nema_plan": ..., "kpis": ...}``, :class:`ArtifactStore` writes the
run to ``<root>/<artifact_id>/`` as

* ``nema_plan.json``    - the optimized ``terasim.nema_timing.v1`` hand-off
* ``optimization.json`` - the whole result in the run-JSON shape the timing
                          report renderer reads (KPIs, cost weights, targets)
* ``current_plan.json`` - the city's current plan, when the plan store has it
* ``timing_report.pdf`` - the signal-controller timing-report style PDF
                          (current vs optimized), rendered by
                          :mod:`terasim_agent_skill.timing_report`

and returns one public URL per file actually written. The MCP server serves
them at ``GET /files/<artifact_id>/<filename>`` (see
:func:`terasim_agent_skill.server.make_file_endpoint`); the deployment puts a
reverse proxy in front of that path only.

Saving must never fail an optimization: a missing plan, a renderer error or a
broken store volume drops the affected file (and its link) and is logged.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

#: File names in the order they are listed for the reader (the PDF first).
ARTIFACT_FILES = (
    "timing_report.pdf",
    "nema_plan.json",
    "current_plan.json",
    "optimization.json",
)
_LABELS = {
    "timing_report.pdf": "Signal timing report (PDF)",
    "nema_plan.json": "Optimized NEMA plan (JSON)",
    "current_plan.json": "Current plan as read (JSON)",
    "optimization.json": "Full optimization result (JSON)",
}
_MEDIA_TYPES = {".pdf": "application/pdf", ".json": "application/json"}
# One path segment: alphanumeric start, then the characters _artifact_id emits.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,199}$")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")

#: ``(corridor_id, intersections) -> nema_plan.v1 document or None``.
PlanLookup = Callable[[str, list[dict]], "dict | None"]


@dataclass(frozen=True)
class SavedArtifacts:
    """What one :meth:`ArtifactStore.save_optimization` call produced."""

    artifact_id: str
    directory: Path
    #: file name -> public URL, only for files that were actually written
    links: dict[str, str]
    #: the city's current plan (``terasim.nema_plan.v1``) the report compared
    #: against, when the plan store had one; the chat summary reuses it
    current_plan: dict | None = None


def _slug(value: Any, fallback: str = "run") -> str:
    text = _UNSAFE_RE.sub("_", str(value or "")).strip("_.-")
    return text[:40] or fallback


def _artifact_id(intersections: list[dict], now: dt.datetime) -> str:
    ids = [_slug(item.get("intersection_id")) for item in intersections if isinstance(item, dict)]
    head = "+".join(ids) or "run"
    return f"{head}-{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def media_type_for(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _run_document(
    result: dict,
    *,
    artifact_id: str,
    corridor_id: str,
    intersections: list[dict],
    plan: dict | None,
    now: dt.datetime,
) -> dict:
    """The optimize result in the run-JSON shape ``timing_report`` reads.

    The renderer looks for ``result.baseline.moe`` / ``result.best.score`` and
    ``optimized_nema_plan``; the tool result carries the same numbers under
    ``kpis.baseline.metrics`` / ``kpis.optimized.cost``. Both views are kept so
    the file is also a faithful copy of what the tool returned.
    """
    kpis = result.get("kpis") if isinstance(result.get("kpis"), dict) else {}

    def side(label: str) -> dict:
        entry = kpis.get(label) if isinstance(kpis.get(label), dict) else {}
        metrics = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else {}
        return {"score": entry.get("cost"), "moe": metrics}

    weights = kpis.get("cost_weights") if isinstance(kpis.get("cost_weights"), dict) else {}
    document = {
        "schema": "terasim.optimization_run.v1",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": artifact_id,
        "corridor_id": corridor_id,
        "intersections": list(intersections),
        "input_plan": plan,
        "result": {
            "baseline": side("baseline"),
            "best": side("optimized"),
            "config": {"kpi_weights": dict(weights)},
        },
        "optimized_nema_plan": result.get("nema_plan"),
        "kpis": kpis,
    }
    if isinstance(result.get("traffic_model"), str):
        # Named only for a run that was not simulated with the default traffic
        # (a CNDE run), as in the tool result.
        document["traffic_model"] = result["traffic_model"]
    return document


def render_timing_report(
    *,
    plan: dict,
    plan_path: Path,
    optimized_doc: dict,
    opt_path: Path,
    out_path: Path,
) -> None:
    """Default renderer: the signal-controller timing-report style PDF via ``timing_report``.

    Imported lazily so the MCP server (and the plain HTTP client) stay usable
    without reportlab; the store turns an ImportError into a dropped PDF.
    """
    from terasim_agent_skill.timing_report import OptimizedResult, build_report

    optimized = OptimizedResult(optimized_doc, source=opt_path.name)
    if not optimized.intersections:
        raise ValueError("the optimization result carries no intersection timings")
    build_report(plan, plan_path, optimized, opt_path, out_path, None, None)


class PlanStoreLookup:
    """Read the city's current plans straight from the service's plan store.

    The MCP container mounts the service's asset volume read-only, so the
    report can show "current" next to "optimized" without a new service
    endpoint. Per intersection: primary key, else the shared same-junction
    coordinate radius (``PlanStore.find_for``). Intersections without a stored
    plan are left out; no plan at all -> ``None``.
    """

    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)

    def __call__(self, corridor_id: str, intersections: list[dict]) -> dict | None:
        if not self.data_root.exists():
            return None
        from terasim_city.plans.store import PlanStore

        store = PlanStore(self.data_root)
        slices: list[dict] = []
        for item in intersections:
            if not isinstance(item, dict):
                continue
            stored = store.find_for(
                corridor_id, item.get("intersection_id"), item.get("lat"), item.get("lon")
            )
            if stored is not None and isinstance(stored.plan, dict):
                slices.append(stored.plan)
        entries = [
            entry
            for plan in slices
            for entry in (plan.get("intersections") or [])
            if isinstance(entry, dict)
        ]
        if not entries:
            return None
        head = {key: value for key, value in slices[0].items() if key != "intersections"}
        return {**head, "corridor_id": corridor_id, "intersections": entries}


class ArtifactStore:
    """Write optimization artifacts under ``root`` and mint their public URLs."""

    def __init__(
        self,
        root: Path | str,
        public_url: str,
        *,
        plan_lookup: PlanLookup | None = None,
        renderer: Callable[..., Any] | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.root = Path(root)
        self.public_url = public_url.rstrip("/")
        self.plan_lookup = plan_lookup
        self._renderer = renderer if renderer is not None else render_timing_report
        self._clock = clock if clock is not None else _utcnow

    def url_for(self, artifact_id: str, filename: str) -> str:
        return f"{self.public_url}/files/{artifact_id}/{filename}"

    def resolve(self, artifact_id: str, filename: str) -> Path | None:
        """The file behind a download URL, or None for anything not written here."""
        if filename not in ARTIFACT_FILES or not _ID_RE.match(artifact_id or ""):
            return None
        path = self.root / artifact_id / filename
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return None
        if self.root.resolve() not in resolved.parents or not resolved.is_file():
            return None
        return path

    def save_optimization(
        self, result: dict, *, corridor_id: str, intersections: list[dict]
    ) -> SavedArtifacts:
        """Write the run's files; every file written gets a link."""
        now = self._clock()
        artifact_id = _artifact_id(intersections, now)
        directory = self.root / artifact_id
        directory.mkdir(parents=True, exist_ok=False)
        links: dict[str, str] = {}

        def write_json(name: str, document: Any) -> Path:
            path = directory / name
            path.write_text(json.dumps(document, indent=2), encoding="utf-8")
            links[name] = self.url_for(artifact_id, name)
            return path

        plan = self._current_plan(corridor_id, intersections)
        write_json("nema_plan.json", result.get("nema_plan"))
        document = _run_document(
            result,
            artifact_id=artifact_id,
            corridor_id=corridor_id,
            intersections=intersections,
            plan=plan,
            now=now,
        )
        opt_path = write_json("optimization.json", document)
        if plan is not None:
            plan_path = write_json("current_plan.json", plan)
            pdf_path = directory / "timing_report.pdf"
            try:
                self._renderer(
                    plan=plan,
                    plan_path=plan_path,
                    optimized_doc=document,
                    opt_path=opt_path,
                    out_path=pdf_path,
                )
            except (Exception, SystemExit):  # the report is optional; the JSONs are not
                log.warning("timing report for %s not rendered", artifact_id, exc_info=True)
                pdf_path.unlink(missing_ok=True)
            else:
                links["timing_report.pdf"] = self.url_for(artifact_id, "timing_report.pdf")
        return SavedArtifacts(artifact_id, directory, links, plan)

    def _current_plan(self, corridor_id: str, intersections: list[dict]) -> dict | None:
        if self.plan_lookup is None:
            return None
        try:
            plan = self.plan_lookup(corridor_id, intersections)
        except Exception:
            log.warning("current-plan lookup for %s failed", corridor_id, exc_info=True)
            return None
        return plan if isinstance(plan, dict) else None


def downloads_markdown(links: dict[str, str]) -> str:
    """The ``**Downloads**`` block appended to a chat summary: bare URLs, PDF first."""
    lines = ["**Downloads**"]
    lines += [f"- {_LABELS[name]}: {links[name]}" for name in ARTIFACT_FILES if name in links]
    return "\n".join(lines)


__all__ = [
    "ARTIFACT_FILES",
    "ArtifactStore",
    "PlanStoreLookup",
    "SavedArtifacts",
    "downloads_markdown",
    "media_type_for",
    "render_timing_report",
]
