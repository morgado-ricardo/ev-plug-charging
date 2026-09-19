"""Base entity: device_info + has_entity_name, so every entity ID derives
from the device name and the entity's own name rather than being spelled
out one at a time. Renaming the device renames all of them together."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_VIN, DOMAIN
from .coordinator import EvPlugChargingCoordinator


class EvPlugChargingEntity(CoordinatorEntity[EvPlugChargingCoordinator]):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EvPlugChargingCoordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="EV Plug Charging",
            model="PSA Car Controller source" if entry.data.get(CONF_VIN) else "Generic",
        )
