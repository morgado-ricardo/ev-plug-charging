"""Integration-level tests using pytest-homeassistant-custom-component: a
real (test) Home Assistant instance, real entity platforms, a mocked PSACC
HTTP endpoint. This is what proves the HA-facing layer (coordinator,
config_flow, entity platforms) actually wires together, as opposed to the
pure-logic suite's proof that the DECISIONS are correct.

Requires pytest-homeassistant-custom-component; skipped automatically if
it is not installed, since it pulls in a large and sometimes
hard-to-build dependency tree. `pip install -r requirements-test.txt`
gets it -- see README.md's Development section.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

# HA's test loader (via enable_custom_integrations) discovers a custom
# integration by finding a `custom_components` PACKAGE on sys.path and
# loading `custom_components.<domain>` from inside it -- so the REPO ROOT
# (which contains custom_components/) must be on sys.path, not
# custom_components/ itself (conftest.py adds the latter, for the pure
# logic tests' `import ev_plug_charging.X` to work directly).
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# pytest_homeassistant_custom_component imports ITS OWN `custom_components`
# namespace package (from its own testing_config/ directory) as a side
# effect of plugin registration, which happens before this module's own
# sys.path.insert above ever runs -- so `custom_components` is already
# cached in sys.modules with a __path__ that does not include ours. A
# namespace package's __path__ is an ordinary mutable list, so the fix is
# to extend the cached one rather than re-import (re-importing would just
# return the same cached, incomplete module).
import custom_components  # noqa: E402

_our_path = str(_REPO_ROOT / "custom_components")
if _our_path not in custom_components.__path__:
    custom_components.__path__.append(_our_path)

from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_mock_service,
)

pytest_plugins = "pytest_homeassistant_custom_component"


VEHICLE_INFO_RESPONSE = {
    "energy": [
        {
            "level": 55,
            "charging": {"status": "Disconnected", "plugged": False},
        }
    ]
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def _setup_entry(hass, aioclient_mock, plug_switch="switch.plug", plug_power="sensor.plug_power"):
    from ev_plug_charging.const import (
        CONF_BATTERY_CAPACITY_KWH,
        CONF_CHARGE_CURRENT_A,
        CONF_PLUG_POWER_SENSOR,
        CONF_PLUG_SWITCH,
        CONF_POLL_INTERVAL,
        CONF_PSACC_URL,
        CONF_SOURCE_TYPE,
        CONF_TEMP_LIMIT,
        CONF_VIN,
        CONFIG_VERSION,
        DOMAIN,
        SOURCE_TYPE_PSACC,
    )

    hass.states.async_set(plug_switch, "off")
    hass.states.async_set(plug_power, "0")

    aioclient_mock.get(
        "http://psacc.example/get_vehicleinfo/VF1TESTVIN?from_cache=1",
        json=VEHICLE_INFO_RESPONSE,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=CONFIG_VERSION,
        data={
            CONF_SOURCE_TYPE: SOURCE_TYPE_PSACC,
            CONF_PSACC_URL: "http://psacc.example",
            CONF_VIN: "VF1TESTVIN",
            CONF_PLUG_SWITCH: plug_switch,
            CONF_PLUG_POWER_SENSOR: plug_power,
            CONF_BATTERY_CAPACITY_KWH: 50.0,
            CONF_CHARGE_CURRENT_A: 8.0,
            CONF_TEMP_LIMIT: 65.0,
            CONF_POLL_INTERVAL: 120,
        },
    )
    entry.add_to_hass(hass)

    # The coordinator's very first refresh can decide to actuate the plug
    # (below_target, in-window at whatever real wall-clock instant the
    # test happens to run) -- and async_setup_entry runs that refresh
    # BEFORE forwarding the switch platform, so the real `switch` domain
    # services aren't registered yet. Register fakes so a tick that
    # actuates doesn't fail with ServiceNotFound depending on the clock.
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")

    result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, result


async def test_setup_entry_succeeds_and_creates_entities(hass, aioclient_mock):
    entry, result = await _setup_entry(hass, aioclient_mock)
    assert result is True
    assert entry.state.value == "loaded"

    # All seven platforms should have created at least one entity each.
    all_states = hass.states.async_all()
    domains_seen = {s.entity_id.split(".")[0] for s in all_states}
    for platform in ("select", "switch", "number", "time", "sensor", "binary_sensor", "button"):
        assert platform in domains_seen, f"no entities created for platform {platform}"


async def test_coordinator_polled_psacc_and_evaluated(hass, aioclient_mock):
    """Outside the charge window there is nothing to do regardless of SoC --
    this pins that down at a fixed midday instant so the assertion doesn't
    depend on the real wall-clock time the test happens to run at (the
    default window is 23:00-07:00, which a plain `dt_util.now()` can land
    inside or outside depending on the hour)."""
    from unittest.mock import patch

    from ev_plug_charging.const import DOMAIN

    noon = dt_util.now().replace(hour=12, minute=0, second=0, microsecond=0)
    with patch("homeassistant.util.dt.now", return_value=noon):
        entry, _ = await _setup_entry(hass, aioclient_mock)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert aioclient_mock.call_count >= 1
    assert coordinator.last_decision is not None
    # Vehicle reports Disconnected/not plugged, plug is off, and it's
    # midday (outside the 23:00-07:00 window) -> nothing to do.
    from ev_plug_charging.models import PlugAction

    assert coordinator.last_decision.plug == PlugAction.UNCHANGED


async def test_enable_switch_reflects_settings_and_can_be_toggled(hass, aioclient_mock):
    from ev_plug_charging.const import DOMAIN

    entry, _ = await _setup_entry(hass, aioclient_mock)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert coordinator.settings.enabled is True

    entity_reg = hass.states.async_all("switch")
    enabled_switch = next(s for s in entity_reg if "enabled" in s.entity_id)
    assert enabled_switch.state == "on"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": enabled_switch.entity_id}, blocking=True
    )
    await hass.async_block_till_done()
    assert coordinator.settings.enabled is False


async def test_unload_entry_cleans_up(hass, aioclient_mock):
    from ev_plug_charging.const import DOMAIN

    entry, _ = await _setup_entry(hass, aioclient_mock)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


# --------------------------------------------------------------------- #
# The telemetry-source abstraction
# --------------------------------------------------------------------- #


async def test_v1_entry_is_migrated_and_backfilled_to_psacc(hass, aioclient_mock):
    """Entries created before source_type existed are all PSACC by
    definition -- that was the only thing this could talk to. The migration
    must backfill them rather than leaving the registry unable to look the
    source up."""
    from ev_plug_charging.const import (
        CONF_PLUG_POWER_SENSOR,
        CONF_PLUG_SWITCH,
        CONF_PSACC_URL,
        CONF_SOURCE_TYPE,
        CONF_VIN,
        CONFIG_VERSION,
        DOMAIN,
        SOURCE_TYPE_PSACC,
    )

    hass.states.async_set("switch.plug", "off")
    hass.states.async_set("sensor.plug_power", "0")
    aioclient_mock.get(
        "http://psacc.example/get_vehicleinfo/VF1TESTVIN?from_cache=1",
        json=VEHICLE_INFO_RESPONSE,
    )

    # A v1 entry: note the absence of CONF_SOURCE_TYPE.
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            CONF_PSACC_URL: "http://psacc.example",
            CONF_VIN: "VF1TESTVIN",
            CONF_PLUG_SWITCH: "switch.plug",
            CONF_PLUG_POWER_SENSOR: "sensor.plug_power",
        },
    )
    entry.add_to_hass(hass)
    assert CONF_SOURCE_TYPE not in entry.data

    # See the comment in _setup_entry: the first refresh can actuate the
    # plug before the real switch services are forwarded.
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == CONFIG_VERSION
    assert entry.data[CONF_SOURCE_TYPE] == SOURCE_TYPE_PSACC

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.source is not None
    assert coordinator.source.source_type == SOURCE_TYPE_PSACC


async def test_config_flow_picks_a_source_then_configures_it(hass, aioclient_mock):
    """The full four-step flow: source type -> that source's own fields
    (connection-tested) -> the plug -> advanced."""
    from homeassistant.data_entry_flow import FlowResultType

    from ev_plug_charging.const import (
        CONF_PLUG_ENERGY_SENSOR,
        CONF_PLUG_POWER_SENSOR,
        CONF_PLUG_SWITCH,
        CONF_PSACC_URL,
        CONF_SOURCE_TYPE,
        CONF_VIN,
        DOMAIN,
        SOURCE_TYPE_PSACC,
    )

    hass.states.async_set("switch.plug", "off")
    hass.states.async_set("sensor.plug_power", "0")
    aioclient_mock.get(
        "http://psacc.example/get_vehicleinfo/VF1TESTVIN?from_cache=1",
        json=VEHICLE_INFO_RESPONSE,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_TYPE: SOURCE_TYPE_PSACC}
    )
    assert result["step_id"] == "psacc"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_PSACC_URL: "http://psacc.example",
            CONF_VIN: "VF1TESTVIN",
        },
    )
    assert result["step_id"] == "actuator"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_PLUG_SWITCH: "switch.plug",
            CONF_PLUG_POWER_SENSOR: "sensor.plug_power",
        },
    )
    assert result["step_id"] == "advanced"

    # CONF_PLUG_ENERGY_SENSOR is the one field in this step with no
    # default -- everything else can be left as {} and still validate.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PLUG_ENERGY_SENSOR: "sensor.plug_energy"}
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SOURCE_TYPE] == SOURCE_TYPE_PSACC
    assert result["data"][CONF_PSACC_URL] == "http://psacc.example"


async def test_advanced_step_requires_the_plug_energy_sensor(hass, aioclient_mock):
    """Unlike every other advanced-step field, there is no sensible default
    to fall back to for a sensor nobody has picked yet -- submitting the
    step with nothing at all must be rejected, not silently create an
    entry with session cost permanently stuck at 0. Schema validation
    failures surface as InvalidData at this level (a production run's
    frontend catches it and re-shows the form with errors; that layer
    isn't exercised by calling async_configure directly)."""
    from homeassistant.data_entry_flow import InvalidData

    from ev_plug_charging.const import (
        CONF_PLUG_POWER_SENSOR,
        CONF_PLUG_SWITCH,
        CONF_PSACC_URL,
        CONF_SOURCE_TYPE,
        CONF_VIN,
        DOMAIN,
        SOURCE_TYPE_PSACC,
    )

    hass.states.async_set("switch.plug", "off")
    hass.states.async_set("sensor.plug_power", "0")
    aioclient_mock.get(
        "http://psacc.example/get_vehicleinfo/VF1TESTVIN?from_cache=1",
        json=VEHICLE_INFO_RESPONSE,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_TYPE: SOURCE_TYPE_PSACC}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PSACC_URL: "http://psacc.example", CONF_VIN: "VF1TESTVIN"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_PLUG_SWITCH: "switch.plug",
            CONF_PLUG_POWER_SENSOR: "sensor.plug_power",
        },
    )
    assert result["step_id"] == "advanced"

    with pytest.raises(InvalidData) as excinfo:
        await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert excinfo.value.path == ["plug_energy_sensor_entity_id"]


