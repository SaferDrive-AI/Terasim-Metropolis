"""Unit tests for the in-memory city job store: status flow + self-healing lease.

Pure in-process logic — no Redis, no SUMO. Lease expiry is driven by an injected
clock so it is deterministic. The Redis implementation's cross-process behaviour
is covered by a separate (optional) requires_redis integration test.
"""

from __future__ import annotations

import pytest

from terasim_service.city.job_store import (
    COMPLETED,
    FAILED,
    RUNNING,
    SUBMITTED,
    InMemoryCityJobStore,
    RedisCityJobStore,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_status_flow_submitted_to_completed() -> None:
    store = InMemoryCityJobStore()
    store.create("r1")
    assert store.get("r1").status == SUBMITTED

    store.set_running("r1")
    assert store.get("r1").status == RUNNING

    store.set_progress("r1", 3, 7, "plan_3")
    rec = store.get("r1")
    assert (rec.done, rec.total, rec.last_program_id) == (3, 7, "plan_3")

    store.set_completed("r1", "/out/r1/signal_plan_comparison.json", {"program_id": "plan_3"})
    rec = store.get("r1")
    assert rec.status == COMPLETED
    assert rec.result_path == "/out/r1/signal_plan_comparison.json"
    assert rec.best == {"program_id": "plan_3"}


def test_set_failed_records_error() -> None:
    store = InMemoryCityJobStore()
    store.create("r1")
    store.set_failed("r1", "boom")
    rec = store.get("r1")
    assert rec.status == FAILED
    assert rec.error == "boom"


def test_get_missing_returns_none() -> None:
    assert InMemoryCityJobStore().get("nope") is None


def test_renew_on_expired_lease_does_not_revive_it() -> None:
    # P1 regression: an expired lease must NOT be revived by renew, or a
    # recovered worker would push concurrency past the cap.
    clock = _Clock()
    store = InMemoryCityJobStore(max_concurrency=1, lease_ttl=100, clock=clock)
    assert store.try_acquire_slots("a", 1) is True
    clock.advance(101)  # a's lease expired; a never renewed
    assert store.try_acquire_slots("b", 1) is True  # b takes the freed slot
    assert store.renew_slot("a") is False  # a must NOT come back
    assert store.try_acquire_slots("c", 1) is False  # still only b holds a slot (cap 1)


def test_invalid_config_fails_loud() -> None:
    with pytest.raises(ValueError):
        InMemoryCityJobStore(max_concurrency=0)
    with pytest.raises(ValueError):
        InMemoryCityJobStore(lease_ttl=0)


def test_create_rejects_duplicate_run_id() -> None:
    # run_id is the lease's only owner, so reuse must be refused (not silently
    # reset) — otherwise a stale worker could release a new job's slot.
    store = InMemoryCityJobStore()
    store.create("r1")
    with pytest.raises(ValueError, match="already exists"):
        store.create("r1")


def test_redis_store_rejects_bad_record_ttl() -> None:
    # Construction-only check (validation runs before any Redis connection):
    # record_ttl < 1 would make EXPIRE drop the record immediately.
    for bad in (0, 0.5, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            RedisCityJobStore(record_ttl=bad)


def test_updated_at_is_set_and_refreshed_on_each_transition() -> None:
    # updated_at uses an injectable wall clock so we can assert it advances with
    # each state change (the public status surfaces it to callers).
    wall = _Clock()
    store = InMemoryCityJobStore(wall_clock=wall)

    wall.t = 100.0
    store.create("r1")
    assert store.get("r1").updated_at == 100.0

    wall.t = 101.0
    store.set_running("r1")
    assert store.get("r1").updated_at == 101.0

    wall.t = 102.0
    store.set_progress("r1", 1, 2, "p1")
    assert store.get("r1").updated_at == 102.0

    wall.t = 103.0
    store.set_completed("r1", "out/comparison.json", {"program_id": "p1"})
    assert store.get("r1").updated_at == 103.0


# --- best_score (additive record field) -----------------------------------------


def test_best_score_defaults_none_and_is_settable() -> None:
    wall = _Clock()
    store = InMemoryCityJobStore(wall_clock=wall)
    store.create("r1")
    assert store.get("r1").best_score is None

    wall.t = 7.0
    store.set_best_score("r1", 0.87)
    rec = store.get("r1")
    assert rec.best_score == 0.87
    assert rec.updated_at == 7.0  # counts as an update

    store.set_best_score("r1", 0.5)
    assert store.get("r1").best_score == 0.5


# --- multi-slot leases -----------------------------------------------------------


def test_try_acquire_slots_counts_against_the_shared_cap() -> None:
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    assert store.try_acquire_slots("a", 2) is True
    assert store.try_acquire_slots("b", 2) is False  # 2 + 2 > 3
    assert store.try_acquire_slots("c", 1) is True  # single slot still fits
    assert store.try_acquire_slots("d", 1) is False  # cap reached


def test_try_acquire_slots_more_than_cap_returns_false() -> None:
    store = InMemoryCityJobStore(max_concurrency=2, lease_ttl=100)
    assert store.try_acquire_slots("a", 3) is False
    assert store.try_acquire_slots("b", 1) is True  # nothing was partially reserved


def test_try_acquire_slots_rejects_non_positive_n() -> None:
    store = InMemoryCityJobStore()
    with pytest.raises(ValueError):
        store.try_acquire_slots("a", 0)
    with pytest.raises(ValueError):
        store.try_acquire_slots("a", -1)


def test_release_frees_all_slots_of_a_run() -> None:
    store = InMemoryCityJobStore(max_concurrency=2, lease_ttl=100)
    assert store.try_acquire_slots("a", 2) is True
    assert store.try_acquire_slots("b", 1) is False
    store.release_slot("a")
    assert store.try_acquire_slots("b", 2) is True  # both slots came back


def test_renew_extends_all_slots_of_a_run() -> None:
    clock = _Clock()
    store = InMemoryCityJobStore(max_concurrency=2, lease_ttl=100, clock=clock)
    assert store.try_acquire_slots("a", 2) is True
    clock.advance(80)
    assert store.renew_slot("a") is True  # expiry now 180 for BOTH members
    clock.advance(80)  # t=160; without the renew the lease would be gone
    assert store.try_acquire_slots("b", 1) is False  # both slots still held


def test_expired_multi_slot_lease_frees_all_slots() -> None:
    clock = _Clock()
    store = InMemoryCityJobStore(max_concurrency=2, lease_ttl=100, clock=clock)
    assert store.try_acquire_slots("a", 2) is True
    clock.advance(101)
    assert store.try_acquire_slots("b", 2) is True  # both freed together
    assert store.renew_slot("a") is False  # and must NOT come back


# --- emergency slots (reserved headroom for incident-expedited runs) --------------


def _redis_available() -> bool:
    try:
        import redis

        from terasim_service.utils.base import redis_connection_kwargs_from_env

        client = redis.Redis(
            **redis_connection_kwargs_from_env(), socket_connect_timeout=0.5
        )
        client.ping()
        return True
    except Exception:
        return False


requires_redis = pytest.mark.skipif(
    not _redis_available(), reason="Redis not available"
)


def _redis_store(max_concurrency: int, emergency_slots: int) -> RedisCityJobStore:
    import uuid

    import redis

    from terasim_service.utils.base import redis_connection_kwargs_from_env

    return RedisCityJobStore(
        client=redis.Redis(**redis_connection_kwargs_from_env()),
        max_concurrency=max_concurrency,
        lease_ttl=100,
        key_prefix=f"test_city_emerg_{uuid.uuid4().hex}",
        emergency_slots=emergency_slots,
    )


def _assert_emergency_semantics(store) -> None:
    """Shared contract: cap 2 + emergency headroom 2 (any store implementation)."""
    # Normal acquisitions fill the cap and can never use the headroom.
    assert store.try_acquire_slots("a", 2) is True
    assert store.try_acquire_slots("b", 1) is False
    assert store.try_acquire_slots("b", 1, emergency=False) is False
    # Emergency acquisitions ride the reserved headroom (limit 2 + 2).
    assert store.try_acquire_slots("e1", 2, emergency=True) is True
    # Headroom exhausted: even an emergency single slot is refused.
    assert store.try_acquire_slots("e2", 1, emergency=True) is False
    # Emergency-held slots release like normal ones and free the headroom.
    store.release_slot("e1")
    assert store.try_acquire_slots("e2", 2, emergency=True) is True
    # Emergency-held slots still occupy the shared pool: normal callers see a
    # full cap even after "a" frees its 2 (e2's 2 fill the normal limit).
    assert store.try_acquire_slots("c", 1) is False
    store.release_slot("a")
    assert store.try_acquire_slots("c", 1) is False
    # Only when the emergency run finishes does normal capacity come back.
    store.release_slot("e2")
    assert store.try_acquire_slots("c", 2) is True


def test_inmemory_emergency_slots_semantics() -> None:
    _assert_emergency_semantics(
        InMemoryCityJobStore(max_concurrency=2, lease_ttl=100, emergency_slots=2)
    )


@requires_redis
def test_redis_emergency_slots_semantics() -> None:
    store = _redis_store(max_concurrency=2, emergency_slots=2)
    try:
        _assert_emergency_semantics(store)
    finally:
        for run_id in ("a", "b", "c", "e1", "e2"):
            store.release_slot(run_id)


def test_inmemory_emergency_acquire_renews_and_releases_normally() -> None:
    clock = _Clock()
    store = InMemoryCityJobStore(
        max_concurrency=1, lease_ttl=100, clock=clock, emergency_slots=1
    )
    assert store.try_acquire_slots("a", 1) is True
    assert store.try_acquire_slots("e", 1, emergency=True) is True
    clock.advance(80)
    assert store.renew_slot("a") is True
    assert store.renew_slot("e") is True  # heartbeats work on emergency leases
    clock.advance(80)  # t=160: without the renews both leases would have lapsed
    assert store.try_acquire_slots("f", 1, emergency=True) is False
    store.release_slot("e")
    assert store.try_acquire_slots("f", 1, emergency=True) is True


@pytest.mark.parametrize("bad", [-1, -5, 1.5, "2"])
def test_invalid_emergency_slots_fails_loud(bad) -> None:
    with pytest.raises(ValueError):
        InMemoryCityJobStore(emergency_slots=bad)
    with pytest.raises(ValueError):
        RedisCityJobStore(emergency_slots=bad)


def test_holds_slots_follows_the_lease() -> None:
    clock = _Clock()
    store = InMemoryCityJobStore(max_concurrency=4, lease_ttl=100, clock=clock)
    assert store.holds_slots("a") is False
    assert store.try_acquire_slots("a", 2) is True
    assert store.holds_slots("a") is True
    clock.advance(60)
    assert store.renew_slot("a") is True
    clock.advance(60)
    assert store.holds_slots("a") is True  # renewed at 60, expires at 160
    clock.advance(41)
    assert store.holds_slots("a") is False  # lapsed: the worker is gone
    assert store.try_acquire_slots("b", 1) is True
    store.release_slot("b")
    assert store.holds_slots("b") is False


# --- run metadata + listing (the run listing endpoint reads these) -----------------


def test_create_records_origin_corridor_intersections_and_submission_time() -> None:
    wall = _Clock()
    store = InMemoryCityJobStore(wall_clock=wall)
    wall.t = 50.0
    store.create(
        "r1", origin="scheduled", corridor_id="c1", intersection_ids=["i1", "i2"],
        traffic_model="cnde",
    )

    rec = store.get("r1")
    assert rec.origin == "scheduled"
    assert rec.corridor_id == "c1"
    assert rec.intersection_ids == ["i1", "i2"]
    assert rec.traffic_model == "cnde"
    assert rec.submitted_at == 50.0
    assert rec.completed_at is None


def test_create_without_metadata_keeps_the_old_record_shape() -> None:
    store = InMemoryCityJobStore()
    store.create("r1")
    rec = store.get("r1")
    assert (rec.origin, rec.corridor_id, rec.intersection_ids) == (None, None, None)
    assert rec.traffic_model is None
    assert rec.submitted_at is not None


def test_create_rejects_an_unknown_origin() -> None:
    store = InMemoryCityJobStore()
    with pytest.raises(ValueError, match="origin"):
        store.create("r1", origin="chat")
    assert store.get("r1") is None


@pytest.mark.parametrize("finish", ["completed", "failed"])
def test_completed_at_is_set_when_the_run_finishes(finish) -> None:
    wall = _Clock()
    store = InMemoryCityJobStore(wall_clock=wall)
    store.create("r1", origin="api")
    wall.t = 9.0
    store.set_running("r1")
    assert store.get("r1").completed_at is None
    wall.t = 12.5
    if finish == "completed":
        store.set_completed("r1", "out/result.json", {"plan_id": "p"})
    else:
        store.set_failed("r1", "boom")
    assert store.get("r1").completed_at == 12.5


def test_list_runs_newest_first_with_limit_and_corridor_filter() -> None:
    wall = _Clock()
    store = InMemoryCityJobStore(wall_clock=wall)
    for index, corridor in enumerate(["a", "b", "a", "a", "b"]):
        wall.t = 100.0 + index
        store.create(f"r{index}", origin="api", corridor_id=corridor)

    assert [rec.run_id for rec in store.list_runs()] == ["r4", "r3", "r2", "r1", "r0"]
    assert [rec.run_id for rec in store.list_runs(limit=2)] == ["r4", "r3"]
    assert [rec.run_id for rec in store.list_runs(corridor_id="a")] == ["r3", "r2", "r0"]
    assert [rec.run_id for rec in store.list_runs(corridor_id="a", limit=1)] == ["r3"]
    assert store.list_runs(corridor_id="unknown") == []


def test_list_runs_returns_copies() -> None:
    store = InMemoryCityJobStore()
    store.create("r1", origin="direct", intersection_ids=["i1"])
    store.list_runs()[0].intersection_ids.append("mutated")
    assert store.get("r1").intersection_ids == ["i1"]


def test_list_runs_rejects_a_non_positive_limit() -> None:
    store = InMemoryCityJobStore()
    with pytest.raises(ValueError):
        store.list_runs(limit=0)
