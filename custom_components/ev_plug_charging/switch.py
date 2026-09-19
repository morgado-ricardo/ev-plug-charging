"""Three switches:

- `enabled` is the master. When off, the coordinator still polls, projects
  and reports, but never actuates the plug (logic.reduce() step 2).
  Observation is explicitly what the disabled state still does.
- `overheat_protection` gates the plug-temperature cutoff.
- `daily_wakeup` is off by default, and only created at all when the
  configured source can actually be asked for a refresh
  (source.supports_refresh) -- see logic._daily_wakeup_due.
"""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import EvPlugChargingCoordinator
from .entity import EvPlugChargingEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EvPlugChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list = [
        EnabledSwitch(coordinator, entry),
        OverheatProtectionSwitch(coordinator, entry),
    ]
    source = coordinator.source
    if source is not None and source.supports_refresh:
        entities.append(DailyWakeupSwitch(coordinator, entry))
    async_add_entities(entities)


class _SettingSwitch(EvPlugChargingEntity, RestoreEntity, SwitchEntity):
    _setting_name: str = ""
    _default_on = True

    @property
    def is_on(self) -> bool:
        return getattr(self.coordinator.settings, self._setting_name)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            setattr(self.coordinator.settings, self._setting_name, last.state == "on")

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.request_settings_update(**{self._setting_name: True})
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.request_settings_update(**{self._setting_name: False})
        self.async_write_ha_state()


class EnabledSwitch(_SettingSwitch):
    _attr_icon = "mdi:power"
    _setting_name = "enabled"

    def __init__(self, coordinator: EvPlugChargingCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "enabled", "Enabled")


class OverheatProtectionSwitch(_SettingSwitch):
    _attr_icon = "mdi:fire-alert"
    _attr_entity_category = EntityCategory.CONFIG
    _setting_name = "overheat_protection"

    def __init__(self, coordinator: EvPlugChargingCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "overheat_protection", "Overheat protection")


class DailyWakeupSwitch(_SettingSwitch):
    """Off by default (const.DEFAULT_DAILY_WAKEUP_ENABLED) -- a second,
    independent 12V budget line the user opts into, separate from the
    rescue refresh's own `rescue_refresh_enabled` option."""

    _attr_icon = "mdi:alarm"
    _attr_entity_category = EntityCategory.CONFIG
    _setting_name = "daily_wakeup_enabled"
    _default_on = False

    def __init__(self, coordinator: EvPlugChargingCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "daily_wakeup", "Daily wakeup")
