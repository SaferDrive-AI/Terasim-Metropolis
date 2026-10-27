"""Simulated signal-performance measures of every completed run, computed in the background.

After a run completes, whatever started it, and while ``TERASIM_CITY_SIM_METRICS`` is on
(the default), the service re-runs the run's baseline and optimized trials in SUMO with
virtual detectors (:func:`terasim_city.evaluation.sim_metrics.compute_sim_metrics`) and
writes ``sim_metrics.json`` (``corridor_ui.sim_metrics.v1``) into the run directory. The
run is recorded as completed, with its result written, before any of this starts, so
``/result`` never waits for it.

Files in the run directory:

``sim_metrics.pending.json``
    written before the run is recorded as completed; its mtime is refreshed every
    heartbeat while the job waits for a worker slot or computes. A marker that stops
    being refreshed (the process died) reads as unavailable after ``STALE_AFTER_S``.
``sim_metrics.json``
    the document, written atomically.
``sim_metrics.error.json``
    ``{"reason": ...}`` when the computation failed.

:func:`sim_metrics_state` turns those (and the run's status) into ``ready``,
``pending`` or ``unavailable`` with a reason; the endpoint answers 200 / 202 / 404 and
the run listing shows the state.

Resources: the job holds one slot of the deployment's shared concurrency cap (job-store
lease ``sim-metrics-<run_id>``, renewed from the heartbeat) while SUMO runs, waiting for
a free slot up to ``TERASIM_CITY_SIM_METRICS_SLOT_WAIT_S`` (default 3600 s). The two
re-runs happen in a spawned child process that is stopped after
``TERASIM_CITY_SIM_METRICS_TIMEOUT_S`` (default 900 s; the computation itself gives up
slightly earlier with a clear reason). An asynchronous run's worker process runs the
job right after recording the run completed and releasing its own slots; the
synchronous endpoint starts a process for it (:func:`make_sim_metrics_process_launcher`,
the same mechanism the asynchronous runs use) once its run is recorded.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from terasim_service.city.run_outputs import read_json, write_json_atomic
from terasim_service.constants import (
    ENVIRONMENT_VARIABLE_CITY_SIM_METRICS,
    ENVIRONMENT_VARIABLE_CITY_SIM_METRICS_SLOT_WAIT_S,
    ENVIRONMENT_VARIABLE_CITY_SIM_METRICS_TIMEOUT_S,
)

logger = logging.getLogger(__name__)

SIM_METRICS_FILENAME = "sim_metrics.json"
SIM_METRICS_PENDING_FILENAME = "sim_metrics.pending.json"
SIM_METRICS_ERROR_FILENAME = "sim_metrics.error.json"

READY = "ready"
PENDING = "pending"
UNAVAILABLE = "unavailable"

DEFAULT_TIMEOUT_S = 900.0
DEFAULT_SLOT_WAIT_S = 3600.0
HEARTBEAT_INTERVAL_S = 30.0
#: A pending marker not refreshed for this long belongs to a job that is gone.
STALE_AFTER_S = 4 * HEARTBEAT_INTERVAL_S
POLL_INTERVAL_S = 5.0
#: The computation's own deadline comes this much before the child is stopped.
_SOFT_DEADLINE_SHARE = 0.95

_FALSY = frozenset({"0", "false", "no", "off"})

#: compute(run_dir, *, run_id, corridor_id) -> the document
Compute = Callable[..., dict]


def sim_metrics_enabled() -> bool:
    """``TERASIM_CITY_SIM_METRICS``: on unless set to 0/false/no/off."""
    raw = os.environ.get(ENVIRONMENT_VARIABLE_CITY_SIM_METRICS, "1").strip().lower()
    return raw not in _FALSY


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number of seconds, got {raw!r}") from exc
    if not value > 0:
        raise ValueError(f"{name} must be > 0, got {raw!r}")
    return value


def slot_token(run_id: str) -> str:
    """The job-store lease a run's metrics job holds."""
    return f"sim-metrics-{run_id}"


@dataclass(frozen=True)
class SimMetricsState:
    """``ready`` / ``pending`` / ``unavailable``, with the reason when unavailable."""

    state: str
    reason: Optional[str] = None


_DISABLED = (
    "simulated signal metrics are disabled on this deployment "
    f"({ENVIRONMENT_VARIABLE_CITY_SIM_METRICS}=0)"
)


