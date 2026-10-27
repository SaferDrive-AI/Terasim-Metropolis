"""Audit log of the console's operator events.

POST /v1/city/audit/events validates and records feedback, plan views, plan downloads
and console sessions; GET /v1/city/audit/events, /events.csv and /summary read them
back. Events are JSON Lines under <output_root>/audit/, appended under a file lock.
"""

from __future__ import annotations

import csv
import io
import json
import multiprocessing
import threading
from datetime import datetime
from pathlib import Path
from typing import get_args

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from terasim_service.city import audit_api
from terasim_service.city.audit_api import (
    DISABLED_DETAIL,
    MAX_BODY_BYTES,
    EventType,
    register_audit_routes,
)
from terasim_service.city.audit_log import (
    CSV_COLUMNS,
    EVENT_TYPES,
    AuditLog,
    AuditLogUnavailable,
    format_at,
    parse_bound,
)
from terasim_service.city.job_store import InMemoryCityJobStore
from terasim_service.city.optimize_api import create_v1_city_router

_EVENTS = "/v1/city/audit/events"
_CSV = "/v1/city/audit/events.csv"
_SUMMARY = "/v1/city/audit/summary"
_EVENT_KEYS = {
    "id", "at", "type", "corridor_id", "run_id", "intersection_id", "plan_id",
    "actor", "actor_source", "detail",
}


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


class _Clock:
    """A settable wall clock (epoch seconds)."""

    def __init__(self, iso: str = "2026-10-11T17:00:00Z") -> None:
        self.now = _ts(iso)

    def set(self, iso: str) -> None:
        self.now = _ts(iso)

    def __call__(self) -> float:
        return self.now


def _client(output_root: Path, *, clock=None, **kwargs) -> TestClient:
    router = APIRouter(prefix="/v1/city")
    register_audit_routes(router, output_root=output_root, clock=clock or _Clock(), **kwargs)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _feedback(
    verdict="accepted", *, run="r1", corridor="c1", intersection=None, comment=None, actor=None
):
    detail = {"verdict": verdict}
    if comment is not None:
        detail["comment"] = comment
    body = {"type": "feedback", "corridor_id": corridor, "run_id": run, "detail": detail}
    if intersection is not None:
        body["intersection_id"] = intersection
    if actor is not None:
        body["actor"] = actor
    return body


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def client(tmp_path, clock) -> TestClient:
    return _client(tmp_path / "out", clock=clock, enabled=True)


# ---------------------------------------------------------------------- recording


