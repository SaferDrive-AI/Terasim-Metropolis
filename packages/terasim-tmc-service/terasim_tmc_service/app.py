"""TMC microservice: registry endpoints, one-shot ticks and the periodic injection loop."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from terasim_agent_skill.client import CityOptimizationClient
from terasim_tmc_service.generator import (
    DEFAULT_SETTLE_LAG_S,
    BehaviorSource,
    VssBehaviorSource,
    aligned_window,
    build_ftmc,
    seconds_until_next_window,
)
from terasim_tmc_service.injector import SnapshotInjector
from terasim_tmc_service.nv_generate_tmc import (
    COUNTED_OBJECT_TYPES,
    counted_object_types_from_env,
)
from terasim_tmc_service.registry import (
    CorridorMismatchError,
    IntersectionRegistry,
    IntersectionTooFarError,
    UnknownIntersectionError,
)
from terasim_tmc_service.spec import CorridorSpec

logger = logging.getLogger(__name__)

DEFAULT_TERASIM_CITY_URL = "http://127.0.0.1:8000"
DEFAULT_REGISTRY_FILE = "./tmc_registry.json"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8200


# -- settings --------------------------------------------------------------------


@dataclass(frozen=True)
class TmcSettings:
    spec_file: Path
    registry_file: Path
    vss_behavior_url: str
    terasim_city_url: str
    settle_lag_s: float
    autostart: bool
    host: str
    port: int
    #: Canonical detector labels counted as traffic (``TMC_COUNTED_OBJECT_TYPES``).
    counted_object_types: frozenset[str] = COUNTED_OBJECT_TYPES

    @classmethod
    def from_environment(cls) -> "TmcSettings":
        spec_file = os.environ.get("TMC_SPEC_FILE")
        if not spec_file:
            raise ValueError("TMC_SPEC_FILE must point at the corridor spec (Metadata.json)")
        vss_behavior_url = os.environ.get("VSS_BEHAVIOR_URL")
        if not vss_behavior_url:
            raise ValueError("VSS_BEHAVIOR_URL must be set, e.g. http://<vss-host>:8081")
        settle_lag_s = float(os.environ.get("TMC_SETTLE_LAG_S", DEFAULT_SETTLE_LAG_S))
        if settle_lag_s < 0:
            raise ValueError("TMC_SETTLE_LAG_S must be >= 0")
        port = int(os.environ.get("TMC_PORT", DEFAULT_PORT))
        if not 1 <= port <= 65535:
            raise ValueError("TMC_PORT must be within 1..65535")
        return cls(
            spec_file=Path(spec_file),
            registry_file=Path(os.environ.get("TMC_REGISTRY_FILE", DEFAULT_REGISTRY_FILE)),
            vss_behavior_url=vss_behavior_url,
            terasim_city_url=os.environ.get("TERASIM_CITY_URL", DEFAULT_TERASIM_CITY_URL),
            settle_lag_s=settle_lag_s,
            autostart=os.environ.get("TMC_AUTOSTART", "1") == "1",
            host=os.environ.get("TMC_HOST", DEFAULT_HOST),
            port=port,
            counted_object_types=counted_object_types_from_env(),
        )


# -- tick ------------------------------------------------------------------------


def _object_type_histogram(behaviors: Any) -> dict[str, int]:
    """``object.type`` -> number of fetched behaviors (shows which labels the detector emits)."""
    histogram: dict[str, int] = {}
    for item in behaviors or ():
        obj = item.get("object") if isinstance(item, dict) else None
        label = obj.get("type") if isinstance(obj, dict) else None
        key = label if isinstance(label, str) else "<none>"
        histogram[key] = histogram.get(key, 0) + 1
    return dict(sorted(histogram.items()))


def run_tick(
    spec: CorridorSpec,
    registry: IntersectionRegistry,
    source: BehaviorSource,
    injector: Any,
    *,
    now: datetime,
    settle_lag_s: float,
    last_windows: dict[str, str],
    counted_object_types: frozenset[str] = COUNTED_OBJECT_TYPES,
) -> dict:
    """One generate+inject pass for the corridor; ``last_windows`` dedups repeated windows."""
    registered = registry.list()
    if not registered:
        return {"status": "idle", "reason": "no registered intersections"}
    timestamp_start, timestamp_end = aligned_window(
        now, spec.observation_seconds, settle_lag_s=settle_lag_s
    )
    if last_windows.get(spec.corridor_id) == timestamp_start:
        return {
            "status": "skipped",
            "reason": "window already injected",
            "timestamp_start": timestamp_start,
        }
    sensors: list[str] = []
    for record in registered:
        for sensor in record.sensors:
            if sensor not in sensors:
                sensors.append(sensor)
    behaviors_by_sensor = {
        sensor: source.fetch(sensor, timestamp_start, timestamp_end) for sensor in sensors
    }
    generated = build_ftmc(
        spec,
        registered,
        behaviors_by_sensor,
        timestamp_start=timestamp_start,
        counted_object_types=counted_object_types,
    )
    reply = injector.inject(generated.payload)
    last_windows[spec.corridor_id] = timestamp_start
    return {
        "status": "injected",
        "corridor_id": spec.corridor_id,
        "timestamp_start": timestamp_start,
        "timestamp_end": timestamp_end,
        "buffered": reply.get("buffered"),
        "behaviors": {sensor: len(items) for sensor, items in behaviors_by_sensor.items()},
        "object_types": {
            sensor: _object_type_histogram(items) for sensor, items in behaviors_by_sensor.items()
        },
        "counts": {
            item["intersection_id"]: item["counts"] for item in generated.payload["intersections"]
        },
        "stats": generated.stats,
    }


# -- HTTP app --------------------------------------------------------------------


class RegisterRequest(BaseModel):
    intersection_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    corridor_id: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_last_windows(path: Path) -> dict[str, str]:
    """Restore ``{corridor_id: timestamp_start}``; an unusable file starts an empty state."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("ignoring unreadable last-windows file %s: %s", path, exc)
        return {}
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        logger.warning(
            "ignoring last-windows file %s: expected an object of corridor_id -> timestamp", path
        )
        return {}
    return dict(data)


