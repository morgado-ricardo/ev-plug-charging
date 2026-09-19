"""Dispatches logic.reduce()'s events: always onto the HA event bus, to the
configured notify service if one is set, and -- for persistent conditions
-- as a self-clearing Repairs issue.

This is the single chokepoint script.ev_notify_ricardo used to be
(packages/ev_charging.yaml:410-429), generalised: any notify.* service, not
one hard-coded group, plus events so a user can build their own automations
without needing a specific notify platform at all.
"""
from __future__ import annotations

import logging
from typing import Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_MUTED_EVENTS, CONF_NOTIFY_SERVICE, DOMAIN, PERSISTENT_EVENTS
from .models import Event

_LOGGER = logging.getLogger(__name__)

_TITLES = {
    f"{DOMAIN}_charge_started": "EV charging started",
    f"{DOMAIN}_charge_not_started": "EV - charge NOT started",
    f"{DOMAIN}_charge_complete": "EV charging complete",
    f"{DOMAIN}_window_shortfall": "EV charging stopped (window closed)",
    f"{DOMAIN}_soc_stale": "EV - SoC not updating",
    f"{DOMAIN}_refresh_attempted": "EV - requesting a fresh reading",
    f"{DOMAIN}_bypass_detected": "EV - charging direct from the wall",
    f"{DOMAIN}_overheat_cutoff": "EV - plug overheating, charging stopped",
    f"{DOMAIN}_evse_no_power": "EV - charging not detected",
    f"{DOMAIN}_soc_full": "EV battery full",
    f"{DOMAIN}_restart_reconciled_off": "EV - charge stopped after restart",
    f"{DOMAIN}_aux_battery_low": "EV - 12V battery trending low",
    f"{DOMAIN}_aux_battery_critical": "EV - 12V battery critically low",
}

# script.opel_notify_critical / opel_notify_emergency (packages/opel.yaml:
# 174-225) send with critical/time-sensitive push data so they break
# through a silenced phone; opel_notify_push sends plain "high" for
# everything else. Ported the same split here rather than one flat
# priority for every event -- an overheat cutoff or a critically low 12V
# battery is not "informational" the way "charge started" is.
_CRITICAL_EVENTS = frozenset(
    {
        f"{DOMAIN}_overheat_cutoff",
        f"{DOMAIN}_aux_battery_critical",
    }
)


def _priority_data(event_name: str) -> dict[str, object]:
    """Extra `data:` keys layered onto the notify call for HA's own
    companion-app conventions. Everything else gets the existing flat
    "high"/EV Charging channel this function replaces; a critical event
    additionally asks iOS to bypass Do Not Disturb/silent mode (needs the
    critical-alert entitlement to actually do so -- a harmless no-op
    without it) and puts Android on its own channel so it isn't bucketed
    with routine notifications."""
    if event_name in _CRITICAL_EVENTS:
        return {
            "channel": "EV Charging Critical",
            "push": {"sound": {"name": "default", "critical": 1, "volume": 1.0}},
        }
    return {"channel": "EV Charging"}


async def async_dispatch_events(
    hass: HomeAssistant, entry: ConfigEntry, events: Iterable[Event]
) -> None:
    muted = set(entry.options.get(CONF_MUTED_EVENTS, []))
    notify_service = entry.options.get(CONF_NOTIFY_SERVICE) or entry.data.get(
        CONF_NOTIFY_SERVICE
    )

    for event in events:
        hass.bus.async_fire(event.name, dict(event.data))

        short_name = event.name.removeprefix(f"{DOMAIN}_")
        if short_name in muted:
            continue

        if notify_service:
            await _async_push(hass, notify_service, event)

        if event.name in PERSISTENT_EVENTS:
            _async_raise_repair(hass, entry, event)
        else:
            _async_clear_repair_if_any(hass, entry, event.name)


async def _async_push(hass: HomeAssistant, notify_service: str, event: Event) -> None:
    domain, _, service = notify_service.partition(".")
    if not service:
        domain, service = "notify", notify_service
    title = _TITLES.get(event.name, event.name)
    message = _format_message(event)
    data = {"tag": "ev-plug-charging", "priority": "high", "ttl": 0}
    data.update(_priority_data(event.name))
    try:
        await hass.services.async_call(
            domain,
            service,
            {
                "title": title,
                "message": message,
                "data": data,
            },
            blocking=False,
        )
    except Exception:  # noqa: BLE001 -- a bad notify target must not crash a tick
        _LOGGER.exception("Failed to send notification for %s", event.name)


def _format_message(event: Event) -> str:
    parts = [f"{k}={v}" for k, v in event.data.items() if v is not None]
    return ", ".join(parts) if parts else "See the integration's sensors for detail."


def _repair_id(entry: ConfigEntry, event_name: str) -> str:
    return f"{entry.entry_id}_{event_name}"


def _async_raise_repair(hass: HomeAssistant, entry: ConfigEntry, event: Event) -> None:
    try:
        from homeassistant.helpers import issue_registry as ir
    except ImportError:  # pragma: no cover -- older HA without Repairs
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        _repair_id(entry, event.name),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=event.name,
        translation_placeholders={k: str(v) for k, v in event.data.items()},
    )


def _async_clear_repair_if_any(hass: HomeAssistant, entry: ConfigEntry, event_name: str) -> None:
    """A matching 'resolved' event (e.g. overheat clearing) can call this
    to dismiss the issue -- reserved for future use; today issues are
    cleared manually or by the underlying condition no longer recurring."""
    try:
        from homeassistant.helpers import issue_registry as ir
    except ImportError:  # pragma: no cover
        return
    ir.async_delete_issue(hass, DOMAIN, _repair_id(entry, event_name))