async def test_config_flow_reports_an_unreachable_source(hass, aioclient_mock):
    """A wrong address is caught in the flow, not three retries into the
    first poll tonight."""
    from homeassistant.data_entry_flow import FlowResultType

    from ev_plug_charging.const import (
        CONF_PSACC_URL,
        CONF_SOURCE_TYPE,
        CONF_VIN,
        DOMAIN,
        SOURCE_TYPE_PSACC,
    )

    aioclient_mock.get(
        "http://nope.example/get_vehicleinfo/VF1TESTVIN?from_cache=1", status=500
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_TYPE: SOURCE_TYPE_PSACC}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_PSACC_URL: "http://nope.example",
            CONF_VIN: "VF1TESTVIN",
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "psacc"
    assert result["errors"]["base"] == "invalid_response"


# --------------------------------------------------------------------------- #
# Notification targets (CONF_NOTIFY_TARGETS): fan-out, isolation, the
# legacy-service-vs-notify-entity split, and the v2 -> v3 migration.
# --------------------------------------------------------------------------- #


async def test_notify_fans_out_to_every_configured_target(hass):
    """Two targets configured -> both receive the push. Calling
    async_dispatch_events directly (rather than driving a real charge)
    tests the chokepoint itself, which is where the fan-out logic lives."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    from ev_plug_charging.const import CONF_NOTIFY_TARGETS, DOMAIN
    from ev_plug_charging.models import Event
    from ev_plug_charging.notify import async_dispatch_events

    calls_a = async_mock_service(hass, "notify", "phone_a")
    calls_b = async_mock_service(hass, "notify", "phone_b")

    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_NOTIFY_TARGETS: ["notify.phone_a", "notify.phone_b"]},
    )
    entry.add_to_hass(hass)

    await async_dispatch_events(
        hass, entry, [Event(name=f"{DOMAIN}_charge_started", data={})]
    )

    assert len(calls_a) == 1
    assert len(calls_b) == 1
    assert calls_a[0].data["message"]
    assert calls_b[0].data["message"]


async def test_one_bad_target_does_not_stop_the_others(hass, caplog):
    """A target with no matching service AND no matching entity must not
    abort the loop -- every OTHER target still gets the push."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    from ev_plug_charging.const import CONF_NOTIFY_TARGETS, DOMAIN
    from ev_plug_charging.models import Event
    from ev_plug_charging.notify import async_dispatch_events

    calls_good = async_mock_service(hass, "notify", "phone_good")

    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_NOTIFY_TARGETS: ["notify.does_not_exist", "notify.phone_good"]
        },
    )
    entry.add_to_hass(hass)

    await async_dispatch_events(
        hass, entry, [Event(name=f"{DOMAIN}_charge_started", data={})]
    )

    assert len(calls_good) == 1
    assert "does_not_exist" in caplog.text


