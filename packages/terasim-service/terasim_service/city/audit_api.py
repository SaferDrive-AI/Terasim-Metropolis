"""Audit log endpoints: operator feedback on plans and console usage.

    POST /v1/city/audit/events       record one event: console_opened, plan_viewed,
                                     plan_downloaded or feedback (201 + the stored event)
    GET  /v1/city/audit/events       events, newest first (filters by corridor, run,
                                     intersection, actor, type, time; limit ≤ 2000)
    GET  /v1/city/audit/events.csv   the same as a CSV download (limit ≤ 100 000)
    GET  /v1/city/audit/summary      usage of the last N days: per-day counts by type,
                                     users, feedback verdicts, acceptance rate, last activity

The service stamps every event's id, time and actor. The actor is the identity an
authenticating proxy put in ``X-Forwarded-User`` / ``X-Forwarded-Email`` /
``X-Remote-User`` (``actor_source: proxy``), else the display name the console sent
(``self``, not verified), else ``anonymous``. Events are kept as JSON Lines under the
output root (:mod:`terasim_service.city.audit_log`). ``TERASIM_CITY_AUDIT_ENABLED=0``
turns every endpoint into a 404 with that reason.

Mounted by :func:`terasim_service.city.optimize_api.create_v1_city_router` on its router.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from terasim_service.city.audit_log import (
    ANONYMOUS,
    VERDICTS,
    AuditLog,
    AuditLogUnavailable,
    csv_rows,
    parse_bound,
)
from terasim_service.constants import ENVIRONMENT_VARIABLE_CITY_AUDIT_ENABLED

#: Largest accepted request body.
MAX_BODY_BYTES = 8 * 1024
#: Events one service process accepts per minute (all clients together).
MAX_EVENTS_PER_MINUTE = 600
DEFAULT_LISTED_EVENTS = 200
MAX_LISTED_EVENTS = 2000
DEFAULT_CSV_EVENTS = 10_000
MAX_CSV_EVENTS = 100_000
MAX_SUMMARY_DAYS = 366
MAX_COMMENT_CHARS = 1000
MAX_ACTOR_CHARS = 100
MAX_PROXY_ACTOR_CHARS = 200
#: Identity headers an authenticating proxy sets, in order of precedence.
IDENTITY_HEADERS = ("X-Forwarded-User", "X-Forwarded-Email", "X-Remote-User")
DISABLED_DETAIL = (
    "the audit log is disabled on this deployment "
    f"({ENVIRONMENT_VARIABLE_CITY_AUDIT_ENABLED}=0)"
)
#: Events that concern one run's plan must name the run.
_NEEDS_RUN = frozenset({"plan_viewed", "plan_downloaded", "feedback"})
_FALSY = frozenset({"0", "false", "no", "off"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_COMMENT_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_FORMAT = re.compile(r"^[a-z0-9_.-]{1,20}$")

#: The four event types (audit_log.EVENT_TYPES), for request validation and the schema.
EventType = Literal["console_opened", "plan_viewed", "plan_downloaded", "feedback"]


def audit_enabled() -> bool:
    """``TERASIM_CITY_AUDIT_ENABLED``: on unless set to 0/false/no/off."""
    raw = os.environ.get(ENVIRONMENT_VARIABLE_CITY_AUDIT_ENABLED, "1").strip().lower()
    return raw not in _FALSY


def _text(value: Optional[str], name: str, max_chars: int) -> Optional[str]:
    """A trimmed single-line string (None when empty); ValueError when too long or unprintable."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if _CONTROL.search(text):
        raise ValueError(f"{name} must not contain control characters")
    if len(text) > max_chars:
        raise ValueError(f"{name} is longer than {max_chars} characters")
    return text


