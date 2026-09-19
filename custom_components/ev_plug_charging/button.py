"""refresh_source -- the manual equivalent of the automatic rescue wakeup,
and reset_rate_learning -- clears the learned-rate samples after a
capacity or vehicle change makes them wrong."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityCategory

from . import rate_model
from .sources import SourceConnectionError, SourceResponseError
from .const import DOMAIN
from .coordinator import EvPlugChargingCoordinator
from .entity import EvPlugChargingEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EvPlugChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            RefreshSourceButton(coordinator, entry),
            ResetRateLearningButton(coordinator, entry),
        ]
    )


class RefreshSourceButton(EvPlugChargingEntity, ButtonEntity):
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EvPlugChargingCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "refresh_source", "Refresh source")

    async def async_press(self) -> None:
        source = self.coordinator.source
        if source is not None and source.supports_refresh:
            try:
                await source.async_request_refresh()
            except (SourceConnectionError, SourceResponseError):
                pass
        await self.coordinator.async_request_refresh()


class ResetRateLearningButton(EvPlugChargingEntity, ButtonEntity):
    _attr_icon = "mdi:speedometer-slow"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EvPlugChargingCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "reset_rate_learning", "Reset rate learning")

    async def async_press(self) -> None:
        self.coordinator._session_state = rate_model.reset_samples(  # noqa: SLF001
            self.coordinator._session_state  # noqa: SLF001
        )
        await self.coordinator.async_request_refresh()
