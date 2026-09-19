"""soc_stale, bypassed, charging_active, overheating, in_charge_window
(display only, D10 -- the actual gating uses logic.in_window() at decision
time, never this entity), source_reachable, completion_notified (the
public mirror of the YAML's input_boolean.ev_charge_complete_notified --
see MIGRATION.md)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

from . import logic as logic_mod
from .const import DOMAIN
from .coordinator import EvPlugChargingCoordinator
from .entity import EvPlugChargingEntity
from .models import ChargeSource


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EvPlugChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list = [
        SocStaleBinarySensor(coordinator, entry),
        BypassedBinarySensor(coordinator, entry),
        ChargingActiveBinarySensor(coordinator, entry),
        PlugDeliveringPowerBinarySensor(coordinator, entry),
        OverheatingBinarySensor(coordinator, entry),
        InWindowBinarySensor(coordinator, entry),
        SourceReachableBinarySensor(coordinator, entry),
        CompletionNotifiedBinarySensor(coordinator, entry),
    ]
    source = coordinator.source
    if source is not None and source.supports_aux_battery:
        entities.append(AuxBatteryLowBinarySensor(coordinator, entry))
    async_add_entities(entities)


class _BaseBinarySensor(EvPlugChargingEntity, BinarySensorEntity):
    @property
    def _decision(self):
        return (self.coordinator.data or {}).get("decision")

    @property
    def _state_obj(self):
        return (self.coordinator.data or {}).get("state")

    @property
    def _telemetry(self):
        return (self.coordinator.data or {}).get("telemetry")


class SocStaleBinarySensor(_BaseBinarySensor):
    _attr_icon = "mdi:timer-alert-outline"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "soc_stale", "SoC stale")

    @property
    def is_on(self):
        d = self._decision
        return d.soc_stale if d else False


class BypassedBinarySensor(_BaseBinarySensor):
    _attr_icon = "mdi:transmission-tower-export"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "bypassed", "Charging bypassed")

    @property
    def is_on(self):
        d = self._decision
        return d is not None and d.charge_source == ChargeSource.BYPASS


class ChargingActiveBinarySensor(_BaseBinarySensor):
    _attr_icon = "mdi:car-electric"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "charging_active", "Charging active")

    @property
    def is_on(self):
        d = self._decision
        return d.charging_active if d else False


class PlugDeliveringPowerBinarySensor(_BaseBinarySensor):
    """Just the plug's own contribution to charging_active -- useful on
    its own when diagnosing an EVSE that's on but drawing nothing,
    without needing to also check whether the car agrees it's charging."""

    _attr_icon = "mdi:power-plug"
    _attr_device_class = BinarySensorDeviceClass.POWER
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "plug_delivering_power", "Plug delivering power")

    @property
    def is_on(self):
        d = self._decision
        return d.plug_delivering if d else False


class OverheatingBinarySensor(_BaseBinarySensor):
    _attr_icon = "mdi:fire-alert"
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "overheating", "Plug overheating")

    @property
    def is_on(self):
        s = self._state_obj
        return s.overheat_latched if s else False


class InWindowBinarySensor(_BaseBinarySensor):
    """DISPLAY ONLY -- mirrors packages/ev_charging.yaml's own warning
    (D10): the actual gating always calls logic.in_window() fresh at
    decision time, never reads this entity."""

    _attr_icon = "mdi:clock-check"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "in_charge_window", "In charge window")

    @property
    def is_on(self):
        settings = self.coordinator.settings
        return logic_mod.in_window(dt_util.utcnow(), settings.window_start, settings.window_end)


class SourceReachableBinarySensor(_BaseBinarySensor):
    _attr_icon = "mdi:cloud-check"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "source_reachable", "Source reachable")

    @property
    def is_on(self):
        t = self._telemetry
        return t.source_reachable if t else False


class CompletionNotifiedBinarySensor(_BaseBinarySensor):
    """Public mirror of input_boolean.ev_charge_complete_notified, for a
    ported packages/opel.yaml's opel_charge_complete automation to read
    directly -- see MIGRATION.md. Read-only; the
    mark_completion_notified service is how it gets SET externally."""

    _attr_icon = "mdi:check-circle-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "completion_notified", "Completion notified")

    @property
    def is_on(self):
        s = self._state_obj
        return s.complete_notified if s else False


class AuxBatteryLowBinarySensor(_BaseBinarySensor):
    """The 7-day trend, held for 6h -- see aux_battery.advance_aux_battery.
    Distinct from the critical alert (raised via Repairs directly at the
    moment it fires; there is no separate "critical" binary_sensor because
    aux_battery_health already reports "low" instantly, and a binary
    sensor gains nothing by duplicating that with a different threshold)."""

    _attr_icon = "mdi:car-battery"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "aux_battery_low", "Aux battery low")

    @property
    def is_on(self):
        s = self._state_obj
        return s.aux_battery_low_notified if s else False
