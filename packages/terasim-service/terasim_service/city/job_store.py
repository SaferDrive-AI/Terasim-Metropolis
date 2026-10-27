"""Job-state store for city signal-evaluation runs.

Tracks each run's status / progress / result location, and enforces a
concurrency cap with a **self-healing lease**: a worker holds a short-lived
lease (TTL) that it renews from a heartbeat while running, and releases when
done. If the worker crashes the lease simply expires, so the slot frees on its
own — no permanent leak, and a still-running task never loses its slot (it keeps
renewing).

A job may hold **multiple slots** (``try_acquire_slots(run_id, n)``): one per
worker process it will spawn. Incident-triggered (expedited) runs may acquire
with ``emergency=True``: the effective limit is then
``max_concurrency + emergency_slots``, a reserved headroom that normal
acquisitions can never use. Emergency-held slots renew/release exactly like
normal ones. The lease is represented as ``n`` members
``{run_id}#0 .. {run_id}#{n-1}`` in the lease set plus a companion slot-count
entry keyed by ``run_id`` (Redis: ``{prefix}:slots:{run_id}`` with a
lease-aligned TTL). Renewal is all-or-nothing across the members; release
removes them all. Legacy members without the ``#i`` suffix
(written by pre-multi-slot workers) still renew/release correctly.

Every record also says who started the run (``origin``: ``"scheduled"``,
``"api"`` or ``"direct"``), its corridor and intersections, the traffic model
its candidates are simulated with (``"sumo"`` or ``"cnde"``), and when it was
submitted and finished; ``list_runs`` returns the records newest first (Redis:
from a sorted-set index per deployment and per corridor, pruned to the record
retention).

Two implementations with identical semantics:
  * ``InMemoryCityJobStore`` — single process; used by unit tests. The clock is
    injectable so lease expiry is deterministic in tests.
  * ``RedisCityJobStore`` — shared across processes; used in production. Slot
    acquisition is atomic via a Lua script.
"""

from __future__ import annotations

import copy
import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Sequence


SUBMITTED = "submitted"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

#: Who started a run: the cadence scheduler, ``POST /v1/city/optimizations``, or the
#: synchronous ``POST /v1/city/optimize``.
ORIGIN_SCHEDULED = "scheduled"
ORIGIN_API = "api"
ORIGIN_DIRECT = "direct"
ORIGINS = (ORIGIN_SCHEDULED, ORIGIN_API, ORIGIN_DIRECT)


def _check_origin(origin: Optional[str]) -> None:
    if origin is not None and origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS} or None, got {origin!r}")


def _check_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")


@dataclass
class JobRecord:
    """Durable state for one evaluation run (survives the worker process)."""

    run_id: str
    status: str
    done: int = 0
    total: int = 0
    last_program_id: Optional[str] = None
    result_path: Optional[str] = None
    best: Optional[dict] = None
    best_score: Optional[float] = None
    error: Optional[str] = None
    updated_at: Optional[float] = None
    #: ``"scheduled"`` / ``"api"`` / ``"direct"``; None for a record created without one.
    origin: Optional[str] = None
    corridor_id: Optional[str] = None
    #: The intersections the request named (submission time).
    intersection_ids: Optional[list] = None
    #: Wall-clock epoch seconds of ``create``, and of ``set_completed``/``set_failed``.
    submitted_at: Optional[float] = None
    completed_at: Optional[float] = None
    #: The traffic model the run's candidates are simulated with (``"sumo"`` /
    #: ``"cnde"``); None for a record created without one.
    traffic_model: Optional[str] = None