class AuditEventIn(BaseModel):
    """The body of POST /v1/city/audit/events."""

    model_config = ConfigDict(extra="forbid")

    type: EventType = Field(
        ..., description="console_opened | plan_viewed | plan_downloaded | feedback"
    )
    corridor_id: str = Field(..., description="The corridor the event concerns (1-128 characters).")
    run_id: Optional[str] = Field(
        None,
        description="The run whose plan the event concerns (required except for console_opened).",
    )
    intersection_id: Optional[str] = Field(None, description="Null: the run's whole plan.")
    plan_id: Optional[str] = None
    actor: Optional[str] = Field(
        None,
        description=(
            "Display name typed in the console (at most 100 characters); used only when no "
            "authenticating proxy identifies the user."
        ),
    )
    detail: Optional[dict[str, Any]] = Field(
        None,
        description=(
            'plan_downloaded: {"format": "pdf"|"json"|"nema"|"zip"|...}; feedback: '
            '{"verdict": "accepted"|"rejected"|"needs_changes", "comment": "<= 1000 chars"}; '
            "otherwise empty."
        ),
    )

    @field_validator("corridor_id")
    @classmethod
    def _corridor(cls, value: str) -> str:
        text = _text(value, "corridor_id", 128)
        if text is None:
            raise ValueError("corridor_id must not be empty")
        return text

    @field_validator("run_id")
    @classmethod
    def _run(cls, value: Optional[str]) -> Optional[str]:
        return _text(value, "run_id", 64)

    @field_validator("intersection_id", "plan_id")
    @classmethod
    def _ids(cls, value: Optional[str], info) -> Optional[str]:
        return _text(value, info.field_name, 128)

    @field_validator("actor")
    @classmethod
    def _actor(cls, value: Optional[str]) -> Optional[str]:
        text = _text(value, "actor", MAX_ACTOR_CHARS)
        return " ".join(text.split()) if text else None

    @model_validator(mode="after")
    def _per_type(self) -> "AuditEventIn":
        if self.type in _NEEDS_RUN and not self.run_id:
            raise ValueError(f"run_id is required for {self.type} events")
        self.detail = _detail(self.type, self.detail or {})
        return self


def _detail(kind: str, detail: dict) -> dict:
    """The validated, normalized ``detail`` of one event type."""
    allowed = {"plan_downloaded": {"format"}, "feedback": {"verdict", "comment"}}.get(kind, set())
    unknown = sorted(set(detail) - allowed)
    if unknown:
        raise ValueError(
            f"detail of {kind} events takes {sorted(allowed) or 'no keys'}, not {unknown}"
        )
    if kind == "plan_downloaded":
        value = detail.get("format")
        if not isinstance(value, str) or not _FORMAT.match(value):
            raise ValueError('detail.format is required: a short lower-case token such as "pdf"')
        return {"format": value}
    if kind == "feedback":
        return _feedback_detail(detail)
    return {}


def _feedback_detail(detail: dict) -> dict:
    verdict = detail.get("verdict")
    if verdict not in VERDICTS:
        raise ValueError(f"detail.verdict must be one of {list(VERDICTS)}")
    out = {"verdict": verdict}
    comment = detail.get("comment")
    if comment is None:
        return out
    if not isinstance(comment, str):
        raise ValueError("detail.comment must be a string")
    comment = comment.replace("\r\n", "\n").replace("\r", "\n").strip()
    if _COMMENT_CONTROL.search(comment):
        raise ValueError("detail.comment must not contain control characters")
    if len(comment) > MAX_COMMENT_CHARS:
        raise ValueError(f"detail.comment is longer than {MAX_COMMENT_CHARS} characters")
    if comment:
        out["comment"] = comment
    return out


class AuditEvent(BaseModel):
    """One recorded event."""

    id: str = Field(..., description="32 hex characters.")
    at: str = Field(..., description="Service time, ISO-8601 UTC with milliseconds.")
    type: EventType
    corridor_id: str
    run_id: Optional[str] = None
    intersection_id: Optional[str] = None
    plan_id: Optional[str] = None
    actor: str = Field(..., description='Who: proxy identity, display name, or "anonymous".')
    actor_source: Literal["proxy", "self", "anonymous"]
    detail: dict[str, Any]


class AuditSummary(BaseModel):
    """Usage of the last N days."""

    corridor_id: Optional[str] = None
    days: int
    timezone: str = Field(
        ..., description="Zone of the day buckets (UTC when the requested one is unknown)."
    )
    since: str = Field(..., description="Start of the first day, ISO-8601 UTC.")
    until: str = Field(..., description="Now, ISO-8601 UTC.")
    totals: dict[str, int] = Field(..., description="Events per type in the window, and total.")
    per_day: list[dict[str, Any]] = Field(..., description="One entry per local day, oldest first.")
    downloads_by_format: dict[str, int]
    feedback: dict[str, Any] = Field(
        ...,
        description=(
            "events (all feedback events), accepted / rejected / needs_changes and "
            "acceptance_rate over each user's latest verdict per plan (null without verdicts)."
        ),
    )
    unique_actors: int = Field(..., description="Distinct actors other than anonymous.")
    anonymous_events: int
    actors: list[dict[str, Any]] = Field(..., description="Up to 20 actors, most events first.")
    last_activity: Optional[dict[str, Any]] = Field(
        None, description="The newest event of the corridor at any time."
    )