def test_post_records_and_returns_the_stamped_event(client, clock, tmp_path) -> None:
    res = client.post(_EVENTS, json=_feedback("needs_changes", intersection="main_1st",
                                             comment="  Offset at 1st Ave\r\nlooks short  "))
    assert res.status_code == 201
    event = res.json()
    assert set(event) == _EVENT_KEYS
    assert len(event["id"]) == 32 and int(event["id"], 16) >= 0
    assert event["at"] == "2026-10-11T17:00:00.000Z"
    assert event["type"] == "feedback"
    assert (event["corridor_id"], event["run_id"], event["intersection_id"], event["plan_id"]) == (
        "c1", "r1", "main_1st", None)
    assert (event["actor"], event["actor_source"]) == ("anonymous", "anonymous")
    assert event["detail"] == {
        "verdict": "needs_changes", "comment": "Offset at 1st Ave\nlooks short"}
    lines = (tmp_path / "out" / "audit" / "events-2026-10.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == [event]


@pytest.mark.parametrize(
    "body",
    [
        {"type": "console_opened", "corridor_id": "c1"},
        {"type": "console_opened", "corridor_id": "c1", "run_id": "r1", "detail": {}},
        {"type": "plan_viewed", "corridor_id": "c1", "run_id": "r1", "intersection_id": "i1",
         "plan_id": "bo_0007"},
        {"type": "plan_downloaded", "corridor_id": "c1", "run_id": "r1",
         "detail": {"format": "nema"}},
        {"type": "plan_downloaded", "corridor_id": "c1", "run_id": "r1",
         "detail": {"format": "zip"}},
        {"type": "feedback", "corridor_id": "c1", "run_id": "r1",
         "detail": {"verdict": "rejected"}},
        {"type": "feedback", "corridor_id": "c1", "run_id": "r1",
         "detail": {"verdict": "accepted", "comment": "x" * 1000}},
    ],
)
def test_valid_events_are_accepted(client, body) -> None:
    res = client.post(_EVENTS, json=body)
    assert res.status_code == 201, res.text
    assert res.json()["type"] == body["type"]


def test_empty_optional_fields_are_absent(client) -> None:
    event = client.post(_EVENTS, json={
        "type": "feedback", "corridor_id": " c1 ", "run_id": "r1", "intersection_id": "  ",
        "plan_id": "", "actor": "", "detail": {"verdict": "accepted", "comment": "   "},
    }).json()
    assert event["corridor_id"] == "c1"
    assert event["intersection_id"] is None and event["plan_id"] is None
    assert event["actor_source"] == "anonymous"
    assert event["detail"] == {"verdict": "accepted"}


@pytest.mark.parametrize(
    "body, fragment",
    [
        ({"type": "opened", "corridor_id": "c1"}, "type"),
        ({"corridor_id": "c1"}, "type"),
        ({"type": "console_opened"}, "corridor_id"),
        ({"type": "console_opened", "corridor_id": "   "}, "corridor_id must not be empty"),
        ({"type": "console_opened", "corridor_id": "c" * 129}, "longer than 128"),
        ({"type": "console_opened", "corridor_id": 7}, "corridor_id"),
        ({"type": "console_opened", "corridor_id": "c1\nc2"}, "control characters"),
        ({"type": "console_opened", "corridor_id": "c1", "extra": 1}, "extra"),
        ({"type": "console_opened", "corridor_id": "c1", "detail": {"page": "x"}}, "no keys"),
        ({"type": "console_opened", "corridor_id": "c1", "actor": "a" * 101}, "longer than 100"),
        ({"type": "plan_viewed", "corridor_id": "c1"}, "run_id is required"),
        ({"type": "plan_viewed", "corridor_id": "c1", "run_id": "r" * 65}, "longer than 64"),
        ({"type": "plan_viewed", "corridor_id": "c1", "run_id": "r1", "intersection_id": "i" * 129},
         "longer than 128"),
        ({"type": "plan_downloaded", "corridor_id": "c1", "run_id": "r1"}, "detail.format"),
        ({"type": "plan_downloaded", "corridor_id": "c1", "run_id": "r1",
          "detail": {"format": "PDF!"}},
         "detail.format"),
        ({"type": "feedback", "corridor_id": "c1", "run_id": "r1"}, "detail.verdict"),
        ({"type": "feedback", "corridor_id": "c1", "run_id": "r1", "detail": {"verdict": "maybe"}},
         "detail.verdict"),
        ({"type": "feedback", "corridor_id": "c1", "run_id": "r1",
          "detail": {"verdict": "accepted", "comment": "x" * 1001}}, "longer than 1000"),
        ({"type": "feedback", "corridor_id": "c1", "run_id": "r1",
          "detail": {"verdict": "accepted", "comment": 5}}, "comment must be a string"),
        ({"type": "feedback", "corridor_id": "c1", "run_id": "r1",
          "detail": {"verdict": "accepted", "rating": 5}}, "rating"),
    ],
)
def test_invalid_events_are_rejected_with_the_reason(client, tmp_path, body, fragment) -> None:
    res = client.post(_EVENTS, json=body)
    assert res.status_code == 422
    assert fragment in json.dumps(res.json()["detail"])
    audit = tmp_path / "out" / "audit"
    assert not audit.exists() or not list(audit.glob("*.jsonl"))


def test_body_must_be_a_json_object_of_at_most_8_kib(client) -> None:
    bad_json = client.post(
        _EVENTS, content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert bad_json.status_code == 422 and "not valid JSON" in bad_json.json()["detail"]
    array = client.post(_EVENTS, json=[{"type": "console_opened", "corridor_id": "c1"}])
    assert array.status_code == 422 and "JSON object" in array.json()["detail"]
    big = {"type": "console_opened", "corridor_id": "c1", "pad": "x" * MAX_BODY_BYTES}
    assert client.post(_EVENTS, json=big).status_code == 413
    # Without a Content-Length (chunked), the size is checked while reading.
    chunks = iter([b'{"type": "console_opened", "corridor_id": "', b"x" * MAX_BODY_BYTES, b'"}'])
    res = client.post(_EVENTS, content=chunks, headers={"Content-Type": "application/json"})
    assert res.status_code == 413


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded",
                                          "multipart/form-data; boundary=x"])
