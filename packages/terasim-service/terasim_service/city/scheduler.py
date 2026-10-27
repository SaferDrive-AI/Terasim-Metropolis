"""Periodic scheduler: aggregate buffered snapshots and submit optimizations.

Every ``cadence_s`` seconds, for each configured corridor: pull the snapshots
of the last ``window_s`` seconds from the buffer, aggregate them into one TMC
demand file (written under ``output_root/scheduled/<corridor>/<ts>/``), and
hand it to the injected ``submit(corridor_cfg, aggregated_path)`` callback.
An entry with ``counts_source: scenario_routes`` needs no snapshots (its worker
derives the turning counts from the scenario's own route files): it fires
whatever the buffer holds, on a placeholder demand file written in the same
place, but skips the tick (one log line) while the last run on the same inputs
is in progress or after it completed. The inputs are the entry's own config and
the size and mtime of its base sumocfg, every file that sumocfg names and its
baseline sheet. A run that failed, or whose worker died (``run_status`` reports
it lost), does not count; ``rerun_unchanged: true`` on the entry runs it on
every tick. The record of the runs lives in memory, so after a restart the entry
runs once more. Every other entry is skipped when its window is empty, and
never on its inputs.
``submit`` returns the new run_id, or raises :class:`SubmissionRejected` with
the HTTP status and detail of a rejected submission (``None`` is still read as
a rejection without a reason). The scheduler logs the status and detail — a
WARNING for 429 (other runs hold the slots), an ERROR for anything else (the
entry needs fixing, e.g. 422 for ``n_workers`` above the deployment's maximum
concurrency) — and tries again next tick.
Recording the latest run per corridor (Redis key ``city_sched:last:{id}``) is
the submit callback's job: the scheduler itself stays Redis-free.

A tick never overlaps a still-running previous tick (asyncio.Lock guard: the
new tick is skipped, not queued). Per-corridor failures are logged and do not
crash the loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from terasim_service.city.job_store import COMPLETED, RUNNING, SUBMITTED

logger = logging.getLogger(__name__)

AGGREGATED_FILENAME = "aggregated_tmc.json"

#: Entry keys the scheduler reads itself; they are never forwarded into the
#: optimization request (whose model rejects unknown fields).
SCHEDULER_ONLY_KEYS: frozenset[str] = frozenset({"rerun_unchanged"})

#: run_status(run_id) -> the run's job status ("submitted", "running", "completed",
#: "failed"), or anything else (e.g. None, "lost") when it is neither finished nor
#: in progress.
RunStatus = Callable[[str], Optional[str]]

#: Fingerprints remembered per scenario-count corridor (the most recent ones).
_FINGERPRINTS_KEPT = 16

#: submit(corridor_cfg, aggregated_tmc_path) -> run_id; raises SubmissionRejected
#: (or returns None) when the submission is rejected.
SubmitCallback = Callable[[dict, Path], Optional[str]]


class SubmissionRejected(Exception):
    """A scheduled submission the service refused, with its HTTP status and detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = int(status_code)
        self.detail = str(detail)


def _scenario_routes_placeholder(corridor_id: str, now: float) -> dict:
    """The demand file of a scenario-count entry: little more than a corridor id and a time.

    The worker derives the turning counts from the base scenario's route files and
    reads only this file's ``corridor_id`` and ``timestamp_start`` (the time of day
    that selects stored-plan patterns); the rest keeps it a well-formed TMC file.
    ``optimize_api._resolve_corridor_request`` writes the same placeholder for a
    corridor_id request (a test pins that the two stay alike).
    """
    started = datetime.fromtimestamp(now, timezone.utc).replace(microsecond=0)
    return {
        "corridor_id": corridor_id,
        "timestamp_start": started.isoformat(),
        "observation_interval": "1h",
        "intersections": [
            {"intersection_id": "placeholder", "lat": 0.0, "lon": 0.0, "counts": {}}
        ],
    }


