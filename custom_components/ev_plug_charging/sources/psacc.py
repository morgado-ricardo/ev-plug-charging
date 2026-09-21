"""PSA Car Controller (PSACC) as a telemetry source.

PSACC (https://github.com/flobz/psa_car_controller) is a self-hosted bridge
to the Stellantis vehicle cloud. It is ONE source among several this
integration could speak to -- everything Stellantis-specific is confined to
this file.

Two calls, both GET, no body, no auth (matching the local-LAN deployments
this was built against):

- vehicle info: ALWAYS requests ?from_cache=1. This is load-bearing.
  Without it PSACC treats the request as a live poll and may wake the
  vehicle over its cellular link to refresh the cache -- exactly the
  12V-draining behaviour this integration exists to avoid. Reading and
  refreshing being *separate acts* is the thing PSACC is unusually good
  at; most vehicle APIs conflate them.
- wakeup: the one deliberate exception, a real remote wakeup, spent at
  most once per session and only when the coordinator's three gates all
  pass.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover
    import aiohttp

from ..const import (
    CONF_PSACC_URL,
    CONF_VIN,
    SOURCE_TYPE_PSACC,
    VEHICLE_COMMAND_CHARGE_START,
    VEHICLE_COMMAND_CHARGE_STOP,
    VEHICLE_COMMAND_FLASH_LIGHTS,
    VEHICLE_COMMAND_HORN,
    VEHICLE_COMMAND_LOCK,
    VEHICLE_COMMAND_PRECONDITION_START,
    VEHICLE_COMMAND_PRECONDITION_STOP,
    VEHICLE_COMMAND_UNLOCK,
    VEHICLE_COMMAND_WAKE,
)
from ..source import TelemetrySnapshot, derive_freshness
from .base import (
    SourceConnectionError,
    SourceError,
    SourceNoVehicleData,
    SourceResponseError,
    TelemetrySource,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30

# Stellantis' own Connected Car API defines charging.status as a closed,
# Swagger-generated enum with exactly five values: Disconnected,
# InProgress, Failure, Stopped, Finished --
# https://github.com/flobz/psa_car_controller/blob/master/psa_car_controller/psa/connected_car_api/models/charging_status_enum.py
# PSACC passes this value straight through from energy[].charging.status
# without translating it, so it reads identically for every Stellantis-group
# brand (Peugeot/Citroën/DS/Opel/Vauxhall/Fiat) and every deployment -- not
# a per-installation setting, unlike the URL or VIN. These two used to be
# config-flow fields on the assumption that they might vary; they don't.
CHARGING_STATUS_IN_PROGRESS = "InProgress"
# A SEPARATE terminal signal from CHARGING_STATUS_IN_PROGRESS above, not
# its logical opposite -- the enum also has Disconnected/Failure/Stopped,
# neither "in progress" nor "finished". This is the only completion path
# that works no matter how the car was charged (plug, EVSE straight into
# the wall, or a public charger with no telemetry of its own).
CHARGING_STATUS_FINISHED = "Finished"

# Candidate field names for a per-reading timestamp inside energy[0].
# Probed in order; the first present and parseable wins. None of these are
# confirmed against a live PSACC instance, so the value-change fallback in
# source.derive_freshness() is what normally runs -- `used_payload_timestamp`
# on the snapshot reports which path is live.
_PAYLOAD_TIMESTAMP_KEYS = ("updated_at", "timestamp", "last_update")

# Every path below is a real PSACC endpoint. They are exposed through the
# vehicle_command service rather than as entities: the transport (host,
# VIN, timeout, error handling) already lives here, so declaring it a
# second time somewhere else means owning it twice.
#
# Note charge_now/1 forces an immediate charge, overriding PSACC's own
# deferred schedule; charge_now/0 does NOT stop charging outright, it
# reverts to that schedule, which may still be mid-charge if its own time
# has passed.
def _vehicle_command_path(command: str, vin: str, params: Optional[dict[str, Any]]) -> str:
    """Pure: (command, vin, params) -> the URL path to call. Split out from
    async_vehicle_command so the mapping is unit-testable without a
    session or an event loop -- the same reason _vehicle_info_url() and
    parse_vehicle_info() are free functions/methods rather than inlined
    into async_fetch()."""
    params = params or {}
    if command == VEHICLE_COMMAND_WAKE:
        return f"/wakeup/{vin}"
    if command == VEHICLE_COMMAND_LOCK:
        return f"/lock_door/{vin}/1"
    if command == VEHICLE_COMMAND_UNLOCK:
        return f"/lock_door/{vin}/0"
    if command == VEHICLE_COMMAND_HORN:
        return f"/horn/{vin}/{params.get('count', 2)}"
    if command == VEHICLE_COMMAND_FLASH_LIGHTS:
        return f"/lights/{vin}/{params.get('duration', 10)}"
    if command == VEHICLE_COMMAND_PRECONDITION_START:
        return f"/preconditioning/{vin}/1"
    if command == VEHICLE_COMMAND_PRECONDITION_STOP:
        return f"/preconditioning/{vin}/0"
    if command == VEHICLE_COMMAND_CHARGE_START:
        return f"/charge_now/{vin}/1"
    if command == VEHICLE_COMMAND_CHARGE_STOP:
        return f"/charge_now/{vin}/0"
    raise SourceError(f"PSACC does not support vehicle command {command!r}")


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def parse_vehicle_info(
    raw: dict[str, Any],
    now: datetime,
    prev: Optional[TelemetrySnapshot],
) -> TelemetrySnapshot:
    """PSACC's get_vehicleinfo JSON -> TelemetrySnapshot. Pure: same inputs,
    same output, no I/O, no HA. Unit-tested directly."""
    # battery.voltage (really the 12V's own SoC%, see below) lives at the
    # payload's top level, a sibling of energy[] rather than nested inside
    # it -- parsed here, before the energy[0]-missing early return, so a
    # temporarily missing energy block doesn't also blind aux-battery
    # tracking for no reason.
    battery_block = raw.get("battery") or {}
    raw_aux = battery_block.get("voltage")
    try:
        aux_battery_soc = float(raw_aux) if raw_aux is not None else None
    except (TypeError, ValueError):
        aux_battery_soc = None

    energy_list = raw.get("energy")
    if not isinstance(energy_list, list) or not energy_list:
        _LOGGER.debug("PSACC payload missing energy[0]; treating as no reading")
        return TelemetrySnapshot(
            soc=None,
            soc_changed_at=prev.soc_changed_at if prev else None,
            charging_status=None,
            car_charging=False,
            plugged=None,
            polled_at=now,
            source_reachable=True,
            aux_battery_soc=aux_battery_soc,
        )

    energy = energy_list[0] or {}
    charging = energy.get("charging") or {}

    raw_level = energy.get("level")
    try:
        soc = float(raw_level) if raw_level is not None else None
    except (TypeError, ValueError):
        soc = None

    charging_status = charging.get("status")
    plugged_raw = charging.get("plugged")
    plugged = bool(plugged_raw) if plugged_raw is not None else None
    # A SEPARATE terminal-status match, not "not InProgress" -- the enum's
    # other values (Disconnected, Failure, Stopped) are neither "in
    # progress" nor "finished".
    car_charge_finished = charging_status == CHARGING_STATUS_FINISHED

    payload_ts: Optional[datetime] = None
    for key in _PAYLOAD_TIMESTAMP_KEYS:
        payload_ts = _parse_timestamp(energy.get(key))
        if payload_ts is not None:
            break

    soc_changed_at, used_payload_timestamp = derive_freshness(prev, soc, payload_ts, now)

    return TelemetrySnapshot(
        soc=soc,
        soc_changed_at=soc_changed_at,
        charging_status=charging_status,
        car_charging=charging_status == CHARGING_STATUS_IN_PROGRESS,
        plugged=plugged,
        polled_at=now,
        source_reachable=True,
        used_payload_timestamp=used_payload_timestamp,
        car_charge_finished=car_charge_finished,
        aux_battery_soc=aux_battery_soc,
    )


class PsaccSource(TelemetrySource):
    source_type = SOURCE_TYPE_PSACC
    supports_refresh = True
    supports_aux_battery = True
    supported_commands = frozenset(
        {
            VEHICLE_COMMAND_WAKE,
            VEHICLE_COMMAND_LOCK,
            VEHICLE_COMMAND_UNLOCK,
            VEHICLE_COMMAND_HORN,
            VEHICLE_COMMAND_FLASH_LIGHTS,
            VEHICLE_COMMAND_PRECONDITION_START,
            VEHICLE_COMMAND_PRECONDITION_STOP,
            VEHICLE_COMMAND_CHARGE_START,
            VEHICLE_COMMAND_CHARGE_STOP,
        }
    )

    def __init__(
        self,
        session: "aiohttp.ClientSession",
        base_url: str,
        vin: str,
    ) -> None:
        self._session = session
        self._base_url = base_url
        self._vin = vin

    @classmethod
    def from_config(cls, session: "aiohttp.ClientSession", config: dict[str, Any]) -> "PsaccSource":
        return cls(
            session=session,
            base_url=config[CONF_PSACC_URL],
            vin=config[CONF_VIN],
        )

    # -- TelemetrySource ---------------------------------------------------

    async def async_fetch(
        self, now: datetime, prev: Optional[TelemetrySnapshot]
    ) -> TelemetrySnapshot:
        raw = await self._get_json(self._vehicle_info_url())
        return parse_vehicle_info(raw, now, prev)

    async def async_request_refresh(self) -> bool:
        await self._get_json(self._url(f"/wakeup/{self._vin}"), allow_empty=True)
        return True

    async def async_vehicle_command(
        self, command: str, params: Optional[dict[str, Any]] = None
    ) -> Any:
        path = _vehicle_command_path(command, self._vin, params)
        await self._get_json(self._url(path), allow_empty=True)
        return None

    @staticmethod
    async def async_validate(hass: Any, user_input: dict[str, Any]) -> dict[str, Any]:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        source = PsaccSource.from_config(async_get_clientsession(hass), user_input)
        raw = await source._get_json(source._vehicle_info_url())  # noqa: SLF001
        energy = raw.get("energy")
        if not isinstance(energy, list) or not energy:
            raise SourceNoVehicleData(
                "PSACC answered, but has no energy data for this vehicle ID"
            )
        first = energy[0] or {}
        return {
            "soc": first.get("level"),
            "charging_status": (first.get("charging") or {}).get("status"),
        }

    def diagnostics(self) -> dict[str, Any]:
        # Deliberately no URL and no VIN -- both identify the vehicle/owner.
        return {
            "source_type": self.source_type,
            "supported_commands": sorted(self.supported_commands),
        }

    # -- transport ---------------------------------------------------------

    def _url(self, path: str, **query: Any) -> str:
        url = f"{self._base_url.rstrip('/')}{path}"
        if query:
            url = f"{url}?" + "&".join(f"{k}={v}" for k, v in query.items())
        return url

    def _vehicle_info_url(self) -> str:
        # from_cache=1 is never omitted -- see the module docstring.
        return self._url(f"/get_vehicleinfo/{self._vin}", **{"from_cache": 1})

    async def _get_json(self, url: str, allow_empty: bool = False) -> dict[str, Any]:
        # Imported here, not at module scope, so that parse_vehicle_info()
        # and the source registry stay importable with no third-party
        # dependency at all -- the same discipline __init__.py uses to keep
        # the pure decision modules testable without Home Assistant.
        import aiohttp

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with self._session.get(url) as resp:
                    if resp.status >= 400:
                        raise SourceResponseError(
                            f"PSACC returned HTTP {resp.status}", status=resp.status
                        )
                    text = await resp.text()
                    if not text and allow_empty:
                        return {}
                    try:
                        return await resp.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError) as err:
                        if allow_empty:
                            return {}
                        raise SourceResponseError(
                            "PSACC returned a non-JSON body"
                        ) from err
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise SourceConnectionError("Timed out reaching PSACC") from err
        except aiohttp.ClientError as err:
            raise SourceConnectionError(f"Could not reach PSACC: {err}") from err