def test_only_json_bodies_are_accepted(client, content_type) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    res = client.post(
        _EVENTS, content=b'{"type": "console_opened", "corridor_id": "c1"}', headers=headers
    )
    assert res.status_code == 415
    ok = client.post(_EVENTS, content=b'{"type": "console_opened", "corridor_id": "c1"}',
                     headers={"Content-Type": "application/json; charset=utf-8"})
    assert ok.status_code == 201


# ---------------------------------------------------------------------- actor


@pytest.mark.parametrize(
    "headers, body_actor, expected",
    [
        ({"X-Forwarded-User": "jsmith", "X-Forwarded-Email": "j@city.gov", "X-Remote-User": "r"},
         "Typed", ("jsmith", "proxy")),
        ({"X-Forwarded-Email": "j@city.gov", "X-Remote-User": "r"}, "Typed",
         ("j@city.gov", "proxy")),
        ({"X-Remote-User": "remote1"}, "Typed", ("remote1", "proxy")),
        ({"X-Forwarded-User": "   "}, "  Jane   Doe ", ("Jane Doe", "self")),
        ({}, None, ("anonymous", "anonymous")),
        ({"X-Forwarded-User": " a\tb "}, None, ("a b", "proxy")),
        ({"X-Forwarded-User": "u" * 300}, None, ("u" * 200, "proxy")),
    ],
)
def test_actor_precedence(client, headers, body_actor, expected) -> None:
    body = {"type": "console_opened", "corridor_id": "c1"}
    if body_actor is not None:
        body["actor"] = body_actor
    event = client.post(_EVENTS, json=body, headers=headers).json()
    assert (event["actor"], event["actor_source"]) == expected


# ---------------------------------------------------------------------- persistence


def test_events_survive_a_new_app_instance(tmp_path, clock) -> None:
    first = _client(tmp_path / "out", clock=clock, enabled=True)
    posted = [first.post(_EVENTS, json=_feedback(v)).json() for v in ("accepted", "rejected")]
    second = _client(tmp_path / "out", clock=clock, enabled=True)
    assert second.get(_EVENTS).json() == posted[::-1]
    # ...and keeps appending to the same file.
    third = second.post(_EVENTS, json=_feedback("needs_changes")).json()
    assert first.get(_EVENTS).json() == [third, *posted[::-1]]


def test_months_are_separate_files_read_newest_first(tmp_path, clock) -> None:
    client = _client(tmp_path / "out", clock=clock, enabled=True)
    clock.set("2026-09-30T23:59:59.999Z")
    september = client.post(_EVENTS, json=_feedback()).json()
    clock.set("2026-10-01T00:00:00Z")
    october = client.post(_EVENTS, json=_feedback()).json()
    audit = tmp_path / "out" / "audit"
    assert sorted(p.name for p in audit.glob("*.jsonl")) == [
        "events-2026-09.jsonl", "events-2026-10.jsonl"]
    assert [e["id"] for e in client.get(_EVENTS).json()] == [october["id"], september["id"]]
    assert client.get(_EVENTS, params={"until": "2026-10-01"}).json() == [september]
    assert client.get(_EVENTS, params={"since": "2026-10-01T00:00:00Z"}).json() == [october]


def test_an_incomplete_line_is_terminated_and_skipped(tmp_path, clock) -> None:
    audit = tmp_path / "out" / "audit"
    audit.mkdir(parents=True)
    (audit / "events-2026-10.jsonl").write_bytes(b'{"id": "x", "at": "2026-10-11T')
    client = _client(tmp_path / "out", clock=clock, enabled=True)
    event = client.post(_EVENTS, json=_feedback()).json()
    assert client.get(_EVENTS).json() == [event]
    data = (audit / "events-2026-10.jsonl").read_bytes()
    assert data.endswith(b"\n") and data.count(b"\n") == 2
    assert json.loads(data.splitlines()[1]) == event