def _load_last_injected(path: Path) -> dict | None:
    """Restore the last injected tick; an unusable file starts without one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("ignoring unreadable last-injected file %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) and data.get("status") == "injected" else None


def _store_json(path: Path, data: Any) -> None:
    """Atomically persist ``data`` as JSON (best effort: never fails a tick)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except (OSError, TypeError, ValueError):
        logger.exception("could not persist %s", path)


def create_app(
    spec: CorridorSpec,
    registry: IntersectionRegistry,
    source: BehaviorSource,
    injector: Any,
    *,
    settle_lag_s: float = DEFAULT_SETTLE_LAG_S,
    autostart: bool = True,
    clock: Callable[[], datetime] | None = None,
    windows_file: Path | str | None = None,
    tick_on_register: bool = True,
    counted_object_types: frozenset[str] = COUNTED_OBJECT_TYPES,
) -> FastAPI:
    """Build the TMC HTTP app around a corridor spec, registry, behavior source and injector.

    ``windows_file`` persists the injected-window watermark (``{corridor_id: timestamp_start}``):
    it is read at creation when the file exists (a corrupt file only logs a warning) and
    rewritten atomically after every injected tick, so a restart inside the same observation
    window skips instead of re-injecting it. The last injected tick is kept next to it
    (``tmc_last_injected.json``) and reported by the health endpoint as ``last_injected``, also
    after later ticks skip the window.
    ``tick_on_register`` runs one best-effort tick on a daemon thread right after a successful
    registration, so the first snapshot does not wait for the next window boundary.
    """
    clock = clock or _utc_now
    windows_path = Path(windows_file) if windows_file is not None else None
    injected_path = windows_path.with_name("tmc_last_injected.json") if windows_path else None
    last_windows: dict[str, str] = {}
    if windows_path is not None and windows_path.exists():
        last_windows = _load_last_windows(windows_path)
    last_injected = None
    if injected_path is not None and injected_path.exists():
        last_injected = _load_last_injected(injected_path)
    state: dict[str, Any] = {
        "last_windows": last_windows,
        "last_tick": None,
        "last_injected": last_injected,
    }
    tick_lock = threading.Lock()

    def tick_now() -> dict:
        with tick_lock:
            try:
                result = run_tick(
                    spec,
                    registry,
                    source,
                    injector,
                    now=clock(),
                    settle_lag_s=settle_lag_s,
                    last_windows=state["last_windows"],
                    counted_object_types=counted_object_types,
                )
            except Exception as exc:
                state["last_tick"] = {
                    "at": _stamp(clock()),
                    "status": "failed",
                    "error": str(exc),
                }
                raise
            tick = {"at": _stamp(clock()), **result}
            if result.get("status") == "injected":
                state["last_injected"] = tick
                if windows_path is not None:
                    _store_json(windows_path, state["last_windows"])
                    _store_json(injected_path, tick)
            state["last_tick"] = tick
        return result

    def _background_tick() -> None:
        try:
            tick_now()
        except Exception:  # noqa: BLE001 - registration must not fail on an upstream outage
            logger.exception("tick after registration failed")

    async def periodic_loop() -> None:
        while True:
            try:
                result = await asyncio.to_thread(tick_now)
                logger.info("tmc tick: %s", {k: v for k, v in result.items() if k != "stats"})
            except Exception:  # noqa: BLE001 - the loop must survive upstream outages
                logger.exception("tmc tick failed")
            delay = seconds_until_next_window(clock(), spec.observation_seconds, settle_lag_s)
            await asyncio.sleep(delay)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(periodic_loop()) if autostart else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="TeraSim Metropolis TMC collector", version="0.2.0", lifespan=lifespan)

    @app.post("/v1/tmc/intersections", status_code=201)
    def register_intersection(request: RegisterRequest):
        try:
            record = registry.register(
                request.intersection_id,
                request.name,
                request.lat,
                request.lon,
                corridor_id=request.corridor_id,
            )
        except UnknownIntersectionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (IntersectionTooFarError, CorridorMismatchError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if tick_on_register:
            threading.Thread(
                target=_background_tick, name="tmc-tick-on-register", daemon=True
            ).start()
        return record.to_dict()

    @app.get("/v1/tmc/intersections")
    def list_intersections():
        registered_ids = set(registry.registered_ids())
        return {
            "corridor_id": spec.corridor_id,
            "observation_interval": spec.observation_interval,
            "available": [
                {
                    "intersection_id": intersection.intersection_id,
                    "lat": intersection.lat,
                    "lon": intersection.lon,
                    "sensors": intersection.sensors,
                    "registered": intersection.intersection_id in registered_ids,
                }
                for intersection in spec.intersections.values()
            ],
            "registered": [record.to_dict() for record in registry.list()],
        }

    @app.post("/v1/tmc/run-once")
    def run_once():
        try:
            return tick_now()
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            # pydantic.ValidationError is a ValueError: our own generated snapshot was malformed.
            raise HTTPException(
                status_code=500, detail=f"snapshot validation failed: {exc}"
            ) from exc

    @app.get("/v1/tmc/health")
    def health():
        return {
            "status": "ok",
            "corridor_id": spec.corridor_id,
            "observation_interval": spec.observation_interval,
            "registered": len(registry.registered_ids()),
            "settle_lag_s": settle_lag_s,
            "counted_object_types": sorted(counted_object_types),
            "autostart": autostart,
            "last_tick": state["last_tick"],
            "last_injected": state["last_injected"],
        }

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = TmcSettings.from_environment()
    spec = CorridorSpec.from_json(settings.spec_file)
    registry = IntersectionRegistry(spec, settings.registry_file)
    source = VssBehaviorSource(settings.vss_behavior_url)
    injector = SnapshotInjector(CityOptimizationClient(settings.terasim_city_url, timeout_s=60.0))
    windows_file = settings.registry_file.with_name("tmc_last_windows.json")
    app = create_app(
        spec, registry, source, injector, settle_lag_s=settings.settle_lag_s,
        autostart=settings.autostart, windows_file=windows_file,
        counted_object_types=settings.counted_object_types,
    )
    logger.info(
        "terasim-tmc-service: corridor=%s interval=%s vss=%s terasim=%s registry=%s windows=%s "
        "counted=%s",
        spec.corridor_id, spec.observation_interval, settings.vss_behavior_url,
        settings.terasim_city_url, settings.registry_file, windows_file,
        ",".join(sorted(settings.counted_object_types)),
    )
    try:
        uvicorn.run(app, host=settings.host, port=settings.port)
    finally:
        source.close()
        injector.close()


if __name__ == "__main__":
    main()