async def test_critical_event_keeps_the_critical_channel(hass):
    """The regression guard for the whole feature: a critical event through
    a LEGACY target must still carry the critical-alert payload."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    from ev_plug_charging.const import CONF_NOTIFY_TARGETS, DOMAIN
    from ev_plug_charging.models import Event
    from ev_plug_charging.notify import async_dispatch_events

    calls = async_mock_service(hass, "notify", "phone")

    entry = MockConfigEntry(domain=DOMAIN, options={CONF_NOTIFY_TARGETS: ["notify.phone"]})
    entry.add_to_hass(hass)

    await async_dispatch_events(
        hass, entry, [Event(name=f"{DOMAIN}_overheat_cutoff", data={"temp_c": 90.0})]
    )

    assert len(calls) == 1
    data = calls[0].data["data"]
    assert data["channel"] == "EV Charging Critical"
    assert data["push"]["sound"]["critical"] == 1


async def test_notify_entity_target_falls_back_to_send_message_without_data(hass, caplog):
    """A notify ENTITY with no matching legacy service routes to
    send_message with ONLY entity_id/title/message -- passing `data` there
    raises vol.Invalid (verified against HA's own notify/__init__.py
    schema), so it must never be attempted."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    from ev_plug_charging.const import CONF_NOTIFY_TARGETS, DOMAIN
    from ev_plug_charging.models import Event
    from ev_plug_charging.notify import async_dispatch_events

    hass.states.async_set("notify.phone_entity", "unknown")
    calls = async_mock_service(hass, "notify", "send_message")

    entry = MockConfigEntry(
        domain=DOMAIN, options={CONF_NOTIFY_TARGETS: ["notify.phone_entity"]}
    )
    entry.add_to_hass(hass)

    await async_dispatch_events(
        hass, entry, [Event(name=f"{DOMAIN}_charge_started", data={})]
    )

    assert len(calls) == 1
    assert calls[0].data["entity_id"] == "notify.phone_entity"
    assert "data" not in calls[0].data
    assert "critical alert" in caplog.text.lower()


