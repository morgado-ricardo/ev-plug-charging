"""Describes EVENT_PLUG_COMMANDED for Home Assistant's logbook.

This is a logbook PLATFORM (discovered via manifest.json's
after_dependencies, not forwarded through const.PLATFORMS -- that list is
for entity platforms, and adding "logbook" there would make HA try to set
up a logbook ENTITY platform and fail). Modelled directly on
homeassistant/components/automation/logbook.py, which is the reference
implementation for this hook.

EVENT_PLUG_COMMANDED is fired with the SAME Context as the switch.turn_on/
turn_off call it brackets (see coordinator.py's _act_on_decision and
notify.py's async_fire_plug_actuation). Registering it here is what lets
the logbook's ContextAugmenter turn that shared context into a readable
"EV Plug Charging: turned the plug on (below_target)" line in the main
Logbook panel, instead of an anonymous state change.

The plug's OWN entity logbook card is a separate view with its own
requirements -- see async_fire_plug_actuation's docstring for why this
platform alone does not reach it, and what does.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
    LazyEventPartialState,
)
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, EVENT_PLUG_COMMANDED


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[
        [str, str, Callable[[LazyEventPartialState], dict[str, Any]]], None
    ],
) -> None:
    @callback
    def describe(event: LazyEventPartialState) -> dict[str, Any]:
        # Deliberately reads ONLY event.data -- keeps this unit-testable
        # with a two-line stub, and matches what the reference
        # implementation (automation/logbook.py) does.
        data = event.data
        action = data.get("action")
        reason = data.get("reason")
        verb = "turned the plug on" if action == "on" else "turned the plug off"
        return {
            LOGBOOK_ENTRY_NAME: "EV Plug Charging",
            LOGBOOK_ENTRY_MESSAGE: f"{verb} ({reason})",
        }

    async_describe_event(DOMAIN, EVENT_PLUG_COMMANDED, describe)