def _scenario_fingerprint(cfg: dict) -> str:
    """The inputs of a scenario-count entry, as one digest.

    The entry's own config plus the size and mtime of its base sumocfg, of every
    file that sumocfg names (net, routes, additional files, GUI settings) and of
    its baseline sheet. A file that cannot be read, or a sumocfg that cannot be
    parsed, enters as such: the submission then reports what is wrong.
    """
    files: list = []
    base = cfg.get("base_sumocfg")
    if base:
        files.append(base)
        try:
            from terasim_city.scenario.sumocfg_io import parse_sumocfg

            info = parse_sumocfg(base)
        except Exception:
            files.append("<sumocfg not parsable>")
        else:
            files += [info.net_file, *info.route_files, *info.additional_files]
            if info.gui_settings_file is not None:
                files.append(info.gui_settings_file)
    if cfg.get("baseline_sheet"):
        files.append(cfg["baseline_sheet"])

    def stat(name) -> list:
        try:
            info = Path(name).stat()
        except (OSError, ValueError):
            return [str(name), None, None]
        return [str(name), info.st_size, info.st_mtime_ns]

    payload = {"entry": cfg, "files": [stat(name) for name in files]}
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_scheduled_config(path) -> dict[str, dict]:
    """Parse the scheduler YAML ``{corridors: {<id>: {...}}}`` into a dict.

    Each corridor entry must carry ``base_sumocfg`` and ``corridor``; a
    two-element ``corridor`` list is normalized to ``{"from","to"}``. Extra
    keys (``max_trials``, ``sim_end``, ``n_workers``, ...) are kept verbatim —
    the submit path forwards them into the optimization request. The corridor
    id from the mapping key is injected as ``corridor_id``.
    """
    import yaml

    path = Path(path)
    if not path.is_file():
        raise ValueError(f"scheduled config not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("corridors"), dict):
        raise ValueError(
            f"scheduled config {path} must be a mapping with a 'corridors' mapping"
        )
    corridors = payload["corridors"]
    if not corridors:
        raise ValueError(f"scheduled config {path} defines no corridors")

    result: dict[str, dict] = {}
    for corridor_id, cfg in corridors.items():
        corridor_id = str(corridor_id)
        if not isinstance(cfg, dict):
            raise ValueError(
                f"scheduled config {path}: corridor {corridor_id!r} must be a "
                f"mapping, got {type(cfg).__name__}"
            )
        for required in ("base_sumocfg", "corridor"):
            if required not in cfg:
                raise ValueError(
                    f"scheduled config {path}: corridor {corridor_id!r} is "
                    f"missing required field {required!r}"
                )
        entry = dict(cfg)
        corridor_ref = entry["corridor"]
        if isinstance(corridor_ref, (list, tuple)):
            if len(corridor_ref) != 2:
                raise ValueError(
                    f"scheduled config {path}: corridor {corridor_id!r} has a "
                    f"list-form 'corridor' of length {len(corridor_ref)}; "
                    "expected exactly [from, to]"
                )
            entry["corridor"] = {
                "from": str(corridor_ref[0]),
                "to": str(corridor_ref[1]),
            }
        elif not isinstance(corridor_ref, dict):
            raise ValueError(
                f"scheduled config {path}: corridor {corridor_id!r} 'corridor' "
                "must be a mapping (tls_ids / from,to) or a [from, to] list"
            )
        if "rerun_unchanged" in entry and not isinstance(entry["rerun_unchanged"], bool):
            raise ValueError(
                f"scheduled config {path}: corridor {corridor_id!r} 'rerun_unchanged' "
                f"must be true or false, got {entry['rerun_unchanged']!r}"
            )
        entry["corridor_id"] = corridor_id
        result[corridor_id] = entry
    return result


class CityScheduler:
    """Cadence loop over configured corridors (see module docstring)."""

    def __init__(
        self,
        buffer,
        submit: SubmitCallback,
        corridors: dict,
        *,
        output_root,
        cadence_s: float,
        window_s: float,
        clock: Callable[[], float] = time.time,
        run_status: Optional[RunStatus] = None,
    ) -> None:
        """``run_status`` looks a submitted run's status up; without it a
        scenario-count entry cannot tell that its last run completed and so runs
        on every tick."""
        if not isinstance(corridors, dict):
            raise ValueError(
                f"corridors must be a dict, got {type(corridors).__name__}"
            )
        if cadence_s <= 0:
            raise ValueError(f"cadence_s must be positive, got {cadence_s}")
        if window_s <= 0:
            raise ValueError(f"window_s must be positive, got {window_s}")
        self._buffer = buffer
        self._submit = submit
        self._corridors = corridors
        self._output_root = Path(output_root)
        self._cadence_s = float(cadence_s)
        self._window_s = float(window_s)
        self._clock = clock
        self._run_status = run_status
        # corridor id -> {input fingerprint: the last run submitted on those inputs},
        # oldest first (scenario-count entries only).
        self._scenario_runs: dict[str, dict[str, str]] = {}
        self._lock = asyncio.Lock()

    async def run(self, stop_event: asyncio.Event) -> None:
        """Tick every cadence until ``stop_event`` is set."""
        while True:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._cadence_s)
                return  # stop requested during the wait
            except asyncio.TimeoutError:
                pass
            await self.tick()

    async def tick(self) -> None:
        """One scheduling pass; skipped if the previous tick is still running."""
        if self._lock.locked():
            logger.warning("city scheduler tick skipped: previous tick still running")
            return
        async with self._lock:
            for corridor_id, cfg in self._corridors.items():
                try:
                    self._process_corridor(corridor_id, cfg)
                except Exception:
                    logger.exception(
                        "scheduled submission for corridor %s failed", corridor_id
                    )

    def _same_inputs_done_or_running(self, corridor_id: str, fingerprint: str) -> bool:
        """True (and logged) when the last run on these very inputs completed or is
        still in progress: submitting again would only repeat it."""
        run_id = self._scenario_runs.get(corridor_id, {}).get(fingerprint)
        if run_id is None or self._run_status is None:
            return False
        try:
            status = self._run_status(run_id)
        except Exception:
            logger.warning(
                "corridor %s: cannot read the status of run %s; running the tick",
                corridor_id,
                run_id,
                exc_info=True,
            )
            return False
        if status in (SUBMITTED, RUNNING):
            logger.info(
                "corridor %s: run %s on these inputs is still in progress; skipping tick",
                corridor_id,
                run_id,
            )
            return True
        if status == COMPLETED:
            logger.info(
                "corridor %s: inputs unchanged since run %s completed; skipping tick "
                "(set rerun_unchanged: true to run on every tick)",
                corridor_id,
                run_id,
            )
            return True
        return False

    def _remember_scenario_run(self, corridor_id: str, fingerprint: str, run_id: str) -> None:
        runs = self._scenario_runs.setdefault(corridor_id, {})
        runs.pop(fingerprint, None)  # the most recent fingerprints come last
        runs[fingerprint] = run_id
        while len(runs) > _FINGERPRINTS_KEPT:
            runs.pop(next(iter(runs)))

    def _process_corridor(self, corridor_id: str, cfg: dict) -> None:
        fingerprint = None
        if cfg.get("counts_source") == "scenario_routes":
            # The counts come from the scenario's own routes inside the worker, so
            # there is no demand to wait for or aggregate: the buffer is not even
            # read. The same inputs give the same answer, so a tick whose inputs
            # match a completed or running run is skipped (unless the entry asks
            # otherwise).
            fingerprint = _scenario_fingerprint(cfg)
            if not cfg.get("rerun_unchanged") and self._same_inputs_done_or_running(
                corridor_id, fingerprint
            ):
                return
            demand = _scenario_routes_placeholder(corridor_id, self._clock())
            demand_note = "scenario routes, no snapshots"
        else:
            snapshots = self._buffer.window(corridor_id, self._window_s)
            if not snapshots:
                logger.info(
                    "corridor %s: no snapshots in the last %.0f s; skipping tick",
                    corridor_id,
                    self._window_s,
                )
                return

            from terasim_service.city.aggregate import aggregate_snapshots

            demand = aggregate_snapshots(snapshots)
            demand_note = f"{len(snapshots)} snapshot(s)"
        timestamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(self._clock()))
        out_dir = self._output_root / "scheduled" / corridor_id / timestamp
        out_dir.mkdir(parents=True, exist_ok=True)
        aggregated_path = out_dir / AGGREGATED_FILENAME
        aggregated_path.write_text(json.dumps(demand, indent=2) + "\n", encoding="utf-8")

        corridor_cfg = dict(cfg)
        corridor_cfg.setdefault("corridor_id", corridor_id)
        try:
            run_id = self._submit(corridor_cfg, aggregated_path)
        except SubmissionRejected as rejected:
            # 429: other runs hold the slots right now. Anything else will not go
            # away by itself (bad entry, n_workers above the cap, ...).
            level = logging.WARNING if rejected.status_code == 429 else logging.ERROR
            logger.log(
                level,
                "corridor %s: submission rejected (HTTP %s): %s; retrying next tick",
                corridor_id,
                rejected.status_code,
                rejected.detail,
            )
            return
        if run_id is None:
            logger.warning(
                "corridor %s: submission rejected (no reason given); retrying next tick",
                corridor_id,
            )
        else:
            if fingerprint is not None:
                self._remember_scenario_run(corridor_id, fingerprint, run_id)
            logger.info(
                "corridor %s: submitted optimization run %s (%s)",
                corridor_id,
                run_id,
                demand_note,
            )
