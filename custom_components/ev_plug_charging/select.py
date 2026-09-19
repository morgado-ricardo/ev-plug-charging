"""input_select.ev_charge_mode, minus "Off (manual)" (decision 4) -- that
role is now the `enabled` switch (see switch.py)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import EvPlugChargingCoordinator
from .entity import EvPlugChargingEntity
from .models import ChargeMode

_OPTIONS = ["smart", "timed"]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EvPlugChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ChargeModeSelect(coordinator, entry)])


class ChargeModeSelect(EvPlugChargingEntity, RestoreEntity, SelectEntity):
    _attr_icon = "mdi:ev-station"
    _attr_options = _OPTIONS

    def __init__(self, coordinator: EvPlugChargingCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "charge_mode", "Charge mode")

    @property
    def current_option(self) -> str:
        return self.coordinator.settings.mode.value

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in _OPTIONS:
            self.coordinator.settings.mode = ChargeMode(last.state)

    async def async_select_option(self, option: str) -> None:
        self.coordinator.request_settings_update(mode=ChargeMode(option))
        self.async_write_ha_state()