def sim_metrics_state(
    run_dir: Path,
    run_status: str,
    *,
    enabled: Optional[bool] = None,
    now: Optional[float] = None,
) -> SimMetricsState:
    """The metrics' state for a run in ``run_status`` (job-store status, or ``"lost"``)."""
    run_dir = Path(run_dir)
    if (run_dir / SIM_METRICS_FILENAME).is_file():
        return SimMetricsState(READY)
    enabled = sim_metrics_enabled() if enabled is None else enabled
    if run_status in ("submitted", "running"):
        return SimMetricsState(PENDING) if enabled else SimMetricsState(UNAVAILABLE, _DISABLED)
    if run_status != "completed":
        return SimMetricsState(
            UNAVAILABLE, f"the optimization did not complete (status: {run_status})"
        )
    error = read_json(run_dir / SIM_METRICS_ERROR_FILENAME)
    if error is not None:
        return SimMetricsState(UNAVAILABLE, str(error.get("reason") or "the computation failed"))
    try:
        marked = (run_dir / SIM_METRICS_PENDING_FILENAME).stat().st_mtime
    except OSError:
        marked = None
    if marked is not None:
        age = (time.time() if now is None else now) - marked
        if age <= STALE_AFTER_S:
            return SimMetricsState(PENDING)
        return SimMetricsState(
            UNAVAILABLE,
            f"the computation stopped without a result (no progress for {age:.0f} s)",
        )
    if not enabled:
        return SimMetricsState(UNAVAILABLE, _DISABLED)
    return SimMetricsState(UNAVAILABLE, "simulated signal metrics were not computed for this run")