async def test_legacy_service_is_preferred_when_both_a_service_and_an_entity_exist(hass):
    """A target string that resolves to BOTH a legacy service and a notify
    entity must use the legacy path -- it is the only one that can carry
    the critical-alert channel, so preferring it is what keeps overheat
    and 12V-critical pushes from being silently downgraded."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    from ev_plug_charging.const import CONF_NOTIFY_TARGETS, DOMAIN
    from ev_plug_charging.models import Event
    from ev_plug_charging.notify import async_dispatch_events

    hass.states.async_set("notify.phone", "unknown")  # also exists as an entity
    legacy_calls = async_mock_service(hass, "notify", "phone")
    entity_calls = async_mock_service(hass, "notify", "send_message")

    entry = MockConfigEntry(domain=DOMAIN, options={CONF_NOTIFY_TARGETS: ["notify.phone"]})
    entry.add_to_hass(hass)

    await async_dispatch_events(
        hass, entry, [Event(name=f"{DOMAIN}_charge_started", data={})]
    )

    assert len(legacy_calls) == 1
    assert len(entity_calls) == 0


async def test_notify_targets_scalar_is_coerced_to_a_list(hass):
    """A bare string in options (a hand-edited .storage file, or an entry
    that bypassed migration) must not be iterated as CHARACTERS -- it is
    one target, not len(string) of them."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    from ev_plug_charging.const import CONF_NOTIFY_TARGETS, DOMAIN
    from ev_plug_charging.models import Event
    from ev_plug_charging.notify import async_dispatch_events

    calls = async_mock_service(hass, "notify", "phone")

    entry = MockConfigEntry(domain=DOMAIN, options={CONF_NOTIFY_TARGETS: "notify.phone"})
    entry.add_to_hass(hass)

    await async_dispatch_events(
        hass, entry, [Event(name=f"{DOMAIN}_charge_started", data={})]
    )

    assert len(calls) == 1


