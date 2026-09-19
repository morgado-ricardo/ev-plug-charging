"""The contract every telemetry source implements.

The coordinator never knows which source it has. It asks for a
`TelemetrySnapshot` and, occasionally, for a refresh -- nothing more. That
is what keeps logic.py, session.py and rate_model.py free of any provider's
quirks, and what makes adding a second source a self-contained job.

Two errors, deliberately source-neutral: a source that cannot be reached
(`SourceConnectionError`) is a different thing from one that answered with
something unusable (`SourceResponseError`). The coordinator treats both the
same way -- an unreachable snapshot, which fails closed -- but
the config flow reports them differently, because "wrong address" and
"wrong vehicle" need different fixes from the user.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional

from ..source import TelemetrySnapshot


class SourceError(Exception):
    """Base class for every error a source raises."""


class SourceConnectionError(SourceError):
    """The source could not be reached at all (DNS, refused, timeout)."""


class SourceResponseError(SourceError):
    """The source answered, but not with something usable -- a bad HTTP
    status, unparseable body, or a payload missing the fields needed."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SourceNoVehicleData(SourceResponseError):
    """The source is reachable and healthy, but has nothing for the vehicle
    identified in the config -- almost always a wrong vehicle ID, which is
    worth telling the user distinctly during setup."""


class TelemetrySource(ABC):
    """One configured provider of state of charge.

    Instances are created by `sources.async_create_source()` from a config
    entry and live as long as the coordinator does.
    """

    #: Stable key stored in the config entry under CONF_SOURCE_TYPE.
    source_type: str = ""

    #: False for a source that has no way to ask for a fresh reading, in
    #: which case the coordinator never spends the one-per-session rescue
    #: refresh and the refresh button becomes a no-op.
    supports_refresh: bool = False

    #: False for a source that has no 12V auxiliary-battery signal at all,
    #: in which case aux_battery.py's four entities are simply not created
    #: (see binary_sensor.py/sensor.py's async_setup_entry).
    supports_aux_battery: bool = False

    #: The vehicle commands this source can execute (const.VEHICLE_COMMANDS
    #: is the full vocabulary). Empty by default -- a source with no
    #: command capability needs no code beyond this. Never touched by
    #: logic.py/session.py/coordinator.py: the only caller is
    #: services.py's `vehicle_command` service, which checks a command is
    #: in this set before ever calling async_vehicle_command below.
    supported_commands: frozenset[str] = frozenset()

    @classmethod
    @abstractmethod
    def from_config(cls, session: Any, config: dict[str, Any]) -> "TelemetrySource":
        """Build an instance from the merged config-entry data+options.

        `session` is an aiohttp ClientSession, passed in rather than created
        so Home Assistant owns its lifecycle. A source with no HTTP needs
        may simply ignore it.
        """

    @abstractmethod
    async def async_fetch(
        self, now: datetime, prev: Optional[TelemetrySnapshot]
    ) -> TelemetrySnapshot:
        """Read the current telemetry.

        Implementations must call `source.derive_freshness()` rather than
        setting `soc_changed_at` themselves, and must normalise their own
        "is it charging" representation into `car_charging`.

        Raises SourceConnectionError / SourceResponseError; the coordinator
        converts those into an unreachable snapshot.
        """

    async def async_request_refresh(self) -> bool:
        """Ask the source for a fresh reading, if it can.

        This is the one-per-session rescue attempt -- for a cloud-backed
        vehicle API it typically wakes the car and costs 12V charge, which
        is why the coordinator gates it so tightly. Returns True if a
        refresh was actually requested.

        The default is a no-op, so a source without this capability needs
        no code at all.
        """
        return False

    @staticmethod
    @abstractmethod
    async def async_validate(hass: Any, user_input: dict[str, Any]) -> dict[str, Any]:
        """Config-flow connection test. Returns a small dict of discovered
        facts to show the user (e.g. current SoC and status), or raises one
        of the errors above.
        """

    async def async_vehicle_command(
        self, command: str, params: Optional[dict[str, Any]] = None
    ) -> Any:
        """Execute a named vehicle command (const.VEHICLE_COMMANDS).

        Only ever called by services.py for a command already confirmed to
        be in `supported_commands` -- this default is effectively
        unreachable in practice, and exists only so declaring
        `supported_commands` doesn't also force overriding this method
        before implementing even one command. A source that overrides
        both is free to raise a more specific error; the default is
        source-neutral.
        """
        raise SourceError(f"{self.source_type} does not support vehicle command {command!r}")

    def diagnostics(self) -> dict[str, Any]:
        """Source-specific detail for the diagnostics download. Must not
        include credentials or anything identifying the vehicle."""
        return {"source_type": self.source_type}
