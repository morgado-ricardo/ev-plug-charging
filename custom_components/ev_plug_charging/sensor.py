"""Read-only sensors. `projected_soc` is the interesting one: it shows the
number the stop is actually gated on. logic.reduce() recomputes that value
from the persisted rate and anchor every tick -- it never reads this entity
back, so nothing here can influence a decision."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EvPlugChargingCoordinator
from .entity import EvPlugChargingEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EvPlugChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list = [
        SocSensor(coordinator, entry),
        ProjectedSocSensor(coordinator, entry),
        ChargeNeededSensor(coordinator, entry),
        EstimatedChargeTimeSensor(coordinator, entry),
        SilenceMinutesSensor(coordinator, entry),
        MinutesPerPercentSensor(coordinator, entry),
        ChargeStartedAtSensor(coordinator, entry),
        ChargeSourceSensor(coordinator, entry),
        LastChargeSourceSensor(coordinator, entry),
        DaysSinceChargeSensor(coordinator, entry),
        SessionEnergySensor(coordinator, entry),
        MonthlyEnergySensor(coordinator, entry),
        TotalEnergySensor(coordinator, entry),
    ]
    source = coordinator.source
    if source is not None and source.supports_aux_battery:
        entities.extend(
            [
                AuxBatterySensor(coordinator, entry),
                AuxBatteryRestingSensor(coordinator, entry),
                AuxBattery7dSensor(coordinator, entry),
                AuxBatteryHealthSensor(coordinator, entry),
            ]
        )
    async_add_entities(entities)


class _BaseSensor(EvPlugChargingEntity, SensorEntity):
    def __init__(self, coordinator, entry, key, name):
        super().__init__(coordinator, entry, key, name)

    @property
    def _telemetry(self):
        return (self.coordinator.data or {}).get("telemetry")

    @property
    def _decision(self):
        return (self.coordinator.data or {}).get("decision")

    @property
    def _state_obj(self):
        return (self.coordinator.data or {}).get("state")


class SocSensor(_BaseSensor):
    _attr_icon = "mdi:battery"
    _attr_native_unit_of_measurement = "%"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "soc", "Battery SoC")

    @property
    def native_value(self):
        t = self._telemetry
        return t.soc if t else None


class ProjectedSocSensor(_BaseSensor):
    """Displays what logic.reduce() just computed. The next reduce() call
    recomputes it fresh from persisted state and does NOT read this entity
    back -- a decision must never depend on a display entity's cached
    value."""

    _attr_icon = "mdi:battery-clock-outline"
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "projected_soc", "Projected SoC")

    @property
    def native_value(self):
        d = self._decision
        return d.projected_soc if d else None

    @property
    def extra_state_attributes(self):
        d = self._decision
        if not d:
            return {}
        return {"decision_reason": d.reason, "charge_source": d.charge_source.value}


class ChargeNeededSensor(_BaseSensor):
    _attr_icon = "mdi:battery-plus-variant"
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "charge_needed", "Charge needed")

    @property
    def native_value(self):
        t = self._telemetry
        if not t or t.soc is None:
            return None
        return max(0.0, self.coordinator.settings.target_soc - t.soc)


class EstimatedChargeTimeSensor(_BaseSensor):
    """Display only -- the charge stops on projected_soc, never on this
    countdown, matching sensor.ev_estimated_charge_time's own note."""

    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = "h"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "estimated_charge_time", "Estimated charge time")

    @property
    def native_value(self):
        t = self._telemetry
        if not t or t.soc is None:
            return None
        needed = max(0.0, self.coordinator.settings.target_soc - t.soc)
        rate = self.coordinator._effective_rate().minutes_per_percent  # noqa: SLF001
        return round(needed * rate / 60, 1)


class SilenceMinutesSensor(_BaseSensor):
    _attr_icon = "mdi:timer-alert-outline"
    _attr_native_unit_of_measurement = "min"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "soc_silence_minutes", "SoC silence")

    @property
    def native_value(self):
        d = self._decision
        return round(d.silence_minutes, 1) if d else None


class MinutesPerPercentSensor(_BaseSensor):
    _attr_icon = "mdi:speedometer"
    _attr_native_unit_of_measurement = "min"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "minutes_per_percent", "Minutes per percent")

    @property
    def native_value(self):
        return round(self.coordinator._effective_rate().minutes_per_percent, 2)  # noqa: SLF001

    @property
    def extra_state_attributes(self):
        s = self._state_obj
        if not s:
            return {}
        return {"sample_count": len(s.rate_samples)}


class ChargeStartedAtSensor(_BaseSensor):
    _attr_icon = "mdi:play-circle-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "charge_started_at", "Charge started at")

    @property
    def native_value(self) -> datetime | None:
        s = self._state_obj
        return s.charge_started_at if s else None


