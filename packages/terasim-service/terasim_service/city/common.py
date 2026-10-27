"""Shared infrastructure for the city API surface and its workers.

Job-store configuration, request-path validation, worker lease primitives and
small formatting helpers used by the v1 optimization router
(:mod:`terasim_service.city.optimize_api`) and its worker
(:mod:`terasim_service.city.worker_optimize`).
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from fastapi import HTTPException

from terasim_service.city.job_store import CityJobStore
from terasim_service.constants import (
    ENVIRONMENT_VARIABLE_CITY_EMERGENCY_SLOTS,
    ENVIRONMENT_VARIABLE_CITY_LEASE_TTL,
    ENVIRONMENT_VARIABLE_CITY_MAX_CONCURRENCY,
)
from terasim_service.utils.base import _int_env, redis_connection_kwargs_from_env

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENCY = 3
DEFAULT_LEASE_TTL = 120
# Reserved headroom above max_concurrency, usable only by emergency
# (incident-expedited) slot acquisitions.
DEFAULT_EMERGENCY_SLOTS = 2
# Must stay >= the worker's heartbeat interval; the lease has to outlive one
# heartbeat cycle or a single slow renew would look like a lost slot.
HEARTBEAT_INTERVAL = 30
DEFAULT_HEARTBEAT_INTERVAL = 30.0
KEY_PREFIX = "city_eval"


#: Loggers whose INFO lines the service shows: the optimization pipeline (e.g. which
#: seeds warm-started a search) and the city API / scheduler (e.g. a skipped tick).
CITY_LOGGERS = ("terasim_city", "terasim_service.city")


def city_log_config(base: dict) -> dict:
    """``base`` (uvicorn's logging dictConfig) plus INFO logging for :data:`CITY_LOGGERS`.

    uvicorn configures only its own loggers and leaves the root at WARNING, so the
    pipeline's INFO lines would never reach the service log. The result adds one
    stderr handler (time, level, logger name) for the city loggers, which do not
    propagate (no duplicates under a configured root). ``base`` is not modified.
    Applied by the service entry point only, never by ``create_app``.
    """
    config = copy.deepcopy(base)
    config["disable_existing_loggers"] = False
    config.setdefault("formatters", {})["city"] = {
        "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
    }
    config.setdefault("handlers", {})["city"] = {
        "class": "logging.StreamHandler",
        "formatter": "city",
        "stream": "ext://sys.stderr",
    }
    loggers = config.setdefault("loggers", {})
    for name in CITY_LOGGERS:
        loggers[name] = {"handlers": ["city"], "level": "INFO", "propagate": False}
    return config


class LeaseLost(Exception):
    """Raised (via the progress callback) when the worker lost its lease mid-run."""


def _best_effort(fn: Callable, *args) -> None:
    """Call a store method, swallowing errors (e.g. Redis unavailable). The lease
    TTL is the ultimate backstop, so a failed cleanup must not crash the worker."""
    try:
        fn(*args)
    except Exception as exc:
        logger.warning("best-effort %s failed: %s", getattr(fn, "__name__", fn), exc)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_input_file(path_str: str, input_root: Optional[Path]) -> Path:
    """Resolve a request path to a real file, optionally confined to input_root."""
    try:
        resolved = Path(path_str).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=f"input not found: {path_str}") from exc
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail=f"not a regular file: {path_str}")
    if input_root is not None and not _is_within(resolved, input_root):
        raise HTTPException(
            status_code=400, detail=f"input is outside the allowed root: {path_str}"
        )
    return resolved


def _validate_map_references(base_sumocfg: Path, input_root: Optional[Path]) -> None:
    """Confine the sumocfg's direct references (net / routes / additional / gui) to input_root.

    The route files count too: the scenario-route flow and counts sources read
    and copy them. No-op when input_root is unset. Parsing failures map to 400
    (a bad request), not 500. SumocfgInfo paths are already resolved to absolute
    paths.
    """
    if input_root is None:
        return
    from terasim_city.scenario.sumocfg_io import parse_sumocfg

    try:
        info = parse_sumocfg(base_sumocfg)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"cannot parse sumocfg: {exc}") from exc

    references = [info.net_file, *info.route_files, *info.additional_files]
    if info.gui_settings_file is not None:
        references.append(info.gui_settings_file)
    for ref in references:
        if not _is_within(ref, input_root):
            raise HTTPException(
                status_code=400,
                detail=f"map reference is outside the allowed root: {ref}",
            )
        if not ref.is_file():
            raise HTTPException(
                status_code=400,
                detail=f"map reference is not a regular file: {ref}",
            )


def store_config_from_env() -> dict:
    """The single source of store construction params, shared by the parent store
    and every child worker, so lease semantics match across processes."""
    return {
        "redis": redis_connection_kwargs_from_env(),
        "max_concurrency": _int_env(
            ENVIRONMENT_VARIABLE_CITY_MAX_CONCURRENCY, DEFAULT_MAX_CONCURRENCY, minimum=1
        ),
        "lease_ttl": _int_env(
            ENVIRONMENT_VARIABLE_CITY_LEASE_TTL, DEFAULT_LEASE_TTL, minimum=HEARTBEAT_INTERVAL + 1
        ),
        "emergency_slots": _int_env(
            ENVIRONMENT_VARIABLE_CITY_EMERGENCY_SLOTS, DEFAULT_EMERGENCY_SLOTS, minimum=0
        ),
        "key_prefix": KEY_PREFIX,
    }


def _get_record_or_404(store: CityJobStore, run_id: str):
    try:
        record = store.get(run_id)
    except Exception as exc:
        logger.exception("job store unavailable reading %s", run_id)
        raise HTTPException(status_code=503, detail="job store unavailable") from exc
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown run_id: {run_id}")
    return record


def _iso(timestamp: Optional[float]) -> Optional[str]:
    """Format an epoch timestamp as ISO-8601 UTC (e.g. 2026-06-03T21:09:03Z)."""
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "CITY_LOGGERS",
    "DEFAULT_EMERGENCY_SLOTS",
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_LEASE_TTL",
    "DEFAULT_MAX_CONCURRENCY",
    "HEARTBEAT_INTERVAL",
    "KEY_PREFIX",
    "LeaseLost",
    "city_log_config",
    "store_config_from_env",
]
