"""Integration-level tests using pytest-homeassistant-custom-component: a
real (test) Home Assistant instance, real entity platforms, a mocked PSACC
HTTP endpoint. This is what proves the HA-facing layer (coordinator,
config_flow, entity platforms) actually wires together, as opposed to the
pure-logic suite's proof that the DECISIONS are correct.

Requires pytest-homeassistant-custom-component; skipped automatically if
it is not installed (it pulls in a large, sometimes hard-to-build
dependency tree -- see EXTRACTING.md/README.md for how to install it).
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

from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

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
        CONF_CHARGE_EFFICIENCY,
        CONF_CHARGE_POWER_KW,
        CONF_CHARGING_STATE_STRING,
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
            CONF_CHARGING_STATE_STRING: "InProgress",
            CONF_PLUG_SWITCH: plug_switch,
            CONF_PLUG_POWER_SENSOR: plug_power,
            CONF_BATTERY_CAPACITY_KWH: 50.0,
            CONF_CHARGE_POWER_KW: 1.84,
            CONF_CHARGE_EFFICIENCY: 0.82,
            CONF_TEMP_LIMIT: 65.0,
            CONF_POLL_INTERVAL: 120,
        },
    )
    entry.add_to_hass(hass)
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
    from ev_plug_charging.const import DOMAIN

    entry, _ = await _setup_entry(hass, aioclient_mock)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert aioclient_mock.call_count >= 1
    assert coordinator.last_decision is not None
    # Vehicle reports Disconnected/not plugged, plug is off -> nothing to do.
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
            "charging_state_string": "InProgress",
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

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SOURCE_TYPE] == SOURCE_TYPE_PSACC
    assert result["data"][CONF_PSACC_URL] == "http://psacc.example"


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
            "charging_state_string": "InProgress",
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "psacc"
    assert result["errors"]["base"] == "invalid_response"
