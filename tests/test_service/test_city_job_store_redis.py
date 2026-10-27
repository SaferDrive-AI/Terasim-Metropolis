"""Integration tests for the city job store against a REAL Redis.

Default-skipped unless a Redis is reachable (same probe idiom the repo uses for
sumolib/sample-data). No SUMO and no full job run — these verify only the part
that can't be exercised in-memory: the atomic Lua acquire/renew/expiry and that
a child process writing through Redis is visible to the parent. Each test uses a
unique key prefix and cleans it up afterwards.
"""

from __future__ import annotations

import multiprocessing
import time
import uuid

import pytest

from terasim_service.city.job_store import COMPLETED, RedisCityJobStore
from terasim_service.utils.base import redis_connection_kwargs_from_env


def _redis_available() -> bool:
    try:
        import redis

        client = redis.Redis(
            **redis_connection_kwargs_from_env(), socket_connect_timeout=0.5
        )
        client.ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_available(), reason="Redis not available")


def _config(prefix: str, *, max_concurrency: int = 1, lease_ttl: float = 100.0) -> dict:
    return {
        "redis": redis_connection_kwargs_from_env(),
        "max_concurrency": max_concurrency,
        "lease_ttl": lease_ttl,
        "key_prefix": prefix,
    }


def _store_from(config: dict) -> RedisCityJobStore:
    import redis

    return RedisCityJobStore(
        client=redis.Redis(**config["redis"]),
        max_concurrency=config["max_concurrency"],
        lease_ttl=config["lease_ttl"],
        key_prefix=config["key_prefix"],
    )


# Module-level so macOS spawn can import it; writes status through its OWN store
# built from the same config the parent used.
def _child_writes_status(config: dict, run_id: str) -> None:
    store = _store_from(config)
    store.set_running(run_id)
    store.set_completed(
        run_id, "/unused/corridor_optimization_result.json", {"plan_id": "pX"}
    )


@pytest.fixture
def prefix():
    name = f"test_city_store_{uuid.uuid4().hex}"
    yield name
    import redis

    client = redis.Redis(**redis_connection_kwargs_from_env())
    for key in client.scan_iter(f"{name}:*"):
        client.delete(key)


def test_lua_acquire_renew_release(prefix) -> None:
    store = _store_from(_config(prefix, max_concurrency=1, lease_ttl=100))
    store.create("r1")
    assert store.try_acquire_slots("r1", 1) is True
    assert store.try_acquire_slots("r2", 1) is False  # cap reached
    assert store.renew_slot("r1") is True
    store.release_slot("r1")
    assert store.try_acquire_slots("r2", 1) is True  # slot freed


def test_lua_lease_expiry_reclaims_slot(prefix) -> None:
    store = _store_from(_config(prefix, max_concurrency=1, lease_ttl=1))
    store.create("r1")
    assert store.try_acquire_slots("r1", 1) is True
    assert store.try_acquire_slots("r2", 1) is False
    time.sleep(1.3)  # let r1's lease lapse
    assert store.try_acquire_slots("r2", 1) is True  # expired lease pruned, slot reclaimed
    assert store.renew_slot("r1") is False  # expired lease must NOT be revived


def test_child_process_writes_parent_reads(prefix) -> None:
    config = _config(prefix, max_concurrency=2, lease_ttl=100)
    parent = _store_from(config)
    parent.create("rX")

    proc = multiprocessing.Process(target=_child_writes_status, args=(config, "rX"))
    proc.start()
    proc.join(timeout=10)
    assert proc.exitcode == 0

    record = parent.get("rX")
    assert record is not None
    assert record.status == COMPLETED
    assert record.best == {"plan_id": "pX"}


def test_holds_slots_follows_the_lease(prefix) -> None:
    store = _store_from(_config(prefix, max_concurrency=2, lease_ttl=1))
    assert store.holds_slots("r1") is False
    assert store.try_acquire_slots("r1", 2) is True
    assert store.holds_slots("r1") is True
    time.sleep(1.3)  # let the lease lapse
    assert store.holds_slots("r1") is False
    assert store.try_acquire_slots("r2", 1) is True
    store.release_slot("r2")
    assert store.holds_slots("r2") is False


def test_run_metadata_round_trips_through_redis(prefix) -> None:
    store = _store_from(_config(prefix))
    store.create(
        "r1", origin="direct", corridor_id="c1", intersection_ids=["i1", "i2"],
        traffic_model="cnde",
    )
    record = store.get("r1")
    assert (record.origin, record.corridor_id, record.intersection_ids) == (
        "direct", "c1", ["i1", "i2"],
    )
    assert record.traffic_model == "cnde"
    assert record.submitted_at is not None and record.completed_at is None

    store.set_completed("r1", "/unused/result.json", {"plan_id": "p"})
    record = store.get("r1")
    assert record.completed_at is not None and record.completed_at >= record.submitted_at

    store.create("r2")  # no metadata: the pre-listing record shape
    record = store.get("r2")
    assert (record.origin, record.corridor_id, record.intersection_ids) == (None, None, None)
    assert record.traffic_model is None


def test_list_runs_reads_the_redis_index_newest_first(prefix) -> None:
    store = _store_from(_config(prefix))
    for index, corridor in enumerate(["a", "b", "a"]):
        store.create(f"r{index}", origin="api", corridor_id=corridor)
        time.sleep(0.01)  # distinct submission times

    assert [rec.run_id for rec in store.list_runs()] == ["r2", "r1", "r0"]
    assert [rec.run_id for rec in store.list_runs(limit=2)] == ["r2", "r1"]
    assert [rec.run_id for rec in store.list_runs(corridor_id="a")] == ["r2", "r0"]
    assert store.list_runs(corridor_id="none") == []


def test_list_runs_skips_and_drops_expired_records(prefix) -> None:
    import redis

    store = _store_from(_config(prefix))
    store.create("r0", origin="api", corridor_id="a")
    time.sleep(0.01)
    store.create("r1", origin="api", corridor_id="a")
    client = redis.Redis(**redis_connection_kwargs_from_env())
    client.delete(f"{prefix}:r1")  # the record's retention TTL ran out

    assert [rec.run_id for rec in store.list_runs()] == ["r0"]
    assert [rec.run_id for rec in store.list_runs(corridor_id="a")] == ["r0"]
    assert client.zscore(f"{prefix}:runs", "r1") is None  # dropped from the index