async def test_v2_entry_migrates_scalar_notify_service_to_a_target_list(hass, aioclient_mock):
    """The v2 -> v3 arm of async_migrate_entry: the old scalar
    CONF_NOTIFY_SERVICE becomes a one-element CONF_NOTIFY_TARGETS list, and
    the old key is gone -- an entry is unambiguously old or new, never a
    third half-migrated shape."""
    from ev_plug_charging.const import (
        CONF_NOTIFY_SERVICE,
        CONF_NOTIFY_TARGETS,
        CONF_PLUG_POWER_SENSOR,
        CONF_PLUG_SWITCH,
        CONF_PSACC_URL,
        CONF_SOURCE_TYPE,
        CONF_VIN,
        CONFIG_VERSION,
        DOMAIN,
        SOURCE_TYPE_PSACC,
    )

    hass.states.async_set("switch.plug", "off")
    hass.states.async_set("sensor.plug_power", "0")
    aioclient_mock.get(
        "http://psacc.example/get_vehicleinfo/VF1TESTVIN?from_cache=1",
        json=VEHICLE_INFO_RESPONSE,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_SOURCE_TYPE: SOURCE_TYPE_PSACC,
            CONF_PSACC_URL: "http://psacc.example",
            CONF_VIN: "VF1TESTVIN",
            CONF_PLUG_SWITCH: "switch.plug",
            CONF_PLUG_POWER_SENSOR: "sensor.plug_power",
        },
        options={CONF_NOTIFY_SERVICE: "notify.old_phone"},
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == CONFIG_VERSION
    assert entry.options[CONF_NOTIFY_TARGETS] == ["notify.old_phone"]
    assert CONF_NOTIFY_SERVICE not in entry.options
    assert CONF_NOTIFY_SERVICE not in entry.data


async def test_migrate_v3_to_v4_preserves_tuned_values(hass, aioclient_mock):
    """The v3 -> v4 arm: the old kW/efficiency pair the setup wizard always
    wrote to `data` converts to amps/prior -- 1.84kW at 230V is exactly 8A,
    and the tuned efficiency carries forward UNCHANGED rather than being
    reset to a default. Both old keys are gone afterwards."""
    from ev_plug_charging.const import (
        CONF_CHARGE_CURRENT_A,
        CONF_CHARGE_EFFICIENCY,
        CONF_CHARGE_POWER_KW,
        CONF_EFFICIENCY_PRIOR,
        CONF_PLUG_POWER_SENSOR,
        CONF_PLUG_SWITCH,
        CONF_PSACC_URL,
        CONF_SOURCE_TYPE,
        CONF_VIN,
        CONFIG_VERSION,
        DOMAIN,
        SOURCE_TYPE_PSACC,
    )

    hass.states.async_set("switch.plug", "off")
    hass.states.async_set("sensor.plug_power", "0")
    aioclient_mock.get(
        "http://psacc.example/get_vehicleinfo/VF1TESTVIN?from_cache=1",
        json=VEHICLE_INFO_RESPONSE,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            CONF_SOURCE_TYPE: SOURCE_TYPE_PSACC,
            CONF_PSACC_URL: "http://psacc.example",
            CONF_VIN: "VF1TESTVIN",
            CONF_PLUG_SWITCH: "switch.plug",
            CONF_PLUG_POWER_SENSOR: "sensor.plug_power",
            CONF_CHARGE_POWER_KW: 1.84,
            CONF_CHARGE_EFFICIENCY: 0.82,
        },
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == CONFIG_VERSION
    assert entry.options[CONF_CHARGE_CURRENT_A] == pytest.approx(8.0)
    assert entry.data[CONF_EFFICIENCY_PRIOR] == 0.82
    assert CONF_CHARGE_POWER_KW not in entry.data
    assert CONF_CHARGE_POWER_KW not in entry.options
    assert CONF_CHARGE_EFFICIENCY not in entry.data
    assert CONF_CHARGE_EFFICIENCY not in entry.options