def test_the_log_is_read_incrementally_as_it_grows(tmp_path, clock) -> None:
    log = AuditLog(tmp_path / "audit", clock=clock)
    first = log.append({"type": "console_opened", "corridor_id": "c1", "actor": "a",
                        "actor_source": "self"})
    assert log.query() == [first]
    second = log.append({"type": "console_opened", "corridor_id": "c1", "actor": "b",
                         "actor_source": "self"})
    assert log.query() == [second, first]
    # Another writer (a second process) appends to the same file.
    other = AuditLog(tmp_path / "audit", clock=clock)
    third = other.append({"type": "console_opened", "corridor_id": "c2", "actor": "c",
                          "actor_source": "self"})
    assert log.query() == [third, second, first]


def test_a_full_month_file_refuses_new_events(tmp_path, clock) -> None:
    log = AuditLog(tmp_path / "audit", clock=clock, max_file_bytes=400)
    log.append({"type": "console_opened", "corridor_id": "c1"})
    with pytest.raises(AuditLogUnavailable, match="2026-10 reached its size limit"):
        for _ in range(5):
            log.append({"type": "console_opened", "corridor_id": "c1"})
    assert len(log.query()) >= 1


def test_storage_errors_answer_503(tmp_path, clock) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "audit").write_text("not a directory")
    client = _client(out, clock=clock, enabled=True)
    res = client.post(_EVENTS, json=_feedback())
    assert res.status_code == 503
    assert "cannot be written" in res.json()["detail"]


def test_rate_limit_answers_429_with_retry_after(tmp_path, clock) -> None:
    client = _client(tmp_path / "out", clock=clock, enabled=True, max_events_per_minute=3)
    assert [client.post(_EVENTS, json=_feedback()).status_code for _ in range(3)] == [201] * 3
    res = client.post(_EVENTS, json=_feedback())
    assert res.status_code == 429
    assert 1 <= int(res.headers["Retry-After"]) <= 60
    assert len(client.get(_EVENTS).json()) == 3


# ---------------------------------------------------------------------- concurrency


def _append_events(directory: str, writer: int, count: int) -> None:
    log = AuditLog(Path(directory))
    for i in range(count):
        log.append({"type": "plan_viewed", "corridor_id": "c1", "run_id": f"w{writer}",
                    "intersection_id": f"i{i}", "actor": f"writer {writer}", "actor_source": "self",
                    "detail": {}})


def test_concurrent_appends_from_processes_and_threads(tmp_path) -> None:
    directory = tmp_path / "audit"
    ctx = multiprocessing.get_context("spawn")
    processes = [ctx.Process(target=_append_events, args=(str(directory), w, 60)) for w in range(4)]
    threads = [
        threading.Thread(target=_append_events, args=(str(directory), 10 + w, 60))
        for w in range(3)
    ]
    for worker in (*processes, *threads):
        worker.start()
    for proc in processes:
        proc.join(timeout=120)
        assert proc.exitcode == 0
    for thread in threads:
        thread.join(timeout=120)
    raw = b"".join(p.read_bytes() for p in directory.glob("events-*.jsonl"))
    lines = raw.split(b"\n")
    assert lines[-1] == b""  # every line terminated
    events = [json.loads(line) for line in lines[:-1]]
    assert len(events) == 7 * 60
    assert len({e["id"] for e in events}) == len(events)
    per_writer = {}
    for e in events:
        per_writer.setdefault(e["run_id"], []).append(int(e["intersection_id"][1:]))
    # each writer's own events stay in the order it appended them
    assert all(seq == list(range(60)) for seq in per_writer.values()) and len(per_writer) == 7
    assert len(AuditLog(directory).query(limit=10_000)) == 7 * 60


# ---------------------------------------------------------------------- listing


