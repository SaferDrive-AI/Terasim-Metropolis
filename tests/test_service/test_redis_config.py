"""Tests for env-driven Redis connection config (redis_connection_kwargs_from_env)."""

from __future__ import annotations

import pytest

from terasim_service.constants import (
    ENVIRONMENT_VARIABLE_DB,
    ENVIRONMENT_VARIABLE_HOST,
    ENVIRONMENT_VARIABLE_PASSWORD,
    ENVIRONMENT_VARIABLE_PORT,
)
from terasim_service.utils import check_redis_connection, redis_connection_kwargs_from_env

_ALL = (
    ENVIRONMENT_VARIABLE_HOST,
    ENVIRONMENT_VARIABLE_PORT,
    ENVIRONMENT_VARIABLE_DB,
    ENVIRONMENT_VARIABLE_PASSWORD,
)


def _clear(monkeypatch):
    for name in _ALL:
        monkeypatch.delenv(name, raising=False)


def test_defaults_when_env_unset(monkeypatch):
    _clear(monkeypatch)
    assert redis_connection_kwargs_from_env() == {"host": "localhost", "port": 6379, "db": 0}


def test_env_overrides(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_HOST, "redis.internal")
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_PORT, "6380")
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_DB, "2")
    assert redis_connection_kwargs_from_env() == {
        "host": "redis.internal",
        "port": 6380,
        "db": 2,
    }


def test_password_included_only_when_set(monkeypatch):
    _clear(monkeypatch)
    assert "password" not in redis_connection_kwargs_from_env()
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_PASSWORD, "secret")
    assert redis_connection_kwargs_from_env()["password"] == "secret"


def test_empty_password_not_passed(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_PASSWORD, "")
    assert "password" not in redis_connection_kwargs_from_env()


def test_bad_port_raises(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_PORT, "not-a-port")
    with pytest.raises(ValueError, match="PORT"):
        redis_connection_kwargs_from_env()


def test_bad_db_raises(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_DB, "x")
    with pytest.raises(ValueError, match="DB"):
        redis_connection_kwargs_from_env()


def test_port_below_range_raises(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_PORT, "0")
    with pytest.raises(ValueError, match="PORT"):
        redis_connection_kwargs_from_env()


def test_port_above_range_raises(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_PORT, "65536")
    with pytest.raises(ValueError, match="PORT"):
        redis_connection_kwargs_from_env()


def test_negative_db_raises(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_DB, "-1")
    with pytest.raises(ValueError, match="DB"):
        redis_connection_kwargs_from_env()


def test_check_redis_connection_uses_env_settings(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_HOST, "redis.internal")
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_PORT, "6390")
    captured = {}

    class _FakeRedis:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def ping(self):
            return True

    monkeypatch.setattr("terasim_service.utils.base.redis.Redis", _FakeRedis)
    check_redis_connection()  # ping ok → connects, does not exit
    assert captured["host"] == "redis.internal"
    assert captured["port"] == 6390


def test_check_redis_connection_exits_on_redis_error(monkeypatch):
    import redis

    _clear(monkeypatch)

    class _FailingRedis:
        def __init__(self, **kwargs):
            pass

        def ping(self):
            raise redis.TimeoutError("boom")

    monkeypatch.setattr("terasim_service.utils.base.redis.Redis", _FailingRedis)
    with pytest.raises(SystemExit):
        check_redis_connection()
