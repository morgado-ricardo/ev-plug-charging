"""Charge window start/end (input_datetime.ev_charge_start_time /
_end_time, minus the has_date=false quirk -- these are plain `time`
entities), plus the daily-wakeup time -- only created when the configured
source can actually be asked for a refresh (source.supports_refresh)."""
from __future__ import annotations

from datetime import time as time_cls

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import EvPlugChargingCoordinator
from .entity import EvPlugChargingEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EvPlugChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list = [
        WindowTimeEntity(coordinator, entry, "window_start", "Window start"),
        WindowTimeEntity(coordinator, entry, "window_end", "Window end"),
    ]
    source = coordinator.source
    if source is not None and source.supports_refresh:
        entities.append(
            WindowTimeEntity(coordinator, entry, "daily_wakeup_time", "Daily wakeup time")
        )
    async_add_entities(entities)


class WindowTimeEntity(EvPlugChargingEntity, RestoreEntity, TimeEntity):
    _attr_icon = "mdi:clock-start"

    def __init__(
        self, coordinator: EvPlugChargingCoordinator, entry: ConfigEntry, key: str, name: str
    ) -> None:
        super().__init__(coordinator, entry, key, name)
        self._key = key

    @property
    def native_value(self) -> time_cls:
        return getattr(self.coordinator.settings, self._key)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            try:
                hh, mm, *_ = last.state.split(":")
                setattr(
                    self.coordinator.settings, self._key, time_cls(int(hh), int(mm))
                )
            except (ValueError, AttributeError):
                pass

    async def async_set_value(self, value: time_cls) -> None:
        self.coordinator.request_settings_update(**{self._key: value})
        self.async_write_ha_state()
