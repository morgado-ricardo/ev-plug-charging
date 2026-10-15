"""Runtime-tunable numbers: target SoC, emergency min-SoC, expected
reporting gap, and the "delivering power" threshold -- plus "Current SoC",
the one number that is not a setting but a reading a person supplies (see
CurrentSocNumber).

All four are entities rather than config options because they are things
you change from a dashboard, not once at setup. The power threshold in
particular is a function of your EVSE's current draw, not a universal
constant, so it cannot sensibly be hard-coded."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import EvPlugChargingCoordinator
from .entity import EvPlugChargingEntity


@dataclass(frozen=True)
class _NumberSpec:
    key: str
    name: str
    icon: str
    unit: str
    min_value: float
    max_value: float
    step: float
    default: float


_SPECS = [
    _NumberSpec("target_soc", "Target SoC", "mdi:battery-charging-80", "%", 20, 100, 5, 80.0),
    _NumberSpec(
        "min_soc_override",
        "Minimum SoC for immediate charge",
        "mdi:battery-alert",
        "%",
        5,
        30,
        5,
        15.0,
    ),
    _NumberSpec(
        "expected_gap_minutes",
        "Expected minutes per 1% SoC report",
        "mdi:timer-sand",
        "min",
        15,
        90,
        5,
        40.0,
    ),
    _NumberSpec(
        "power_threshold_w",
        "Delivering-power threshold",
        "mdi:flash",
        "W",
        10,
        500,
        10,
        50.0,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EvPlugChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    source = coordinator.source
    reports_live = source is None or source.reports_live
    specs = [
        spec
        for spec in _SPECS
        # The reporting gap describes a live feed; with none there is
        # nothing for it to tune.
        if reports_live or spec.key != "expected_gap_minutes"
    ]
    entities: list[NumberEntity] = [SettingNumber(coordinator, entry, spec) for spec in specs]
    entities.append(CurrentSocNumber(coordinator, entry))
    async_add_entities(entities)


class SettingNumber(EvPlugChargingEntity, RestoreEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: EvPlugChargingCoordinator, entry: ConfigEntry, spec: _NumberSpec
    ) -> None:
        super().__init__(coordinator, entry, spec.key, spec.name)
        self._spec = spec
        self._attr_icon = spec.icon
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_native_min_value = spec.min_value
        self._attr_native_max_value = spec.max_value
        self._attr_native_step = spec.step

    @property
    def native_value(self) -> float:
        return getattr(self.coordinator.settings, self._spec.key)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            try:
                setattr(self.coordinator.settings, self._spec.key, float(last.state))
            except ValueError:
                pass

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.request_settings_update(**{self._spec.key: value})
        self.async_write_ha_state()


class CurrentSocNumber(EvPlugChargingEntity, NumberEntity):
    """The state of charge, as a person reads it off the car.

    Setting it hands the value to the coordinator as a manual reading
    (coordinator.async_set_manual_soc); logic.effective_reading decides
    when it outranks telemetry and when one charge has spent it. Shows the
    value only while it IS the reading in force, unknown otherwise -- a
    number left showing last night's 50% would invite confirming it again.

    Deliberately not a RestoreEntity: the value it shows is derived, and
    the reading itself is persisted with the rest of SessionState.
    """

    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:battery-sync"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1

    def __init__(self, coordinator: EvPlugChargingCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "current_soc", "Current SoC")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.manual_soc

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_manual_soc(value)
        self.async_write_ha_state()
