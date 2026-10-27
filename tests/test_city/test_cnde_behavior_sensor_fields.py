"""CndeVehicleFactory gives CNDE background vehicles the slim ego-sensor field set."""

from __future__ import annotations

import pytest

from terasim_city.factories.cnde_behavior import (
    CNDE_EGO_SENSOR_FIELD_NAMES,
    FULL_OBSERVATION_ENV_VAR,
    CndeVehicleFactory,
)


def test_slim_fields_are_a_subset_of_the_nde_defaults(monkeypatch):
    monkeypatch.delenv(FULL_OBSERVATION_ENV_VAR, raising=False)
    pytest.importorskip("terasim_nde_nade")
    from terasim_nde_nade.vehicle.nde_ego_sensor import NDEEgoSensor

    fields = CndeVehicleFactory()._ego_sensor_fields()
    assert tuple(fields) == CNDE_EGO_SENSOR_FIELD_NAMES
    defaults = NDEEgoSensor.DEFAULT_PARAMS["fields"]
    for name, getter in fields.items():
        assert getter is defaults[name]
    # the expensive derived context stays out: it needs next_links
    assert "next_links" not in fields


def test_full_observation_switch_restores_the_default_set(monkeypatch):
    monkeypatch.setenv(FULL_OBSERVATION_ENV_VAR, "1")
    assert CndeVehicleFactory()._ego_sensor_fields() is None