class CityJobStore(Protocol):
    """Interface the endpoints and worker depend on (prod: Redis, test: memory)."""

    def create(
        self,
        run_id: str,
        *,
        origin: Optional[str] = None,
        corridor_id: Optional[str] = None,
        intersection_ids: Optional[Sequence[str]] = None,
        traffic_model: Optional[str] = None,
    ) -> None: ...
    def set_running(self, run_id: str) -> None: ...
    def set_progress(
        self, run_id: str, done: int, total: int, program_id: Optional[str]
    ) -> None: ...
    def set_completed(self, run_id: str, result_path: str, best: dict) -> None: ...
    def set_failed(self, run_id: str, error: str) -> None: ...
    def set_best_score(self, run_id: str, score: float) -> None: ...
    def get(self, run_id: str) -> Optional[JobRecord]: ...
    def list_runs(
        self, *, corridor_id: Optional[str] = None, limit: int = 20
    ) -> list[JobRecord]: ...
    # concurrency lease
    @property
    def max_concurrency(self) -> int: ...
    @property
    def emergency_slots(self) -> int: ...
    def try_acquire_slots(
        self, run_id: str, n: int, *, emergency: bool = False
    ) -> bool: ...
    def renew_slot(self, run_id: str) -> bool: ...
    def release_slot(self, run_id: str) -> None: ...
    def holds_slots(self, run_id: str) -> bool: ...


