"""Runtime-tunable numbers: target SoC, emergency min-SoC, expected
reporting gap, and the "delivering power" threshold -- the last one was a
hard-coded 50 W in the YAML (a function of EVSE current, not a universal
constant); making it a number entity is a deliberate improvement flagged
in the port plan section 8."""
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
    async_add_entities([SettingNumber(coordinator, entry, spec) for spec in _SPECS])


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