async def test_migrate_v3_to_v4_removes_stale_data_copy_when_options_is_newer(
    hass, aioclient_mock
):
    """An entry that has been through Options at least once has the CURRENT
    value in `options` (Options resubmits every advanced-step field) and a
    STALE copy still sitting in `data` (Options never touches `data`).
    Migration must prefer the options value and remove the stale one --
    not just pop whichever dict it checks first and leave the other."""
    from ev_plug_charging.const import (
        CONF_CHARGE_CURRENT_A,
        CONF_CHARGE_EFFICIENCY,
        CONF_CHARGE_POWER_KW,
        CONF_EFFICIENCY_PRIOR,
        CONF_PLUG_POWER_SENSOR,
        CONF_PLUG_SWITCH,
        CONF_PSACC_URL,
        CONF_SOURCE_TYPE,
        CONF_VIN,
        CONFIG_VERSION,
        DOMAIN,
        SOURCE_TYPE_PSACC,
    )

    hass.states.async_set("switch.plug", "off")
    hass.states.async_set("sensor.plug_power", "0")
    aioclient_mock.get(
        "http://psacc.example/get_vehicleinfo/VF1TESTVIN?from_cache=1",
        json=VEHICLE_INFO_RESPONSE,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            CONF_SOURCE_TYPE: SOURCE_TYPE_PSACC,
            CONF_PSACC_URL: "http://psacc.example",
            CONF_VIN: "VF1TESTVIN",
            CONF_PLUG_SWITCH: "switch.plug",
            CONF_PLUG_POWER_SENSOR: "sensor.plug_power",
            # Stale: the value the setup wizard wrote, since superseded.
            CONF_CHARGE_POWER_KW: 1.84,
            CONF_CHARGE_EFFICIENCY: 0.82,
        },
        options={
            # Current: what a later Options save actually left in place.
            CONF_CHARGE_POWER_KW: 3.68,  # 16A
            CONF_CHARGE_EFFICIENCY: 0.9,
        },
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == CONFIG_VERSION
    assert entry.options[CONF_CHARGE_CURRENT_A] == pytest.approx(16.0)
    assert entry.data[CONF_EFFICIENCY_PRIOR] == 0.9
    assert CONF_CHARGE_POWER_KW not in entry.data
    assert CONF_CHARGE_POWER_KW not in entry.options
    assert CONF_CHARGE_EFFICIENCY not in entry.data
    assert CONF_CHARGE_EFFICIENCY not in entry.options


async def test_options_flow_round_trips_notify_targets(hass, aioclient_mock):
    """The actual "reconfigurable without redoing the integration"
    requirement: submit targets through the options flow, confirm they
    persist. custom_value=True on the selector means a target string not
    already known to hass (no matching service or entity right now) is
    still accepted -- the trap this guards against is a target dropped
    silently because it wasn't in that render's live-computed option list."""
    from ev_plug_charging.const import (
        CONF_BATTERY_CAPACITY_KWH,
        CONF_CHARGE_CURRENT_A,
        CONF_MUTED_EVENTS,
        CONF_NOTIFY_TARGETS,
        CONF_PLUG_ENERGY_SENSOR,
        CONF_PLUG_POWER_SENSOR,
        CONF_PLUG_SWITCH,
        CONF_PSACC_URL,
        CONF_RESCUE_REFRESH_ENABLED,
        CONF_SOURCE_TYPE,
        CONF_TEMP_LIMIT,
        CONF_VIN,
        DOMAIN,
        SOURCE_TYPE_PSACC,
    )

    hass.states.async_set("switch.plug", "off")
    hass.states.async_set("sensor.plug_power", "0")
    aioclient_mock.get(
        "http://psacc.example/get_vehicleinfo/VF1TESTVIN?from_cache=1",
        json=VEHICLE_INFO_RESPONSE,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE_TYPE: SOURCE_TYPE_PSACC,
            CONF_PSACC_URL: "http://psacc.example",
            CONF_VIN: "VF1TESTVIN",
            CONF_PLUG_SWITCH: "switch.plug",
            CONF_PLUG_POWER_SENSOR: "sensor.plug_power",
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"

    # async_create_entry replaces the WHOLE options dict -- every field the
    # schema declares has to be submitted, or the others are blanked.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PLUG_ENERGY_SENSOR: "sensor.plug_energy",
            CONF_TEMP_LIMIT: 65.0,
            CONF_BATTERY_CAPACITY_KWH: 50.0,
            CONF_CHARGE_CURRENT_A: "8",
            "poll_interval_seconds": 120,
            CONF_NOTIFY_TARGETS: ["notify.phone_a", "notify.phone_b"],
            CONF_MUTED_EVENTS: [],
            CONF_RESCUE_REFRESH_ENABLED: True,
        },
    )
    await hass.async_block_till_done()

    assert entry.options[CONF_NOTIFY_TARGETS] == ["notify.phone_a", "notify.phone_b"]

    # Re-opening the form pre-fills what was just saved.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema_keys = {
        (k.schema if hasattr(k, "schema") else k): k
        for k in result["data_schema"].schema
    }
    targets_key = schema_keys[CONF_NOTIFY_TARGETS]
    assert targets_key.default() == ["notify.phone_a", "notify.phone_b"]


