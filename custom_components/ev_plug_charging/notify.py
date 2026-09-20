"""Dispatches logic.reduce()'s events: always onto the HA event bus, to
every configured notification target, and -- for persistent conditions --
as a self-clearing Repairs issue.

One chokepoint, so every reportable condition goes out the same way: to
every target the user configures, or none at all -- the events fire
regardless, so an automation can be built without any notify platform.

async_fire_plug_actuation() (bottom of this file) is deliberately a
SEPARATE entry point, not routed through async_dispatch_events: it is an
audit trail for the logbook, not a reportable condition, and muting a push
must not erase the record that the plug was actuated at all.
"""
from __future__ import annotations

import logging
from typing import Iterable

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_LOGBOOK_ENTRY
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceNotFound

from .const import (
    CONF_MUTED_EVENTS,
    CONF_NOTIFY_TARGETS,
    DOMAIN,
    EVENT_PLUG_COMMANDED,
    PERSISTENT_EVENTS,
)
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

# These go out on the critical/time-sensitive push channel, so they break
# through a silenced phone; everything else sends plain "high". One flat
# priority for every event would be wrong in both directions -- an
# overheat cutoff is not "informational" the way "charge started" is, and
# "charge started" does not deserve to wake anyone up.
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


def _coerce_targets(value: object) -> list[str]:
    """`.storage/core.config_entries` is a hand-editable text file, and
    entries this old can still carry the pre-v3 scalar shape if something
    bypassed async_migrate_entry (a restored backup, a hand edit). A bare
    string handed to a `for` loop iterates its CHARACTERS, not itself --
    coerce defensively rather than let that happen."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


async def async_dispatch_events(
    hass: HomeAssistant, entry: ConfigEntry, events: Iterable[Event]
) -> None:
    muted = set(entry.options.get(CONF_MUTED_EVENTS, []))
    targets = _coerce_targets(entry.options.get(CONF_NOTIFY_TARGETS))

    for event in events:
        hass.bus.async_fire(event.name, dict(event.data))

        short_name = event.name.removeprefix(f"{DOMAIN}_")
        if short_name not in muted:
            for target in targets:
                await _async_push_one(hass, target, event)

        if event.name in PERSISTENT_EVENTS:
            _async_raise_repair(hass, entry, event)
        else:
            _async_clear_repair_if_any(hass, entry, event.name)


async def _async_push_one(hass: HomeAssistant, target: str, event: Event) -> None:
    """One target, fully isolated: a bad target must never stop the
    others, and must never crash the tick that triggered it (D8's fail-
    closed rule is about the CHARGING decision, not about a notification
    -- but the same "never let a side channel take down the main one"
    instinct applies)."""
    try:
        await _async_push(hass, target, event)
    except (ServiceNotFound, vol.Invalid):
        _LOGGER.warning(
            "Notification target %r rejected the %s event (service not found or "
            "bad payload) -- check it still exists in Settings -> Devices & "
            "Services -> EV Plug Charging -> Configure",
            target,
            event.name,
        )
    except Exception:  # noqa: BLE001 -- a bad notify target must not crash a tick
        _LOGGER.exception("Failed to send notification for %s to %r", event.name, target)


async def _async_push(hass: HomeAssistant, target: str, event: Event) -> None:
    """Two send paths, because only one of them can carry `data`.

    A LEGACY notify.<service> (what mobile_app has always registered, and
    what most non-mobile_app notify platforms still register) accepts
    title/message/target/data -- so the critical-alert channel and sound
    in `data` survive.

    A notify ENTITY's `send_message` service is registered with ONLY
    `message` and `title` (`vol.Schema({message, title},
    extra=PREVENT_EXTRA)` in HA's own notify/__init__.py) -- passing
    `data` there does not degrade gracefully, it raises vol.Invalid
    synchronously, before the call would even reach the entity. So the
    two paths need two different payloads, not one dict reused with a key
    dropped.

    The legacy path is tried first and preferred whenever it exists,
    because it is the only one that can carry a critical alert -- picking
    the entity path when both exist would silently downgrade every
    overheat/12V-critical push for that target.
    """
    domain, _, service = target.partition(".")
    if not service:
        domain, service = "notify", target

    title = _TITLES.get(event.name, event.name)
    message = _format_message(event)

    if hass.services.has_service(domain, service):
        data = {"tag": "ev-plug-charging", "priority": "high", "ttl": 0}
        data.update(_priority_data(event.name))
        await hass.services.async_call(
            domain,
            service,
            {"title": title, "message": message, "data": data},
            blocking=False,
        )
        return

    if hass.states.get(target) is not None:
        _warn_once_degraded(hass, target)
        await hass.services.async_call(
            "notify",
            "send_message",
            {"entity_id": target, "title": title, "message": message},
            blocking=False,
        )
        return

    raise ServiceNotFound(domain, service)


def _warn_once_degraded(hass: HomeAssistant, target: str) -> None:
    """Warn the first time a target falls through to the entity path in
    this HA run, not once per event -- a busy night would otherwise spam
    the log with the same fact. Keyed in hass.data rather than a module-
    level set: a module-level set would leak across config-entry reloads
    and, in a test run, across the whole test session."""
    seen = hass.data.setdefault(f"{DOMAIN}_notify_degraded_targets", set())
    if target in seen:
        return
    seen.add(target)
    _LOGGER.warning(
        "Notification target %r is a notify entity with no matching legacy "
        "service, so it cannot carry the critical-alert channel -- an "
        "overheat or critically-low-12V push to it will not break through "
        "Do Not Disturb. Prefer the notify.<device> service for a target "
        "you want critical alerts to reach.",
        target,
    )


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


async def async_fire_plug_actuation(
    hass: HomeAssistant,
    entity_id: str,
    action: str,
    reason: str,
    context: Context,
) -> None:
    """Fire two bus events, both carrying `context` -- the SAME Context the
    caller is about to pass to the switch.turn_on/turn_off service call --
    so Home Assistant's logbook can attribute that state change to this
    integration instead of recording an anonymous flip.

    Two events, because HA's logbook has two separate views and each needs
    a different shape to be reached:

    - EVENT_PLUG_COMMANDED (a domain event with NO entity_id in its data)
      is described by logbook.py's async_describe_events and shows up in
      the main Logbook panel, alongside "triggered by automation X" lines.
    - EVENT_LOGBOOK_ENTRY (raw core.EVENT_LOGBOOK_ENTRY, "logbook_entry")
      carries entity_id explicitly, which is what makes it appear on the
      PLUG'S OWN history/more-info logbook card -- a describable domain
      event alone does not reach that view, because logbook only queries
      event types owned by the SAME config-entry domain as the entities
      being viewed, and switch.* belongs to a different integration
      entirely (whatever created the plug).

    Fired directly here rather than through async_dispatch_events on
    purpose: this must run BEFORE the switch service call (the logbook
    attributes a context to whichever row is EARLIEST to carry that
    context id -- fire after, and the plug's own state-change row wins
    that slot and there is nothing left to attribute it to), and it must
    never become a push notification or respect CONF_MUTED_EVENTS -- an
    audit trail that can be muted is not an audit trail.
    """
    verb = "turned the plug on" if action == "on" else "turned the plug off"
    message = f"{verb} ({reason})"

    hass.bus.async_fire(
        EVENT_PLUG_COMMANDED,
        {"action": action, "reason": reason},
        context=context,
    )
    hass.bus.async_fire(
        EVENT_LOGBOOK_ENTRY,
        {"name": "EV Plug Charging", "message": message, "entity_id": entity_id},
        context=context,
    )