@pytest.fixture
def populated(client, clock):
    posted = []

    def post(iso, body, headers=None):
        clock.set(iso)
        res = client.post(_EVENTS, json=body, headers=headers or {})
        assert res.status_code == 201, res.text
        posted.append(res.json())

    post("2026-10-01T10:00:00Z", {"type": "console_opened", "corridor_id": "c1"})
    post("2026-10-02T10:00:00Z", {"type": "plan_viewed", "corridor_id": "c1", "run_id": "r1",
                                  "intersection_id": "i1"}, {"X-Forwarded-User": "ana"})
    post("2026-10-03T10:00:00Z", {"type": "plan_downloaded", "corridor_id": "c1", "run_id": "r1",
                                  "detail": {"format": "pdf"}}, {"X-Forwarded-User": "ana"})
    post("2026-10-04T10:00:00Z", _feedback("rejected", run="r1", actor="Bo"))
    post("2026-10-05T10:00:00Z", _feedback("accepted", run="r2", corridor="c2", actor="Bo"))
    return posted


def test_filters_and_limit(client, populated) -> None:
    def ids(**params):
        res = client.get(_EVENTS, params=params)
        assert res.status_code == 200, res.text
        return [e["id"] for e in res.json()]

    by_index = [e["id"] for e in populated]
    assert ids() == by_index[::-1]
    assert ids(corridor_id="c1") == by_index[3::-1]
    assert ids(run_id="r1") == [by_index[3], by_index[2], by_index[1]]
    assert ids(intersection_id="i1") == [by_index[1]]
    assert ids(actor="ana") == [by_index[2], by_index[1]]
    assert ids(type="feedback") == [by_index[4], by_index[3]]
    assert ids(type="feedback", corridor_id="c1") == [by_index[3]]
    assert ids(since="2026-10-03") == [by_index[4], by_index[3], by_index[2]]
    assert ids(since="2026-10-02T12:00:00+02:00", until="2026-10-04T10:00:00Z") == [
        by_index[2], by_index[1]]
    assert ids(until="2026-10-02T10:00:00.000Z") == [by_index[0]]
    assert ids(limit=2) == [by_index[4], by_index[3]]
    assert ids(limit=999_999) == by_index[::-1]  # above the maximum: clamped
    assert ids(corridor_id="nope") == []


@pytest.mark.parametrize("params", [{"limit": 0}, {"type": "opened"}, {"since": "yesterday"},
                                    {"until": "2026-13-01"}])
def test_invalid_filters_answer_422(client, params) -> None:
    assert client.get(_EVENTS, params=params).status_code == 422
    assert client.get(_CSV, params=params).status_code == 422


def test_listing_is_clamped_to_2000(tmp_path, clock) -> None:
    audit = tmp_path / "out" / "audit"
    audit.mkdir(parents=True)
    lines = [json.dumps({"id": f"{i:032x}", "at": format_at(clock() + i), "type": "console_opened",
                         "corridor_id": "c1", "run_id": None, "intersection_id": None,
                         "plan_id": None,
                         "actor": "anonymous", "actor_source": "anonymous", "detail": {}})
             for i in range(2005)]
    (audit / "events-2026-10.jsonl").write_text("\n".join(lines) + "\n")
    client = _client(tmp_path / "out", clock=clock, enabled=True)
    assert len(client.get(_EVENTS, params={"limit": 5000}).json()) == 2000
    assert len(client.get(_EVENTS).json()) == 200
    rows = list(csv.reader(io.StringIO(client.get(_CSV).text.lstrip("﻿"))))
    assert len(rows) == 1 + 2005


def test_empty_log(client) -> None:
    assert client.get(_EVENTS).json() == []
    assert client.get(_CSV).text == "﻿" + ",".join(CSV_COLUMNS) + "\r\n"
    summary = client.get(_SUMMARY).json()
    assert summary["totals"]["total"] == 0 and summary["last_activity"] is None
    assert summary["feedback"]["acceptance_rate"] is None


# ---------------------------------------------------------------------- CSV


