"""Append-only audit log of what operators do with the console: feedback and usage.

The operator console records four kinds of events:
``console_opened``, ``plan_viewed``, ``plan_downloaded`` and ``feedback`` (an operator's
verdict on a plan, with an optional comment). They are kept as JSON Lines under the
service's output root, so they survive restarts on the same volume as the runs::

    <output_root>/audit/events-YYYY-MM.jsonl   one event per line, UTC month of its time
    <output_root>/audit/.lock                  flock: writers exclusive, readers shared

A writer takes the exclusive lock, stamps the event's time, terminates a line a crash
may have left incomplete, appends one line and fsyncs; readers take the shared lock.
That holds across threads, uvicorn workers and processes sharing the volume. Readers
skip lines that do not parse. Files are never rewritten or pruned by the service.

This module is storage and queries only (no HTTP); :mod:`terasim_service.city.audit_api`
validates requests and serves the endpoints.
"""

from __future__ import annotations

import csv
import fcntl
import io
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import Counter, OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

AUDIT_DIRNAME = "audit"
LOCK_FILENAME = ".lock"
EVENT_TYPES = ("console_opened", "plan_viewed", "plan_downloaded", "feedback")
VERDICTS = ("accepted", "rejected", "needs_changes")
ANONYMOUS = "anonymous"
#: A month's file stops growing at this size (about 150 000 events): a runaway client
#: must not fill the volume the optimization runs live on.
MAX_FILE_BYTES = 64 * 1024 * 1024
#: Parsed month files kept in memory (re-read incrementally when they grow).
_CACHE_FILES = 4
#: Actors listed in the summary.
SUMMARY_ACTORS = 20
CSV_COLUMNS = (
    "id", "at", "type", "actor", "actor_source", "corridor_id", "run_id",
    "intersection_id", "plan_id", "verdict", "comment", "format",
)
#: Spreadsheet programs evaluate cells starting with these as formulas.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_FILE_RE = re.compile(r"^events-(\d{4})-(\d{2})\.jsonl$")
_FRACTION = re.compile(r"(T\d{2}:\d{2}:\d{2})\.(\d+)")
_EVENT_KEYS = (
    "id", "at", "type", "corridor_id", "run_id", "intersection_id", "plan_id",
    "actor", "actor_source", "detail",
)


class AuditLogUnavailable(Exception):
    """The event cannot be written (storage error, or the month's file is full)."""


