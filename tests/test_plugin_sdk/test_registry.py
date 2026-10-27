"""Plugin registry: built-ins, explicit registration, entry-point discovery (no simulation)."""

from __future__ import annotations

import pytest
from terasim_plugin_sdk import (
    API_VERSION,
    ENTRY_POINT_GROUP,
    PluginNotFoundError,
    PluginRegistrationError,
    PluginRegistry,
    TrafficOperationPlugin,
    get_plugin,
    is_compatible_api_version,
    list_plugins,
    register,
)
from terasim_plugin_sdk.builtin import SignalTimingPlugin

from tests.test_plugin_sdk.conftest import make_distribution


class EchoPlugin:
    """A syntactically valid plugin; its run is never called in these tests."""

    name = "echo_ops"
    version = "1.2.3"
    api_version = API_VERSION
    description = "test double"

    def run(self, request, progress=None):  # pragma: no cover - not run here
        raise NotImplementedError


def _variant(**attributes):
    return type("Variant", (EchoPlugin,), attributes)()


def test_builtin_signal_timing_is_always_registered():
    registry = PluginRegistry(entry_points=False)
    assert registry.names() == ["signal_timing"]
    plugin = registry.get("signal_timing")
    assert isinstance(plugin, SignalTimingPlugin)
    assert isinstance(plugin, TrafficOperationPlugin)
    info = registry.info("signal_timing")
    assert (info.source, info.api_version) == ("builtin", API_VERSION)
    assert PluginRegistry(builtins=False, entry_points=False).names() == []


def test_explicit_registration_duplicates_and_replace():
    registry = PluginRegistry(entry_points=False)
    info = registry.register(EchoPlugin())
    assert (info.name, info.version, info.source) == ("echo_ops", "1.2.3", "explicit")
    assert info.target.endswith(":EchoPlugin")
    assert "echo_ops" in registry and len(registry) == 2

    with pytest.raises(PluginRegistrationError, match="already registered"):
        registry.register(EchoPlugin())
    with pytest.raises(PluginRegistrationError, match="already registered"):
        registry.register(_variant(name="signal_timing"))

    replacement = _variant(version="2.0.0")
    registry.register(replacement, replace=True)
    assert registry.get("echo_ops") is replacement

    registry.unregister("echo_ops")
    assert registry.names() == ["signal_timing"]
    with pytest.raises(PluginNotFoundError):
        registry.unregister("echo_ops")


@pytest.mark.parametrize(
    "attributes, message",
    [
        ({"name": "Echo Ops"}, "lowercase identifier"),
        ({"name": ""}, "lowercase identifier"),
        ({"version": ""}, "version"),
        ({"api_version": "2.0"}, "not compatible"),
        ({"api_version": "1.99"}, "not compatible"),
        ({"api_version": "one"}, "not compatible"),
        ({"run": None}, "must be a method"),
        ({"run": lambda self: None}, "accept \\(request, progress=None\\)"),
        ({"description": 5}, "description"),
    ],
)
def test_invalid_plugins_are_rejected(attributes, message):
    registry = PluginRegistry(entry_points=False)
    with pytest.raises(PluginRegistrationError, match=message):
        registry.register(_variant(**attributes))


def test_api_version_rule():
    assert is_compatible_api_version(API_VERSION)
    assert is_compatible_api_version("1.0", sdk_api_version="1.3")
    assert not is_compatible_api_version("1.4", sdk_api_version="1.3")
    assert not is_compatible_api_version("2.0", sdk_api_version="1.3")
    assert not is_compatible_api_version("0.9", sdk_api_version="1.3")
    assert not is_compatible_api_version(1.0)


def test_unknown_plugin_lists_the_known_ones():
    registry = PluginRegistry(entry_points=False)
    with pytest.raises(PluginNotFoundError, match="registered plugins: signal_timing") as caught:
        registry.get("ramp_metering")
    assert caught.value.name == "ramp_metering"
    assert isinstance(caught.value, LookupError)