class InMemoryCityJobStore:
    """Single-process store. Lease expiry uses an injectable clock for testing."""

    def __init__(
        self,
        max_concurrency: int = 3,
        lease_ttl: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        emergency_slots: int = 2,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError(f"max_concurrency must be positive, got {max_concurrency}")
        if lease_ttl <= 0:
            raise ValueError(f"lease_ttl must be positive, got {lease_ttl}")
        if not isinstance(emergency_slots, int) or emergency_slots < 0:
            raise ValueError(
                f"emergency_slots must be an int >= 0, got {emergency_slots!r}"
            )
        self._max = max_concurrency
        self._emergency = emergency_slots
        self._ttl = lease_ttl
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._jobs: dict[str, JobRecord] = {}
        self._leases: dict[str, float] = {}  # member ("run_id#i") -> expiry time
        self._slots: dict[str, int] = {}  # run_id -> number of members held

    @property
    def max_concurrency(self) -> int:
        """Slots normal acquisitions may hold at once (all jobs together)."""
        return self._max

    @property
    def emergency_slots(self) -> int:
        """Extra headroom above ``max_concurrency`` only emergency acquisitions may use."""
        return self._emergency

    # ---- job record ----
    def create(
        self,
        run_id: str,
        *,
        origin: Optional[str] = None,
        corridor_id: Optional[str] = None,
        intersection_ids: Optional[Sequence[str]] = None,
        traffic_model: Optional[str] = None,
    ) -> None:
        _check_origin(origin)
        with self._lock:
            if run_id in self._jobs:
                raise ValueError(f"run_id already exists: {run_id}")
            now = self._wall_clock()
            self._jobs[run_id] = JobRecord(
                run_id=run_id,
                status=SUBMITTED,
                updated_at=now,
                origin=origin,
                corridor_id=corridor_id,
                intersection_ids=(
                    None if intersection_ids is None else [str(i) for i in intersection_ids]
                ),
                submitted_at=now,
                traffic_model=traffic_model,
            )

    def set_running(self, run_id: str) -> None:
        with self._lock:
            rec = self._jobs[run_id]
            rec.status = RUNNING
            rec.updated_at = self._wall_clock()

    def set_progress(
        self, run_id: str, done: int, total: int, program_id: Optional[str]
    ) -> None:
        with self._lock:
            rec = self._jobs[run_id]
            rec.done, rec.total, rec.last_program_id = done, total, program_id
            rec.updated_at = self._wall_clock()

    def set_completed(self, run_id: str, result_path: str, best: dict) -> None:
        with self._lock:
            rec = self._jobs[run_id]
            rec.status, rec.result_path = COMPLETED, result_path
            rec.best = copy.deepcopy(best)
            rec.updated_at = rec.completed_at = self._wall_clock()

    def set_failed(self, run_id: str, error: str) -> None:
        with self._lock:
            rec = self._jobs[run_id]
            rec.status, rec.error = FAILED, error
            rec.updated_at = rec.completed_at = self._wall_clock()

    def set_best_score(self, run_id: str, score: float) -> None:
        with self._lock:
            rec = self._jobs[run_id]
            rec.best_score = float(score)
            rec.updated_at = self._wall_clock()

    def get(self, run_id: str) -> Optional[JobRecord]:
        with self._lock:
            rec = self._jobs.get(run_id)
            return copy.deepcopy(rec) if rec is not None else None

    def list_runs(
        self, *, corridor_id: Optional[str] = None, limit: int = 20
    ) -> list[JobRecord]:
        """Records newest first (by submission time), optionally of one corridor only."""
        _check_limit(limit)
        with self._lock:
            records = [
                copy.deepcopy(rec)
                for rec in self._jobs.values()
                if corridor_id is None or rec.corridor_id == corridor_id
            ]
        records.sort(key=lambda rec: (rec.submitted_at or 0.0, rec.run_id), reverse=True)
        return records[:limit]

    # ---- concurrency lease ----
    @staticmethod
    def _member(run_id: str, index: int) -> str:
        return f"{run_id}#{index}"

    def _prune(self, now: float) -> None:
        # caller holds the lock
        expired = [member for member, exp in self._leases.items() if exp <= now]
        for member in expired:
            del self._leases[member]
        if expired:
            # Members of one run expire together (they always share one expiry),
            # so a run whose first member is gone holds nothing anymore.
            stale = [
                rid
                for rid in self._slots
                if self._member(rid, 0) not in self._leases
            ]
            for rid in stale:
                del self._slots[rid]

    def try_acquire_slots(self, run_id: str, n: int, *, emergency: bool = False) -> bool:
        if n < 1:
            raise ValueError(f"slot count must be >= 1, got {n}")
        limit = self._max + (self._emergency if emergency else 0)
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._leases) + n > limit:
                return False
            expiry = now + self._ttl
            for index in range(n):
                self._leases[self._member(run_id, index)] = expiry
            self._slots[run_id] = n
            return True

    def renew_slot(self, run_id: str) -> bool:
        with self._lock:
            now = self._clock()
            count = self._slots.get(run_id)
            if count is None:
                # Legacy pre-multi-slot representation: a plain run_id member.
                exp = self._leases.get(run_id)
                if exp is None or exp <= now:
                    # Slot was released or the lease expired: do NOT revive it,
                    # otherwise a recovered worker could exceed the concurrency cap.
                    self._leases.pop(run_id, None)
                    return False
                self._leases[run_id] = now + self._ttl
                return True
            members = [self._member(run_id, index) for index in range(count)]
            # All-or-nothing: one missing/expired member means the lease is gone.
            if any(
                self._leases.get(member) is None or self._leases[member] <= now
                for member in members
            ):
                for member in members:
                    self._leases.pop(member, None)
                self._slots.pop(run_id, None)
                return False
            expiry = now + self._ttl
            for member in members:
                self._leases[member] = expiry
            return True

    def release_slot(self, run_id: str) -> None:
        with self._lock:
            count = self._slots.pop(run_id, 0)
            for index in range(count):
                self._leases.pop(self._member(run_id, index), None)
            self._leases.pop(run_id, None)  # legacy plain member

    def holds_slots(self, run_id: str) -> bool:
        """Whether ``run_id`` holds an unexpired lease, i.e. its worker is alive.

        Read-only apart from pruning expired leases. A live run renews its lease
        from a heartbeat; a worker killed before it could record the outcome lets
        it lapse.
        """
        with self._lock:
            self._prune(self._clock())
            return self._member(run_id, 0) in self._leases or run_id in self._leases