# --------------------------------------------------------------------------- #
# Attributing a plug actuation to this integration: a shared Context on the
# switch call, an EVENT_PLUG_COMMANDED describable by logbook.py, and a raw
# logbook_entry that reaches the plug's own history card.
# --------------------------------------------------------------------------- #


async def test_plug_actuation_carries_a_context_shared_with_its_event(hass, aioclient_mock):
    """The whole point of the feature: the switch.turn_on call and the
    EVENT_PLUG_COMMANDED that brackets it share one Context, which is what
    lets Home Assistant's logbook attribute the state change to this
    integration instead of recording an anonymous flip."""
    from unittest.mock import patch

    from pytest_homeassistant_custom_component.common import async_mock_service

    from ev_plug_charging.const import (
        CONF_PLUG_POWER_SENSOR,
        CONF_PLUG_SWITCH,
        CONF_PSACC_URL,
        CONF_SOURCE_TYPE,
        CONF_VIN,
        DOMAIN,
        EVENT_PLUG_COMMANDED,
        SOURCE_TYPE_PSACC,
    )

    hass.states.async_set("switch.plug", "off")
    hass.states.async_set("sensor.plug_power", "0")
    # SoC 55 vs the default 80% target -> below_target -> plug ON on the
    # very first refresh, PROVIDED it runs inside the default 23:00-07:00
    # window -- frozen here so the assertion doesn't depend on the real
    # wall-clock time the test happens to run at.
    aioclient_mock.get(
        "http://psacc.example/get_vehicleinfo/VF1TESTVIN?from_cache=1",
        json=VEHICLE_INFO_RESPONSE,
    )
    switch_calls = async_mock_service(hass, "switch", "turn_on")

    commanded_events = []
    hass.bus.async_listen(EVENT_PLUG_COMMANDED, commanded_events.append)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE_TYPE: SOURCE_TYPE_PSACC,
            CONF_PSACC_URL: "http://psacc.example",
            CONF_VIN: "VF1TESTVIN",
            CONF_PLUG_SWITCH: "switch.plug",
            CONF_PLUG_POWER_SENSOR: "sensor.plug_power",
        },
    )
    entry.add_to_hass(hass)
    midnight = dt_util.now().replace(hour=23, minute=30, second=0, microsecond=0)
    with patch("homeassistant.util.dt.now", return_value=midnight):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert len(switch_calls) == 1
    assert len(commanded_events) == 1
    assert switch_calls[0].context is not None
    assert switch_calls[0].context.id == commanded_events[0].context.id
    assert commanded_events[0].data["action"] == "on"
    assert commanded_events[0].data["reason"] == "below_target"


async def test_actuation_event_is_fired_before_the_switch_service_call(hass, aioclient_mock):
    """Load-bearing ordering, not cosmetic: the logbook attributes a
    context to whichever row is EARLIEST to carry it. Fire the event after
    the switch call instead, and the plug's own state-change row wins that
    slot and there is nothing left to attribute it to."""
    from unittest.mock import patch

    from pytest_homeassistant_custom_component.common import async_mock_service

    from ev_plug_charging.const import (
        CONF_PLUG_POWER_SENSOR,
        CONF_PLUG_SWITCH,
        CONF_PSACC_URL,
        CONF_SOURCE_TYPE,
        CONF_VIN,
        DOMAIN,
        EVENT_PLUG_COMMANDED,
        SOURCE_TYPE_PSACC,
    )

    hass.states.async_set("switch.plug", "off")
    hass.states.async_set("sensor.plug_power", "0")
    aioclient_mock.get(
        "http://psacc.example/get_vehicleinfo/VF1TESTVIN?from_cache=1",
        json=VEHICLE_INFO_RESPONSE,
    )

    from homeassistant.const import EVENT_CALL_SERVICE

    order = []
    async_mock_service(hass, "switch", "turn_on")
    hass.bus.async_listen(EVENT_PLUG_COMMANDED, lambda e: order.append("event"))

    def _on_call_service(event):
        # HA fires this itself, synchronously, before dispatching to the
        # handler -- listening on it is the non-invasive way to observe
        # "the switch call happened", without monkeypatching a read-only
        # attribute on ServiceRegistry.
        if event.data.get("domain") == "switch":
            order.append("service_call")

    hass.bus.async_listen(EVENT_CALL_SERVICE, _on_call_service)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE_TYPE: SOURCE_TYPE_PSACC,
            CONF_PSACC_URL: "http://psacc.example",
            CONF_VIN: "VF1TESTVIN",
            CONF_PLUG_SWITCH: "switch.plug",
            CONF_PLUG_POWER_SENSOR: "sensor.plug_power",
        },
    )
    entry.add_to_hass(hass)
    # Frozen in-window, same reasoning as the context-sharing test above:
    # the ordering assertion needs an actuation to actually happen.
    midnight = dt_util.now().replace(hour=23, minute=30, second=0, microsecond=0)
    with patch("homeassistant.util.dt.now", return_value=midnight):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert order == ["event", "service_call"]