def mark_pending(run_dir: Path) -> None:
    """Mark the metrics of the run in ``run_dir`` as pending (before it reads completed)."""
    try:
        run_dir = Path(run_dir)
        (run_dir / SIM_METRICS_ERROR_FILENAME).unlink(missing_ok=True)
        write_json_atomic(
            run_dir / SIM_METRICS_PENDING_FILENAME,
            {"since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "pid": os.getpid()},
        )
    except Exception:  # noqa: BLE001 - never fails the run
        logger.warning("could not mark the metrics of %s pending", run_dir, exc_info=True)


def _touch(path: Path) -> None:
    try:
        os.utime(path, None)
    except FileNotFoundError:
        mark_pending(path.parent)
    except OSError:
        logger.debug("could not refresh %s", path, exc_info=True)


def _record_failure(run_dir: Path, reason: str) -> None:
    try:
        write_json_atomic(
            Path(run_dir) / SIM_METRICS_ERROR_FILENAME,
            {"reason": reason, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not record the metrics failure of %s", run_dir, exc_info=True)


class _ReasonError(Exception):
    """A failure whose message is the whole reason (recorded without a type prefix)."""


def _reason(exc: BaseException) -> str:
    from terasim_city.evaluation.sim_metrics import SimMetricsUnavailable

    if isinstance(exc, SimMetricsUnavailable):
        return str(exc)
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


# ---------------------------------------------------------------------------
# The computation, in a child process
# ---------------------------------------------------------------------------

def _child_main(run_dir: str, run_id: str, corridor_id, out_path: str, err_path: str,
                soft_timeout_s: float) -> None:
    """Spawned child: compute the document and write it to ``out_path`` (else the reason)."""
    try:
        from terasim_city.evaluation.sim_metrics import compute_sim_metrics, write_sim_metrics

        doc = compute_sim_metrics(
            run_dir, run_id=run_id, corridor_id=corridor_id,
            deadline=time.monotonic() + soft_timeout_s,
        )
        write_sim_metrics(doc, out_path)
    except BaseException as exc:  # noqa: BLE001 - report every failure to the parent
        try:
            Path(err_path).write_text(_reason(exc), encoding="utf-8")
        finally:
            os._exit(1)


def _compute_in_child(
    run_dir: Path,
    *,
    run_id: str,
    corridor_id: Optional[str],
    timeout_s: float,
    child_target=None,
) -> dict:
    """Run the computation in a spawned process; stop it after ``timeout_s``."""
    token = uuid.uuid4().hex
    out_path = Path(run_dir) / f".sim_metrics.{token}.json"
    err_path = Path(run_dir) / f".sim_metrics.{token}.err"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=child_target or _child_main,
        args=(str(run_dir), run_id, corridor_id, str(out_path), str(err_path),
              max(1.0, timeout_s * _SOFT_DEADLINE_SHARE)),
        daemon=True,
    )
    try:
        process.start()
        process.join(timeout_s)
        if process.is_alive():
            process.terminate()
            process.join(10)
            if process.is_alive():
                process.kill()
                process.join(5)
            raise _ReasonError(f"the SUMO re-runs did not finish within {timeout_s:g} s")
        if process.exitcode != 0 or not out_path.is_file():
            reason = None
            if err_path.is_file():
                reason = err_path.read_text(encoding="utf-8").strip() or None
            raise _ReasonError(reason or f"the computation exited with code {process.exitcode}")
        return json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        out_path.unlink(missing_ok=True)
        err_path.unlink(missing_ok=True)


def run_sim_metrics_job(
    store,
    run_id: str,
    run_dir: Path,
    *,
    corridor_id: Optional[str] = None,
    compute: Optional[Compute] = None,
    timeout_s: Optional[float] = None,
    slot_wait_s: Optional[float] = None,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
    poll_interval: float = POLL_INTERVAL_S,
    child_target=None,
) -> str:
    """Compute and write the run's ``sim_metrics.json``; returns the final state. Never raises.

    ``compute(run_dir, *, run_id, corridor_id)`` replaces the SUMO computation in its child
    process (tests). The corridor defaults to the run's job record.
    """
    run_dir = Path(run_dir)
    token = slot_token(run_id)
    pending = run_dir / SIM_METRICS_PENDING_FILENAME
    stop = threading.Event()
    acquired = False
    heartbeat = None
    try:
        timeout_s = timeout_s or _float_env(
            ENVIRONMENT_VARIABLE_CITY_SIM_METRICS_TIMEOUT_S, DEFAULT_TIMEOUT_S
        )
        slot_wait_s = slot_wait_s or _float_env(
            ENVIRONMENT_VARIABLE_CITY_SIM_METRICS_SLOT_WAIT_S, DEFAULT_SLOT_WAIT_S
        )
        if not pending.is_file():
            mark_pending(run_dir)
        if corridor_id is None:
            try:
                record = store.get(run_id)
                corridor_id = record.corridor_id if record is not None else None
            except Exception:  # noqa: BLE001 - the corridor is informative only
                corridor_id = None

        def _beat() -> None:
            while not stop.wait(heartbeat_interval):
                _touch(pending)
                if acquired:
                    try:
                        renewed = store.renew_slot(token)
                    except Exception:  # noqa: BLE001
                        renewed = False
                    if not renewed:
                        logger.warning("metrics job of %s lost its worker slot lease", run_id)

        heartbeat = threading.Thread(target=_beat, daemon=True)
        heartbeat.start()

        give_up = time.monotonic() + slot_wait_s
        while True:
            acquired = bool(store.try_acquire_slots(token, 1))
            if acquired:
                break
            if time.monotonic() >= give_up or stop.wait(poll_interval):
                raise _ReasonError(
                    f"no worker slot became free within {slot_wait_s:g} s "
                    "(all slots were busy with optimization runs)"
                )

        if compute is not None:
            document = compute(run_dir, run_id=run_id, corridor_id=corridor_id)
        else:
            document = _compute_in_child(
                run_dir, run_id=run_id, corridor_id=corridor_id, timeout_s=timeout_s,
                child_target=child_target,
            )
        from terasim_city.evaluation.sim_metrics import write_sim_metrics

        write_sim_metrics(document, run_dir / SIM_METRICS_FILENAME)
        (run_dir / SIM_METRICS_ERROR_FILENAME).unlink(missing_ok=True)
        logger.info("simulated signal metrics of run %s written", run_id)
        return READY
    except _ReasonError as exc:
        logger.warning("simulated signal metrics of run %s unavailable: %s", run_id, exc)
        _record_failure(run_dir, str(exc))
        return UNAVAILABLE
    except BaseException as exc:  # noqa: BLE001 - a background job must not raise
        logger.warning("simulated signal metrics of run %s failed: %s", run_id, exc,
                       exc_info=True)
        _record_failure(run_dir, _reason(exc))
        return UNAVAILABLE
    finally:
        stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=5)
        if acquired:
            try:
                store.release_slot(token)
            except Exception:  # noqa: BLE001 - the lease TTL is the backstop
                logger.warning("could not release the metrics slot of %s", run_id)
        pending.unlink(missing_ok=True)


def run_sim_metrics_process(run_id: str, run_dir: str, store_config: dict) -> None:
    """multiprocessing entry point: the job with a Redis store built from ``store_config``."""
    import redis

    from terasim_service.city.job_store import RedisCityJobStore

    store = RedisCityJobStore(
        client=redis.Redis(**store_config["redis"]),
        max_concurrency=store_config["max_concurrency"],
        lease_ttl=store_config["lease_ttl"],
        key_prefix=store_config.get("key_prefix", "city_eval"),
        emergency_slots=store_config.get("emergency_slots", 2),
    )
    run_sim_metrics_job(store, run_id, Path(run_dir))


def make_sim_metrics_process_launcher(store_config: dict) -> Callable[[str, Path], None]:
    """Production launcher for the synchronous endpoint: one process per run's metrics job."""

    def launch(run_id: str, run_dir: Path) -> None:
        process = multiprocessing.Process(
            target=run_sim_metrics_process,
            args=(run_id, str(run_dir), store_config),
            daemon=False,
        )
        process.start()

    return launch


__all__ = [
    "PENDING",
    "READY",
    "SIM_METRICS_ERROR_FILENAME",
    "SIM_METRICS_FILENAME",
    "SIM_METRICS_PENDING_FILENAME",
    "STALE_AFTER_S",
    "SimMetricsState",
    "UNAVAILABLE",
    "make_sim_metrics_process_launcher",
    "mark_pending",
    "run_sim_metrics_job",
    "run_sim_metrics_process",
    "sim_metrics_enabled",
    "sim_metrics_state",
    "slot_token",
]
