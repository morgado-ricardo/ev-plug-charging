"""Repairs integration point.

All issues this integration raises today (see notify.py's
PERSISTENT_EVENTS handling) are informational, not fixable: overheat,
bypass, EVSE-no-power, charge-not-started. There is nothing HA's Repairs
UI can offer to "fix" automatically -- a human has to look at the plug, the
cable, or the car. They clear themselves once the underlying condition
stops recurring (or can be dismissed by hand), so no confirm/fix flow is
registered here.

This module exists as the place a future fixable issue (e.g. "PSACC has
been unreachable for N days -- reconfigure?") would register an
async_create_fix_flow, per Home Assistant's repairs platform contract.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None):
    """No fixable issues are raised today; see the module docstring."""
    raise NotImplementedError