async def test_actuation_does_not_trigger_a_push_notification(hass, aioclient_mock):
    """The actuation event is an audit trail, not a reportable condition --
    it must not go through the push path even with targets configured, and
    it must not respect CONF_MUTED_EVENTS (an audit trail that can be
    muted is not an audit trail)."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    from ev_plug_charging.const import (
        CONF_NOTIFY_TARGETS,
        CONF_PLUG_POWER_SENSOR,
        CONF_PLUG_SWITCH,
        CONF_PSACC_URL,
        CONF_SOURCE_TYPE,
        CONF_VIN,
        DOMAIN,
        SOURCE_TYPE_PSACC,
    )

    hass.states.async_set("switch.plug", "off")
    hass.states.async_set("sensor.plug_power", "0")
    aioclient_mock.get(
        "http://psacc.example/get_vehicleinfo/VF1TESTVIN?from_cache=1",
        json=VEHICLE_INFO_RESPONSE,
    )
    async_mock_service(hass, "switch", "turn_on")
    notify_calls = async_mock_service(hass, "notify", "phone")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SOURCE_TYPE: SOURCE_TYPE_PSACC,
            CONF_PSACC_URL: "http://psacc.example",
            CONF_VIN: "VF1TESTVIN",
            CONF_PLUG_SWITCH: "switch.plug",
            CONF_PLUG_POWER_SENSOR: "sensor.plug_power",
        },
        options={CONF_NOTIFY_TARGETS: ["notify.phone"]},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # The first tick's only reportable condition is the actuation itself
    # (below_target, no separate charge_started push on this exact tick's
    # data) -- the point is that whatever DID fire, it did not reach notify
    # via the actuation path.
    assert notify_calls == []


def test_logbook_is_not_an_entity_platform():
    """"logbook" must never appear in const.PLATFORMS: that list is
    forwarded to async_forward_entry_setups as ENTITY platforms, and
    logbook is a separate integration-platform hook (discovered via
    manifest.json's after_dependencies). Adding it there would make HA
    try to set up a logbook ENTITY platform and fail."""
    from ev_plug_charging.const import PLATFORMS

    assert "logbook" not in PLATFORMS


def test_logbook_platform_describes_the_actuation_event():
    """Unit-tests the describer in isolation: register it, then invoke it
    with a stub exposing only `.data`, matching what the real
    LazyEventPartialState the logbook hands it looks like from this
    module's point of view."""
    from dataclasses import dataclass
    from typing import Any

    from ev_plug_charging.const import DOMAIN, EVENT_PLUG_COMMANDED
    from ev_plug_charging.logbook import async_describe_events

    registered: dict[str, tuple[str, Any]] = {}

    def _collect(domain, event_type, describe_callback):
        registered[event_type] = (domain, describe_callback)

    async_describe_events(hass=None, async_describe_event=_collect)

    assert EVENT_PLUG_COMMANDED in registered
    described_domain, describe = registered[EVENT_PLUG_COMMANDED]
    assert described_domain == DOMAIN

    @dataclass
    class _StubEvent:
        data: dict

    result = describe(_StubEvent(data={"action": "on", "reason": "below_target"}))
    assert result["name"] == "EV Plug Charging"
    assert "turned the plug on" in result["message"]
    assert "below_target" in result["message"]

    result_off = describe(_StubEvent(data={"action": "off", "reason": "target_reached"}))
    assert "turned the plug off" in result_off["message"]