class _RateLimit:
    """At most ``per_minute`` accepted events per fixed one-minute window."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._per_minute = per_minute
        self._clock = clock
        self._lock = threading.Lock()
        self._start: Optional[float] = None
        self._count = 0

    def retry_after(self) -> Optional[int]:
        """None and count the event when allowed, else the seconds to wait."""
        now = self._clock()
        with self._lock:
            if self._start is None or now - self._start >= 60.0:
                self._start, self._count = now, 0
            if self._count >= self._per_minute:
                return max(1, math.ceil(60.0 - (now - self._start)))
            self._count += 1
            return None


def _actor(request: Request, event: AuditEventIn) -> tuple[str, str]:
    """``(actor, actor_source)``: the proxy's identity header, the typed name, or anonymous."""
    for name in IDENTITY_HEADERS:
        raw = request.headers.get(name)
        if raw:
            value = " ".join(_CONTROL.sub(" ", raw).split())[:MAX_PROXY_ACTOR_CHARS].strip()
            if value:
                return value, "proxy"
    if event.actor:
        return event.actor, "self"
    return ANONYMOUS, "anonymous"


async def _json_object(request: Request) -> dict:
    """The request body as a JSON object: 415 unless JSON, 413 above 8 KiB, 422 unparsable."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="the body must be application/json")
    too_large = f"the body is larger than {MAX_BODY_BYTES} bytes"
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=too_large)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail=too_large)
    try:
        payload = json.loads(bytes(body))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="the body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="the body must be a JSON object")
    return payload


def _bound(value: Optional[str], name: str) -> Optional[str]:
    if value is None or value == "":
        return None
    try:
        return parse_bound(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{name} must be an ISO-8601 date-time or date, got {value!r}"
        ) from exc


def _validation_detail(exc: ValidationError) -> list:
    return [
        {
            "loc": ["body", *err.get("loc", ())],
            "msg": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in exc.errors()
    ]


_DISABLED = {404: {"description": "The audit log is disabled on this deployment"}}
_SINCE = "Inclusive; ISO-8601 date-time or date (UTC)."
_UNTIL = "Exclusive; ISO-8601 date-time or date (UTC)."


def register_audit_routes(
    router: APIRouter,
    *,
    output_root: Path,
    enabled: Optional[bool] = None,
    clock: Callable[[], float] = time.time,
    max_events_per_minute: int = MAX_EVENTS_PER_MINUTE,
) -> AuditLog:
    """Add the audit endpoints to ``router`` (prefix ``/v1/city``); returns the log.

    ``enabled`` None reads ``TERASIM_CITY_AUDIT_ENABLED`` now (default on).
    """
    log = AuditLog.under(Path(output_root), clock=clock)
    is_enabled = audit_enabled() if enabled is None else bool(enabled)
    limiter = _RateLimit(max_events_per_minute)

    def require_enabled() -> None:
        if not is_enabled:
            raise HTTPException(status_code=404, detail=DISABLED_DETAIL)

    def filters(corridor_id, run_id, intersection_id, actor, event_type, since, until) -> dict:
        return {
            "corridor_id": corridor_id or None,
            "run_id": run_id or None,
            "intersection_id": intersection_id or None,
            "actor": actor or None,
            "event_type": event_type,
            "since": _bound(since, "since"),
            "until": _bound(until, "until"),
        }

    @router.post(
        "/audit/events",
        status_code=201,
        response_model=AuditEvent,
        tags=["city-audit"],
        responses={
            **_DISABLED,
            413: {"description": "Body larger than 8 KiB"},
            415: {"description": "Body not application/json"},
            422: {"description": "Invalid event (the detail says why)"},
            429: {"description": "Too many events per minute (Retry-After)"},
            503: {"description": "The log cannot be written (the detail says why)"},
        },
        summary="Record a console event (feedback, plan view or download, session)",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": AuditEventIn.model_json_schema()}},
            }
        },
    )
    async def post_event(request: Request):
        """Record one operator event. The service stamps id, time (UTC) and actor.

        actor: the authenticating proxy's X-Forwarded-User / X-Forwarded-Email /
        X-Remote-User (actor_source "proxy"), else the body's display name ("self"),
        else "anonymous".
        """
        require_enabled()
        payload = await _json_object(request)
        try:
            event = AuditEventIn.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_validation_detail(exc)) from exc
        actor, source = _actor(request, event)
        wait = limiter.retry_after()
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail=f"more than {max_events_per_minute} events in a minute; try again later",
                headers={"Retry-After": str(wait)},
            )
        record = {
            "type": event.type,
            "corridor_id": event.corridor_id,
            "run_id": event.run_id,
            "intersection_id": event.intersection_id,
            "plan_id": event.plan_id,
            "actor": actor,
            "actor_source": source,
            "detail": event.detail,
        }
        try:
            stored = await run_in_threadpool(log.append, record)
        except AuditLogUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(status_code=201, content=stored)

    @router.get(
        "/audit/events",
        response_model=None,
        tags=["city-audit"],
        responses={200: {"model": list[AuditEvent], "description": "Events, newest first"},
                   **_DISABLED, 422: {"description": "Invalid filter"}},
        summary="Audit log events, newest first",
    )
    def list_events(
        corridor_id: Optional[str] = Query(None),
        run_id: Optional[str] = Query(None),
        intersection_id: Optional[str] = Query(None),
        actor: Optional[str] = Query(None),
        event_type: Optional[EventType] = Query(None, alias="type"),
        since: Optional[str] = Query(None, description=_SINCE),
        until: Optional[str] = Query(None, description=_UNTIL),
        limit: int = Query(
            DEFAULT_LISTED_EVENTS,
            ge=1,
            description=f"At most {MAX_LISTED_EVENTS} (larger values return that many).",
        ),
    ):
        """Recorded events matching every given filter, newest first."""
        require_enabled()
        found = log.query(
            **filters(corridor_id, run_id, intersection_id, actor, event_type, since, until),
            limit=min(limit, MAX_LISTED_EVENTS),
        )
        return JSONResponse(content=found)

    @router.get(
        "/audit/events.csv",
        response_class=StreamingResponse,
        tags=["city-audit"],
        responses={200: {"content": {"text/csv": {}}, "description": "CSV download"},
                   **_DISABLED, 422: {"description": "Invalid filter"}},
        summary="Audit log events as CSV",
    )
    def export_events(
        corridor_id: Optional[str] = Query(None),
        run_id: Optional[str] = Query(None),
        intersection_id: Optional[str] = Query(None),
        actor: Optional[str] = Query(None),
        event_type: Optional[EventType] = Query(None, alias="type"),
        since: Optional[str] = Query(None, description=_SINCE),
        until: Optional[str] = Query(None, description=_UNTIL),
        limit: int = Query(
            DEFAULT_CSV_EVENTS,
            ge=1,
            description=f"At most {MAX_CSV_EVENTS} (larger values return that many).",
        ),
    ):
        """The events of GET /audit/events as a CSV file (UTF-8 with BOM), newest first."""
        require_enabled()
        found = log.query(
            **filters(corridor_id, run_id, intersection_id, actor, event_type, since, until),
            limit=min(limit, MAX_CSV_EVENTS),
        )
        scope = re.sub(r"[^A-Za-z0-9_.-]+", "_", corridor_id)[:64] if corridor_id else "all"
        day = datetime.fromtimestamp(clock(), tz=timezone.utc).strftime("%Y%m%d")
        return StreamingResponse(
            csv_rows(found),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="audit_{scope}_{day}.csv"'},
        )

    @router.get(
        "/audit/summary",
        response_model=AuditSummary,
        tags=["city-audit"],
        responses={**_DISABLED, 422: {"description": "days outside 1-366"}},
        summary="Console usage and feedback of the last N days",
    )
    def summary(
        corridor_id: Optional[str] = Query(None, description="Only this corridor (default: all)."),
        days: int = Query(30, ge=1, le=MAX_SUMMARY_DAYS),
        tz: Optional[str] = Query(None, description="IANA zone of the day buckets (default UTC)."),
    ):
        """Per-day counts by event type, users, feedback verdicts and acceptance rate."""
        require_enabled()
        return log.summary(corridor_id=corridor_id or None, days=days, tz_name=tz)

    return log


__all__ = [
    "DISABLED_DETAIL",
    "IDENTITY_HEADERS",
    "MAX_BODY_BYTES",
    "MAX_EVENTS_PER_MINUTE",
    "AuditEvent",
    "AuditEventIn",
    "AuditSummary",
    "audit_enabled",
    "register_audit_routes",
]