class ChargeSourceSensor(_BaseSensor):
    _attr_icon = "mdi:power-plug"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "charge_source", "Charge source")

    @property
    def native_value(self):
        d = self._decision
        return d.charge_source.value if d else None


class LastChargeSourceSensor(_BaseSensor):
    """The sticky companion to ChargeSourceSensor: how the session that
    just ended was actually powered, still correct once the live value has
    fallen back to "none". Needed because a car-confirmed completion
    (logic.reduce()'s car_finished_edge) can arrive well after the live
    charge_source has already gone quiet, and a notification that says
    "charged via none" is useless."""

    _attr_icon = "mdi:history"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "last_charge_source", "Last charge source")

    @property
    def native_value(self):
        s = self._state_obj
        return s.last_charge_source.value if s else None


class SessionEnergySensor(_BaseSensor):
    _attr_icon = "mdi:lightning-bolt"
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "session_energy", "Session energy")

    @property
    def native_value(self):
        s = self._state_obj
        return round(s.session_energy_kwh, 3) if s else None


class MonthlyEnergySensor(_BaseSensor):
    _attr_icon = "mdi:lightning-bolt"
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "monthly_energy", "Monthly energy")

    @property
    def native_value(self):
        return round(self.coordinator.monthly_energy_kwh, 3)


class TotalEnergySensor(_BaseSensor):
    _attr_icon = "mdi:lightning-bolt"
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "total_energy", "Lifetime energy")

    @property
    def native_value(self):
        return round(self.coordinator.lifetime_energy_kwh, 3)


class DaysSinceChargeSensor(_BaseSensor):
    """Free: the integration already knows when its own sessions end
    (SessionState.charge_completed_at, set at logic._mark_complete's one
    choke point)."""

    _attr_icon = "mdi:ev-station"
    _attr_native_unit_of_measurement = "d"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "days_since_charge", "Days since charge")

    @property
    def native_value(self):
        s = self._state_obj
        if not s or s.charge_completed_at is None:
            return None
        return round((dt_util.now() - s.charge_completed_at).total_seconds() / 86400, 1)


class _BaseAuxBatterySensor(EvPlugChargingEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def _aux(self):
        return (self.coordinator.data or {}).get("aux_battery")

    @property
    def _state_obj(self):
        return (self.coordinator.data or {}).get("state")


class AuxBatterySensor(_BaseAuxBatterySensor):
    """The raw 12V reading -- PSACC's `battery.voltage` field, which is
    NOT a voltage despite the name (see sources/psacc.py). Rides on the
    same cached payload every routine poll already fetches."""

    _attr_icon = "mdi:car-battery"
    _attr_native_unit_of_measurement = "%"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "aux_battery", "Aux battery")

    @property
    def native_value(self):
        a = self._aux
        return a.current if a else None


class AuxBatteryRestingSensor(_BaseAuxBatterySensor):
    """Only a value while the car is at rest -- charging and driving both
    inflate the raw reading, so the recorder should only see comparable
    samples. A history graph mixing resting and charging readings tells
    you nothing about the battery's actual condition."""

    _attr_icon = "mdi:car-battery"
    _attr_native_unit_of_measurement = "%"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "aux_battery_resting", "Aux battery resting")

    @property
    def native_value(self):
        a = self._aux
        return a.resting if a else None


class AuxBattery7dSensor(_BaseAuxBatterySensor):
    """Rolling mean of resting samples, one per day -- the figure the
    health band and the low-battery alert are actually computed from,
    because the raw reading is too noisy tick to tick to gate anything
    on directly."""

    _attr_icon = "mdi:car-battery"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "aux_battery_7d", "Aux battery 7d")

    @property
    def native_value(self):
        a = self._aux
        return round(a.avg_7d, 1) if a and a.avg_7d is not None else None

    @property
    def extra_state_attributes(self):
        s = self._state_obj
        if not s:
            return {}
        return {"sample_count": len(s.aux_battery_samples)}


class AuxBatteryHealthSensor(_BaseAuxBatterySensor):
    """healthy / watch / low / unknown. The 7d-vs-30d drift lives here as
    an attribute rather than its own entity: it is the most informative
    figure this tracker produces ("nightly charging is slowly losing
    ground even though the level still looks fine"), but it does not need
    top-level visibility to be useful, and the 30-day sample buffer it
    needs already exists for the health band itself."""

    _attr_icon = "mdi:car-battery"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "aux_battery_health", "Aux battery health")

    @property
    def native_value(self):
        a = self._aux
        return a.health if a else None

    @property
    def extra_state_attributes(self):
        a = self._aux
        if not a:
            return {}
        return {
            "avg_7d": a.avg_7d,
            "avg_30d": a.avg_30d,
            "drift": round(a.drift, 1) if a.drift is not None else None,
        }