def test_csv_export(client, populated, clock) -> None:
    clock.set("2026-10-06T08:00:00Z")
    tricky = client.post(_EVENTS, json=_feedback(
        "needs_changes", actor="-Eve",
        comment='=HYPERLINK("http://x")\nsecond line, "quoted"')).json()
    res = client.get(_CSV, params={"corridor_id": "c1"})
    assert res.status_code == 200
    assert res.headers["content-type"] == "text/csv; charset=utf-8"
    assert res.headers["content-disposition"] == 'attachment; filename="audit_c1_20261006.csv"'
    assert res.content.startswith("﻿".encode("utf-8"))
    assert "\r\n" in res.text
    rows = list(csv.DictReader(io.StringIO(res.content.decode("utf-8-sig"))))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert [r["id"] for r in rows] == [tricky["id"]] + [e["id"] for e in populated[3::-1]]
    first = rows[0]
    assert first["actor"] == "'-Eve" and first["actor_source"] == "self"
    assert first["comment"] == '\'=HYPERLINK("http://x")\nsecond line, "quoted"'
    assert first["verdict"] == "needs_changes" and first["format"] == ""
    download = next(r for r in rows if r["type"] == "plan_downloaded")
    assert (download["format"], download["actor"], download["run_id"],
            download["intersection_id"]) == (
        "pdf", "ana", "r1", "")
    everything = client.get(_CSV)
    assert 'filename="audit_all_20261006.csv"' in everything.headers["content-disposition"]
    assert len(list(csv.reader(io.StringIO(everything.content.decode("utf-8-sig"))))) == 1 + 6
    odd = client.get(_CSV, params={"corridor_id": "a b/c"})
    assert 'filename="audit_a_b_c_20261006.csv"' in odd.headers["content-disposition"]


# ---------------------------------------------------------------------- summary


def _summary_log(tmp_path, clock) -> TestClient:
    client = _client(tmp_path / "out", clock=clock, enabled=True)

    def post(iso, body, headers=None):
        clock.set(iso)
        assert client.post(_EVENTS, json=body, headers=headers or {}).status_code == 201

    # Long before the window: only counts for last_activity of corridor c3.
    post("2026-06-01T12:00:00Z", {"type": "console_opened", "corridor_id": "c3"})
    # 2026-10-09 18:00 PDT = 2026-10-10 01:00 UTC: a local Oct 9 event.
    post("2026-10-10T01:00:00Z", {"type": "console_opened", "corridor_id": "c1", "actor": "Bo"})
    post("2026-10-10T01:05:00Z", _feedback("rejected", run="r1", actor="Bo"))
    post("2026-10-10T16:00:00Z", {"type": "plan_viewed", "corridor_id": "c1", "run_id": "r1",
                                  "intersection_id": "i1"}, {"X-Forwarded-User": "ana"})
    post("2026-10-10T16:01:00Z", {"type": "plan_downloaded", "corridor_id": "c1", "run_id": "r1",
                                  "detail": {"format": "pdf"}}, {"X-Forwarded-User": "ana"})
    post("2026-10-10T16:02:00Z", {"type": "plan_downloaded", "corridor_id": "c1", "run_id": "r1",
                                  "detail": {"format": "nema"}})
    post("2026-10-10T16:03:00Z", {"type": "plan_downloaded", "corridor_id": "c1", "run_id": "r1",
                                  "detail": {"format": "pdf"}})
    # Bo changes his mind.
    post("2026-10-11T15:00:00Z", _feedback("accepted", run="r1", actor="Bo"))
    post("2026-10-11T15:10:00Z", _feedback("needs_changes", run="r1"), {"X-Forwarded-User": "ana"})
    post("2026-10-11T15:20:00Z", _feedback("accepted", run="r1", intersection="i1"),
         {"X-Forwarded-User": "ana"})   # another plan scope: intersection i1
    post("2026-10-11T15:30:00Z", _feedback("rejected", run="r2"))   # anonymous...
    post("2026-10-11T15:31:00Z", _feedback("accepted", run="r2"))   # ...corrects it
    post("2026-10-11T16:00:00Z", {"type": "console_opened", "corridor_id": "c2"})
    clock.set("2026-10-11T17:00:00Z")   # 10:00 PDT
    return client