def test_entry_point_discovery_finds_installed_distributions(tmp_path, monkeypatch):
    """A distribution on sys.path publishing the example plugin is discovered by name."""
    site = tmp_path / "site"
    make_distribution(
        site,
        "terasim-webster-fixed-time",
        {"webster_fixed_time": "webster_fixed_time:WebsterFixedTimePlugin"},
        version="0.1.0",
    )
    monkeypatch.syspath_prepend(str(site))

    registry = PluginRegistry()
    assert registry.names() == ["signal_timing", "webster_fixed_time"]
    info = registry.info("webster_fixed_time")
    assert info.source == "entry_point"
    assert info.distribution == "terasim-webster-fixed-time 0.1.0"
    assert info.target == "webster_fixed_time:WebsterFixedTimePlugin"
    assert registry.load_errors == ()

    from webster_fixed_time import WebsterFixedTimePlugin

    assert isinstance(registry.get("webster_fixed_time"), WebsterFixedTimePlugin)


def test_broken_entry_points_are_skipped_and_reported(tmp_path, monkeypatch):
    site = tmp_path / "site"
    make_distribution(
        site,
        "third-party-ops",
        {
            "good_ops": "third_party_ops:GoodPlugin",
            "factory_ops": "third_party_ops:make_plugin",
            "broken_import": "no_such_module_anywhere:Plugin",
            "mismatch_ops": "third_party_ops:GoodPlugin",
            "future_ops": "third_party_ops:FuturePlugin",
            "signal_timing": "third_party_ops:ShadowPlugin",
            "Bad-Name": "third_party_ops:GoodPlugin",
        },
        modules={
            "third_party_ops": f"""
                class GoodPlugin:
                    name = "good_ops"
                    version = "0.3.0"
                    api_version = "{API_VERSION}"

                    def run(self, request, progress=None):
                        raise NotImplementedError


                class FactoryPlugin(GoodPlugin):
                    name = "factory_ops"


                def make_plugin():
                    return FactoryPlugin()


                class FuturePlugin(GoodPlugin):
                    name = "future_ops"
                    api_version = "9.0"


                class ShadowPlugin(GoodPlugin):
                    name = "signal_timing"
            """,
        },
    )
    monkeypatch.syspath_prepend(str(site))

    registry = PluginRegistry()
    assert registry.names() == ["factory_ops", "good_ops", "signal_timing"]
    assert isinstance(registry.get("signal_timing"), SignalTimingPlugin)  # never shadowed
    errors = {error.name: error.error for error in registry.load_errors}
    assert set(errors) == {
        "broken_import",
        "mismatch_ops",
        "future_ops",
        "signal_timing",
        "Bad-Name",
    }
    assert "could not load" in errors["broken_import"]
    assert "differs from the plugin's name" in errors["mismatch_ops"]
    assert "not compatible" in errors["future_ops"]
    assert "duplicate plugin name" in errors["signal_timing"]
    assert "must match" in errors["Bad-Name"]
    assert all(error.distribution == "third-party-ops 1.0.0" for error in registry.load_errors)


def test_entry_points_are_read_once_and_on_demand(monkeypatch):
    calls = []

    def fake_entry_points(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr("terasim_plugin_sdk.registry.metadata.entry_points", fake_entry_points)
    registry = PluginRegistry()
    assert calls == []
    registry.names()
    registry.get("signal_timing")
    assert calls == [{"group": ENTRY_POINT_GROUP}]
    registry.discover(force=True)
    assert len(calls) == 2


def test_process_wide_registry_functions(fresh_default_registry, monkeypatch):
    monkeypatch.setattr("terasim_plugin_sdk.registry.metadata.entry_points", lambda **kwargs: [])
    register(EchoPlugin())
    assert isinstance(get_plugin("echo_ops"), EchoPlugin)
    assert [info.name for info in list_plugins()] == ["echo_ops", "signal_timing"]