def format_at(timestamp: float) -> str:
    """Epoch seconds as ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (UTC, milliseconds, fixed width).

    The fixed width makes the strings sort like the times they name.
    """
    millis = int(timestamp * 1000)
    moment = datetime.fromtimestamp(millis // 1000, tz=timezone.utc)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{millis % 1000:03d}Z"


def parse_at(value: str) -> datetime:
    """The aware UTC datetime of a stored ``at``."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def parse_bound(value: str) -> str:
    """A query bound (ISO-8601 date-time or date) as a stored-``at`` string.

    ``2026-10-11`` is 00:00 UTC; a date-time without an offset is UTC; ``Z`` and
    offsets are accepted. Raises ValueError for anything else.
    """
    text = value.strip()
    if not text:
        raise ValueError("empty date-time")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            moment = datetime.combine(date.fromisoformat(text), datetime.min.time(), timezone.utc)
        else:
            if text[-1] in "zZ":
                text = text[:-1] + "+00:00"
            # Python 3.10's fromisoformat takes only 3 or 6 fraction digits.
            text = _FRACTION.sub(
                lambda m: f"{m.group(1)}.{(m.group(2) + '00000')[:6]}", text, count=1
            )
            moment = datetime.fromisoformat(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        return format_at(moment.timestamp())
    except (OverflowError, OSError) as exc:  # out of the platform's range
        raise ValueError(f"date-time out of range: {value!r}") from exc


def resolve_zone(name: Optional[str]) -> tuple[tzinfo, str]:
    """``(zone, its name)`` for an IANA zone name; UTC when empty or unknown."""
    if name and name.strip() and name.strip().upper() != "UTC":
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name.strip()), name.strip()
        except Exception:  # noqa: BLE001 - unknown or invalid zone, or no tz database
            logger.info("unknown time zone %r for the audit summary; using UTC", name)
    return timezone.utc, "UTC"


@dataclass(frozen=True)
class _Parsed:
    ino: int
    size: int
    events: tuple


def _parse_lines(data: bytes) -> list:
    events = []
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue  # a line a crash left incomplete
        if isinstance(event, dict) and isinstance(event.get("at"), str) and event.get("type"):
            events.append(event)
    return events


def _month_bounds(path: Path) -> Optional[tuple[str, str]]:
    """``(start, end)`` of a month file as stored-``at`` strings, or None for other files."""
    match = _FILE_RE.match(path.name)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    try:
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=timezone.utc)
        return format_at(start.timestamp()), format_at(end.timestamp())
    except (ValueError, OverflowError, OSError):
        return None  # not a month this log could have written (e.g. events-0000-13.jsonl)


class AuditLog:
    """The audit events of one deployment, in ``directory`` (``<output_root>/audit``)."""

    def __init__(
        self,
        directory: Path,
        *,
        clock: Callable[[], float] = time.time,
        max_file_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        self.directory = Path(directory)
        self._clock = clock
        self._max_file_bytes = max_file_bytes
        self._cache: "OrderedDict[str, _Parsed]" = OrderedDict()
        self._cache_lock = threading.Lock()

    @classmethod
    def under(cls, output_root: Path, **kwargs) -> "AuditLog":
        """The log kept under a service output root."""
        return cls(Path(output_root) / AUDIT_DIRNAME, **kwargs)

    # ------------------------------------------------------------------ locking

    @contextmanager
    def _locked(self, exclusive: bool):
        path = self.directory / LOCK_FILENAME
        if exclusive:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
        else:
            try:
                fd = os.open(path, os.O_RDONLY)
            except OSError:
                try:
                    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
                except OSError:
                    fd = None  # nothing written yet, or a read-only volume: read without it
        try:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            if fd is not None:
                os.close(fd)  # releases the lock

    # ------------------------------------------------------------------ writing

    def append(self, record: dict) -> dict:
        """Stamp ``id`` and ``at`` on ``record`` (the other §7.1 keys), append it, return it.

        Raises AuditLogUnavailable when the line cannot be written.
        """
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._locked(exclusive=True):
                merged = {**record, "id": uuid.uuid4().hex, "at": format_at(self._clock())}
                event = {key: merged.get(key) for key in _EVENT_KEYS}
                event["detail"] = dict(record.get("detail") or {})
                text = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                line = (text + "\n").encode("utf-8")
                self._append_line(self.directory / f"events-{event['at'][:7]}.jsonl", line)
        except AuditLogUnavailable:
            raise
        except OSError as exc:
            logger.warning("could not write an audit event to %s: %s", self.directory, exc)
            reason = exc.strerror or str(exc)
            raise AuditLogUnavailable(f"the audit log cannot be written: {reason}") from exc
        return event

    def _append_line(self, path: Path, line: bytes) -> None:
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o666)
        try:
            size = os.fstat(fd).st_size
            if size + len(line) > self._max_file_bytes:
                raise AuditLogUnavailable(
                    f"the audit log for {path.name[7:14]} reached its size limit "
                    f"({self._max_file_bytes // (1024 * 1024)} MiB)"
                )
            if size and os.pread(fd, 1, size - 1) != b"\n":
                line = b"\n" + line  # terminate a line a crash left incomplete
            view = memoryview(line)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    # ------------------------------------------------------------------ reading

    def _month_files(self) -> list[Path]:
        try:
            names = os.listdir(self.directory)
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("cannot read the audit log in %s: %s", self.directory, exc)
            return []
        return sorted((self.directory / n for n in names if _FILE_RE.match(n)), reverse=True)

    def _load(self, path: Path) -> tuple:
        """The events of one month file, oldest first (cached; growth read incrementally)."""
        key = str(path)
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            return ()
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached is not None and cached.ino == stat.st_ino and cached.size == stat.st_size:
            events = cached.events
        else:
            offset = 0
            base: tuple = ()
            if cached is not None and cached.ino == stat.st_ino and cached.size < stat.st_size:
                offset, base = cached.size, cached.events
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(stat.st_size - offset)
            events = base + tuple(_parse_lines(data))
            cached = _Parsed(stat.st_ino, offset + len(data), events)
        with self._cache_lock:
            self._cache[key] = cached
            self._cache.move_to_end(key)
            while len(self._cache) > _CACHE_FILES:
                self._cache.popitem(last=False)
        return events

    def newest_first(
        self, *, since: Optional[str] = None, until: Optional[str] = None
    ) -> Iterator[dict]:
        """Every event, newest first, from the month files that can hold [since, until).

        Within a month file the order is the order of appending (``at`` is stamped
        under the writers' lock). Events outside the bounds may still be yielded;
        callers compare ``at`` themselves.
        """
        files = []
        with self._locked(exclusive=False):
            for path in self._month_files():
                bounds = _month_bounds(path)
                if bounds is None:
                    continue
                start, end = bounds
                if until is not None and start >= until:
                    continue
                if since is not None and end <= since:
                    break  # files are newest first: every further one is older
                files.append(path)
            loaded = [self._load(path) for path in files]
        for events in loaded:
            yield from reversed(events)

    def query(
        self,
        *,
        corridor_id: Optional[str] = None,
        run_id: Optional[str] = None,
        intersection_id: Optional[str] = None,
        actor: Optional[str] = None,
        event_type: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        """Events matching every given filter, newest first, at most ``limit``."""
        wanted = {
            key: value
            for key, value in (
                ("corridor_id", corridor_id), ("run_id", run_id),
                ("intersection_id", intersection_id), ("actor", actor), ("type", event_type),
            )
            if value is not None
        }
        out: list[dict] = []
        if limit <= 0:
            return out
        for event in self.newest_first(since=since, until=until):
            at = event["at"]
            if (since is not None and at < since) or (until is not None and at >= until):
                continue
            if any(event.get(key) != value for key, value in wanted.items()):
                continue
            out.append(event)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ summary

    def summary(
        self, *, corridor_id: Optional[str] = None, days: int = 30, tz_name: Optional[str] = None
    ) -> dict:
        """Usage of the last ``days`` local days."""
        zone, zone_name = resolve_zone(tz_name)
        now = self._clock()
        today = datetime.fromtimestamp(now, tz=zone).date()
        first = today - timedelta(days=days - 1)
        start = datetime(first.year, first.month, first.day, tzinfo=zone)
        since = format_at(start.timestamp())
        per_day = OrderedDict(
            ((first + timedelta(days=i)).isoformat(), Counter()) for i in range(days)
        )
        totals: Counter = Counter()
        formats: Counter = Counter()
        actors: dict = {}
        latest_verdicts: dict = {}
        last_activity = None
        for event in self.newest_first(since=since):
            if corridor_id is not None and event.get("corridor_id") != corridor_id:
                continue
            if last_activity is None:
                last_activity = event
            kind = event["type"]
            if event["at"] < since or kind not in EVENT_TYPES:
                continue
            try:
                day = parse_at(event["at"]).astimezone(zone).date().isoformat()
            except ValueError:
                continue
            if day not in per_day:
                continue
            per_day[day][kind] += 1
            totals[kind] += 1
            detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
            name = event.get("actor") or ANONYMOUS
            seen = actors.setdefault(name, {
                "actor": name, "actor_source": event.get("actor_source"),
                "events": 0, "last_at": event["at"],
            })
            seen["events"] += 1
            if kind == "plan_downloaded" and detail.get("format"):
                formats[str(detail["format"])] += 1
            if kind == "feedback" and detail.get("verdict") in VERDICTS:
                # newest first: the first verdict seen per plan and user is the latest
                latest_verdicts.setdefault(
                    (event.get("run_id"), event.get("intersection_id"), name), detail["verdict"]
                )
        if last_activity is None:
            last_activity = next(
                (e for e in self.newest_first()
                 if corridor_id is None or e.get("corridor_id") == corridor_id),
                None,
            )
        verdicts = Counter(latest_verdicts.values())
        decided = sum(verdicts[v] for v in VERDICTS)
        listed = sorted(actors.values(), key=lambda a: (-a["events"], a["actor"]))[:SUMMARY_ACTORS]
        return {
            "corridor_id": corridor_id,
            "days": days,
            "timezone": zone_name,
            "since": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "until": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "totals": {**{k: totals[k] for k in EVENT_TYPES}, "total": sum(totals.values())},
            "per_day": [
                {"date": day, **{k: counts[k] for k in EVENT_TYPES}, "total": sum(counts.values())}
                for day, counts in per_day.items()
            ],
            "downloads_by_format": dict(sorted(formats.items(), key=lambda kv: (-kv[1], kv[0]))),
            "feedback": {
                "events": totals["feedback"],
                **{v: verdicts[v] for v in VERDICTS},
                "acceptance_rate": (verdicts["accepted"] / decided) if decided else None,
            },
            "unique_actors": sum(1 for name in actors if name != ANONYMOUS),
            "anonymous_events": actors.get(ANONYMOUS, {}).get("events", 0),
            "actors": listed,
            "last_activity": (
                {"at": last_activity["at"], "type": last_activity["type"],
                 "actor": last_activity.get("actor") or ANONYMOUS}
                if last_activity is not None else None
            ),
        }


# ---------------------------------------------------------------------- CSV


def _cell(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def csv_rows(events: Iterable[dict], *, chunk: int = 500) -> Iterator[str]:
    """The CSV export of ``events`` in pieces: BOM + header, then rows."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    buffer.write("﻿")
    writer.writerow(CSV_COLUMNS)
    pending = 0
    for event in events:
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        writer.writerow([_cell(v) for v in (
            event.get("id"), event.get("at"), event.get("type"), event.get("actor"),
            event.get("actor_source"), event.get("corridor_id"), event.get("run_id"),
            event.get("intersection_id"), event.get("plan_id"), detail.get("verdict"),
            detail.get("comment"), detail.get("format"),
        )])
        pending += 1
        if pending >= chunk:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
            pending = 0
    yield buffer.getvalue()


__all__ = [
    "ANONYMOUS",
    "AUDIT_DIRNAME",
    "CSV_COLUMNS",
    "EVENT_TYPES",
    "MAX_FILE_BYTES",
    "VERDICTS",
    "AuditLog",
    "AuditLogUnavailable",
    "csv_rows",
    "format_at",
    "parse_at",
    "parse_bound",
    "resolve_zone",
]