class RedisCityJobStore:
    """Redis-backed store shared across processes (production).

    Job state lives in a hash per run (``{prefix}:{run_id}``). Concurrency leases
    live in a sorted set (``{prefix}:leases``) scored by expiry; acquisition is
    atomic via a Lua script that prunes expired leases, checks the count, and
    adds the new lease in one step. The run listing reads two sorted sets scored
    by submission time, ``{prefix}:runs`` (every run) and
    ``{prefix}:runs:corridor:{corridor_id}``; entries older than the record
    retention are pruned on every ``create``, and entries whose record has
    expired are dropped when a listing meets them.
    """

    # Atomic multi-slot acquire: prune expired members, check headroom for all n
    # members at once, then add them plus the slot-count key (lease-aligned TTL).
    _ACQUIRE_LUA = """
    local key = KEYS[1]
    local slots_key = KEYS[2]
    local now = tonumber(ARGV[1])
    local ttl = tonumber(ARGV[2])
    local maxc = tonumber(ARGV[3])
    local run_id = ARGV[4]
    local n = tonumber(ARGV[5])
    redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
    if redis.call('ZCARD', key) + n > maxc then
        return 0
    end
    for i = 0, n - 1 do
        redis.call('ZADD', key, now + ttl, run_id .. '#' .. i)
    end
    redis.call('SET', slots_key, n, 'PX', math.floor(ttl * 1000))
    return 1
    """

    # Renew only a lease whose members ALL still exist and have not expired;
    # otherwise drop every member and refuse (return 0), so a recovered worker
    # cannot revive slots another job has taken. Without a slot-count key the
    # lease is a legacy single plain-run_id member (pre-multi-slot worker).
    _RENEW_LUA = """
    local key = KEYS[1]
    local slots_key = KEYS[2]
    local now = tonumber(ARGV[1])
    local ttl = tonumber(ARGV[2])
    local run_id = ARGV[3]
    local n = tonumber(redis.call('GET', slots_key) or '0')
    if n == 0 then
        local score = redis.call('ZSCORE', key, run_id)
        if (not score) or (tonumber(score) <= now) then
            redis.call('ZREM', key, run_id)
            return 0
        end
        redis.call('ZADD', key, now + ttl, run_id)
        return 1
    end
    for i = 0, n - 1 do
        local score = redis.call('ZSCORE', key, run_id .. '#' .. i)
        if (not score) or (tonumber(score) <= now) then
            for j = 0, n - 1 do
                redis.call('ZREM', key, run_id .. '#' .. j)
            end
            redis.call('DEL', slots_key)
            return 0
        end
    end
    for i = 0, n - 1 do
        redis.call('ZADD', key, now + ttl, run_id .. '#' .. i)
    end
    redis.call('PEXPIRE', slots_key, math.floor(ttl * 1000))
    return 1
    """

    _RELEASE_LUA = """
    local key = KEYS[1]
    local slots_key = KEYS[2]
    local run_id = ARGV[1]
    local n = tonumber(redis.call('GET', slots_key) or '0')
    for i = 0, n - 1 do
        redis.call('ZREM', key, run_id .. '#' .. i)
    end
    redis.call('ZREM', key, run_id)
    redis.call('DEL', slots_key)
    return 1
    """

    def __init__(
        self,
        client=None,
        max_concurrency: int = 3,
        lease_ttl: float = 120.0,
        record_ttl: float = 7 * 24 * 3600,
        key_prefix: str = "city_eval",
        emergency_slots: int = 2,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError(f"max_concurrency must be positive, got {max_concurrency}")
        if lease_ttl <= 0:
            raise ValueError(f"lease_ttl must be positive, got {lease_ttl}")
        if not math.isfinite(record_ttl) or record_ttl < 1:
            raise ValueError(
                f"record_ttl must be a finite number of seconds >= 1, got {record_ttl}"
            )
        if not isinstance(emergency_slots, int) or emergency_slots < 0:
            raise ValueError(
                f"emergency_slots must be an int >= 0, got {emergency_slots!r}"
            )
        import redis

        self._r = client if client is not None else redis.Redis()
        self._max = max_concurrency
        self._emergency = emergency_slots
        self._ttl = lease_ttl
        self._record_ttl = record_ttl
        self._prefix = key_prefix
        self._leases_key = f"{key_prefix}:leases"
        self._acquire = self._r.register_script(self._ACQUIRE_LUA)
        self._renew = self._r.register_script(self._RENEW_LUA)
        self._release = self._r.register_script(self._RELEASE_LUA)

    @property
    def max_concurrency(self) -> int:
        """Slots normal acquisitions may hold at once (all jobs together)."""
        return self._max

    @property
    def emergency_slots(self) -> int:
        """Extra headroom above ``max_concurrency`` only emergency acquisitions may use."""
        return self._emergency

    def _job_key(self, run_id: str) -> str:
        return f"{self._prefix}:{run_id}"

    def _slots_key(self, run_id: str) -> str:
        return f"{self._prefix}:slots:{run_id}"

    def _index_key(self, corridor_id: Optional[str] = None) -> str:
        if corridor_id is None:
            return f"{self._prefix}:runs"
        return f"{self._prefix}:runs:corridor:{corridor_id}"

    @staticmethod
    def _now() -> float:
        # wall clock so all processes agree on lease expiry
        return time.time()

    # ---- job record ----
    def create(
        self,
        run_id: str,
        *,
        origin: Optional[str] = None,
        corridor_id: Optional[str] = None,
        intersection_ids: Optional[Sequence[str]] = None,
        traffic_model: Optional[str] = None,
    ) -> None:
        _check_origin(origin)
        key = self._job_key(run_id)
        # Reject reuse of a run_id: the lease is owned by run_id alone, so reusing
        # it would let a stale worker release/renew a different job's slot. Then
        # set a retention TTL so finished records don't pile up in Redis forever.
        if self._r.exists(key):
            raise ValueError(f"run_id already exists: {run_id}")
        now = self._now()
        mapping = {
            "status": SUBMITTED,
            "done": 0,
            "total": 0,
            "updated_at": now,
            "submitted_at": now,
        }
        if origin is not None:
            mapping["origin"] = origin
        if corridor_id is not None:
            mapping["corridor_id"] = corridor_id
        if intersection_ids is not None:
            mapping["intersection_ids"] = json.dumps([str(i) for i in intersection_ids])
        if traffic_model is not None:
            mapping["traffic_model"] = traffic_model
        oldest = now - self._record_ttl
        indexes = [self._index_key()]
        if corridor_id is not None:
            indexes.append(self._index_key(corridor_id))
        pipe = self._r.pipeline()
        pipe.hset(key, mapping=mapping)
        pipe.expire(key, int(self._record_ttl))
        for index in indexes:
            pipe.zadd(index, {run_id: now})
            # The records behind older entries have expired by now.
            pipe.zremrangebyscore(index, "-inf", f"({oldest}")
        pipe.execute()

    def set_running(self, run_id: str) -> None:
        self._r.hset(
            self._job_key(run_id), mapping={"status": RUNNING, "updated_at": self._now()}
        )

    def set_progress(
        self, run_id: str, done: int, total: int, program_id: Optional[str]
    ) -> None:
        self._r.hset(
            self._job_key(run_id),
            mapping={
                "done": done,
                "total": total,
                "last_program_id": program_id or "",
                "updated_at": self._now(),
            },
        )

    def set_completed(self, run_id: str, result_path: str, best: dict) -> None:
        now = self._now()
        self._r.hset(
            self._job_key(run_id),
            mapping={
                "status": COMPLETED,
                "result_path": result_path,
                "best": json.dumps(best),
                "updated_at": now,
                "completed_at": now,
            },
        )

    def set_failed(self, run_id: str, error: str) -> None:
        now = self._now()
        self._r.hset(
            self._job_key(run_id),
            mapping={
                "status": FAILED,
                "error": error,
                "updated_at": now,
                "completed_at": now,
            },
        )

    def set_best_score(self, run_id: str, score: float) -> None:
        self._r.hset(
            self._job_key(run_id),
            mapping={"best_score": float(score), "updated_at": self._now()},
        )

    def get(self, run_id: str) -> Optional[JobRecord]:
        raw = self._r.hgetall(self._job_key(run_id))
        if not raw:
            return None
        return self._record(run_id, raw)

    @staticmethod
    def _record(run_id: str, raw: dict) -> JobRecord:
        h = {_dec(k): _dec(v) for k, v in raw.items()}
        best = h.get("best")
        best_score = h.get("best_score")
        intersection_ids = h.get("intersection_ids")
        return JobRecord(
            run_id=run_id,
            status=h.get("status", SUBMITTED),
            done=int(h.get("done", 0) or 0),
            total=int(h.get("total", 0) or 0),
            last_program_id=h.get("last_program_id") or None,
            result_path=h.get("result_path") or None,
            best=json.loads(best) if best else None,
            best_score=float(best_score) if best_score not in (None, "") else None,
            error=h.get("error") or None,
            updated_at=float(h["updated_at"]) if h.get("updated_at") else None,
            origin=h.get("origin") or None,
            corridor_id=h.get("corridor_id") or None,
            intersection_ids=json.loads(intersection_ids) if intersection_ids else None,
            submitted_at=float(h["submitted_at"]) if h.get("submitted_at") else None,
            completed_at=float(h["completed_at"]) if h.get("completed_at") else None,
            traffic_model=h.get("traffic_model") or None,
        )

    def list_runs(
        self, *, corridor_id: Optional[str] = None, limit: int = 20
    ) -> list[JobRecord]:
        """Records newest first (by submission time), optionally of one corridor only.

        Reads the index page by page; an entry whose record has expired is skipped
        and removed from the index afterwards.
        """
        _check_limit(limit)
        index = self._index_key(corridor_id)
        page = max(limit, 50)
        records: list[JobRecord] = []
        expired: list[str] = []
        start = 0
        while len(records) < limit:
            run_ids = [_dec(r) for r in self._r.zrevrange(index, start, start + page - 1)]
            if not run_ids:
                break
            start += len(run_ids)
            pipe = self._r.pipeline()
            for run_id in run_ids:
                pipe.hgetall(self._job_key(run_id))
            for run_id, raw in zip(run_ids, pipe.execute()):
                if not raw:
                    expired.append(run_id)
                    continue
                records.append(self._record(run_id, raw))
                if len(records) >= limit:
                    break
        if expired:
            self._r.zrem(index, *expired)
            if corridor_id is not None:
                self._r.zrem(self._index_key(), *expired)
        return records

    # ---- concurrency lease ----
    def try_acquire_slots(self, run_id: str, n: int, *, emergency: bool = False) -> bool:
        if n < 1:
            raise ValueError(f"slot count must be >= 1, got {n}")
        # The Lua script already takes the limit as ARGV[3]; emergency runs
        # simply get a higher limit (the reserved headroom). Renew/release are
        # limit-free, so they need no change.
        limit = self._max + (self._emergency if emergency else 0)
        result = self._acquire(
            keys=[self._leases_key, self._slots_key(run_id)],
            args=[self._now(), self._ttl, limit, run_id, n],
        )
        return bool(result)

    def renew_slot(self, run_id: str) -> bool:
        result = self._renew(
            keys=[self._leases_key, self._slots_key(run_id)],
            args=[self._now(), self._ttl, run_id],
        )
        return bool(result)

    def release_slot(self, run_id: str) -> None:
        # If the slot-count key already expired, any surviving members lapse on
        # their own via the lease TTL (the acquire script prunes them).
        self._release(
            keys=[self._leases_key, self._slots_key(run_id)],
            args=[run_id],
        )

    def holds_slots(self, run_id: str) -> bool:
        """Whether ``run_id`` holds an unexpired lease, i.e. its worker is alive (read-only)."""
        now = self._now()
        for member in (f"{run_id}#0", run_id):  # multi-slot member, legacy plain member
            score = self._r.zscore(self._leases_key, member)
            if score is not None and float(score) > now:
                return True
        return False


def _dec(value) -> str:
    """Decode a Redis bytes value to str (no-op if already str)."""
    return value.decode("utf-8") if isinstance(value, bytes) else value