def test_summary_counts_days_users_and_feedback(tmp_path, clock) -> None:
    client = _summary_log(tmp_path, clock)
    summary = client.get(_SUMMARY, params={"corridor_id": "c1", "days": 7,
                                           "tz": "America/Los_Angeles"}).json()
    assert summary["corridor_id"] == "c1" and summary["days"] == 7
    assert summary["timezone"] == "America/Los_Angeles"
    assert summary["since"] == "2026-10-05T07:00:00Z"   # local midnight Oct 5, PDT
    assert summary["until"] == "2026-10-11T17:00:00Z"
    assert [d["date"] for d in summary["per_day"]] == [f"2026-10-{d:02d}" for d in range(5, 12)]
    by_day = {d["date"]: d for d in summary["per_day"]}
    assert by_day["2026-10-09"] == {"date": "2026-10-09", "console_opened": 1, "plan_viewed": 0,
                                    "plan_downloaded": 0, "feedback": 1, "total": 2}
    assert by_day["2026-10-10"]["plan_viewed"] == 1 and by_day["2026-10-10"]["plan_downloaded"] == 3
    assert by_day["2026-10-11"]["feedback"] == 5 and by_day["2026-10-11"]["total"] == 5
    assert sum(d["total"] for d in summary["per_day"]) == summary["totals"]["total"] == 11
    assert summary["totals"] == {"console_opened": 1, "plan_viewed": 1, "plan_downloaded": 3,
                                 "feedback": 6, "total": 11}
    assert summary["downloads_by_format"] == {"pdf": 2, "nema": 1}
    # Latest verdict per (run, intersection, actor): Bo accepted (r1), ana needs changes (r1),
    # ana accepted (r1/i1), anonymous accepted (r2).
    assert summary["feedback"] == {"events": 6, "accepted": 3, "rejected": 0, "needs_changes": 1,
                                   "acceptance_rate": 0.75}
    assert summary["unique_actors"] == 2
    assert summary["anonymous_events"] == 4
    assert [(a["actor"], a["actor_source"], a["events"]) for a in summary["actors"]] == [
        ("ana", "proxy", 4), ("anonymous", "anonymous", 4), ("Bo", "self", 3)]
    assert summary["actors"][0]["last_at"] == "2026-10-11T15:20:00.000Z"
    assert summary["last_activity"] == {"at": "2026-10-11T15:31:00.000Z", "type": "feedback",
                                        "actor": "anonymous"}


def test_summary_day_buckets_follow_the_zone(tmp_path, clock) -> None:
    client = _summary_log(tmp_path, clock)
    utc = client.get(_SUMMARY, params={"corridor_id": "c1", "days": 7}).json()
    assert utc["timezone"] == "UTC" and utc["since"] == "2026-10-05T00:00:00Z"
    by_day = {d["date"]: d["total"] for d in utc["per_day"]}
    assert by_day["2026-10-09"] == 0 and by_day["2026-10-10"] == 6   # the 01:00 UTC events move
    unknown = client.get(
        _SUMMARY, params={"corridor_id": "c1", "days": 7, "tz": "Mars/Olympus"}).json()
    assert unknown["timezone"] == "UTC" and unknown["per_day"] == utc["per_day"]


def test_summary_window_and_corridor_scope(tmp_path, clock) -> None:
    client = _summary_log(tmp_path, clock)
    today = client.get(
        _SUMMARY, params={"corridor_id": "c1", "days": 1, "tz": "America/Los_Angeles"}).json()
    assert len(today["per_day"]) == 1 and today["totals"]["total"] == 5
    everything = client.get(_SUMMARY, params={"days": 366}).json()
    assert everything["corridor_id"] is None and len(everything["per_day"]) == 366
    assert everything["totals"]["console_opened"] == 3   # c1, c2 and the June c3 event
    assert everything["last_activity"]["at"] == "2026-10-11T16:00:00.000Z"
    old = client.get(_SUMMARY, params={"corridor_id": "c3"}).json()
    assert old["totals"]["total"] == 0
    assert old["last_activity"] == {"at": "2026-06-01T12:00:00.000Z", "type": "console_opened",
                                    "actor": "anonymous"}
    assert client.get(_SUMMARY, params={"corridor_id": "none"}).json()["last_activity"] is None
    default = client.get(_SUMMARY).json()
    assert default["days"] == 30 and len(default["per_day"]) == 30
    for days in (0, 367):
        assert client.get(_SUMMARY, params={"days": days}).status_code == 422


# ---------------------------------------------------------------------- disabled / mounted


@pytest.mark.parametrize("value", ["0", "false", "No", "off"])
def test_disabled_answers_404_with_the_reason(tmp_path, clock, monkeypatch, value) -> None:
    monkeypatch.setenv("TERASIM_CITY_AUDIT_ENABLED", value)
    client = _client(tmp_path / "out", clock=clock)
    responses = [
        client.post(_EVENTS, json=_feedback()),
        client.get(_EVENTS), client.get(_CSV), client.get(_SUMMARY),
    ]
    assert [r.status_code for r in responses] == [404] * 4
    assert all(r.json()["detail"] == DISABLED_DETAIL for r in responses)
    assert "TERASIM_CITY_AUDIT_ENABLED=0" in DISABLED_DETAIL
    assert not (tmp_path / "out" / "audit").exists()


@pytest.mark.parametrize("value", [None, "1", "true", "yes", ""])
def test_enabled_by_default(monkeypatch, value) -> None:
    if value is None:
        monkeypatch.delenv("TERASIM_CITY_AUDIT_ENABLED", raising=False)
    else:
        monkeypatch.setenv("TERASIM_CITY_AUDIT_ENABLED", value)
    assert audit_api.audit_enabled() is True


def test_the_v1_city_router_serves_the_audit_log(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TERASIM_CITY_AUDIT_ENABLED", raising=False)
    store = InMemoryCityJobStore(max_concurrency=2, lease_ttl=100)
    app = FastAPI()
    app.include_router(create_v1_city_router(store, lambda *_a: None, output_root=tmp_path / "out"))
    client = TestClient(app)
    event = client.post(_EVENTS, json=_feedback()).json()
    assert client.get(_EVENTS).json() == [event]
    assert client.get(_SUMMARY).json()["totals"]["feedback"] == 1
    assert (tmp_path / "out" / "audit" / f"events-{event['at'][:7]}.jsonl").is_file()
    paths = app.openapi()["paths"]
    assert {
        "/v1/city/audit/events", "/v1/city/audit/events.csv", "/v1/city/audit/summary"
    } <= set(paths)
    # The run listing is unaffected by the audit directory under the output root.
    assert client.get("/v1/city/optimizations").json() == []


# ---------------------------------------------------------------------- helpers


def test_event_types_and_time_formats() -> None:
    assert get_args(EventType) == EVENT_TYPES
    assert format_at(_ts("2026-10-11T17:03:12.345678Z")) == "2026-10-11T17:03:12.345Z"
    assert parse_bound("2026-10-11") == "2026-10-11T00:00:00.000Z"
    assert parse_bound("2026-10-11T10:00:00-07:00") == "2026-10-11T17:00:00.000Z"
    assert parse_bound("2026-10-11T10:00:00") == "2026-10-11T10:00:00.000Z"
    assert parse_bound("2026-10-11T10:00:00.5z") == "2026-10-11T10:00:00.500Z"
    assert parse_bound("2026-10-11T10:00:00.1234567+00:00") == "2026-10-11T10:00:00.123Z"
    for bad in ("", "soon", "2026-10-32", "2026-10-11T25:00", "0000-01-01"):
        with pytest.raises(ValueError):
            parse_bound(bad)


def test_stray_files_in_the_audit_directory_are_ignored(tmp_path, clock) -> None:
    audit = tmp_path / "out" / "audit"
    audit.mkdir(parents=True)
    line = '{"id": "x", "at": "2026-10-01T00:00:00.000Z", "type": "feedback"}\n'
    for name in ("events-0000-13.jsonl", "events-9999-12.jsonl", "notes.txt",
                 "events-2026-10.jsonl.bak"):
        (audit / name).write_text(line)
    client = _client(tmp_path / "out", clock=clock, enabled=True)
    event = client.post(_EVENTS, json=_feedback()).json()
    assert client.get(_EVENTS).json() == [event]


def test_reading_a_broken_audit_directory_returns_nothing(tmp_path, clock) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "audit").write_text("not a directory")
    client = _client(out, clock=clock, enabled=True)
    assert client.get(_EVENTS).json() == []
    assert client.get(_SUMMARY).json()["totals"]["total"] == 0
