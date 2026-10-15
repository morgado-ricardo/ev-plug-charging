"""The confirm-start-SoC prompt, as plain data: the question notify.py
sends when a Smart charge can't start for want of a trustworthy reading,
and the parsing of the answer coordinator.py receives back.

Zero Home Assistant imports, like logic.py, so both halves are unit-tested
without HA installed. The HA plumbing around them (sending the push,
listening for the companion app's action event, rejecting an answer for a
window that has closed) stays in notify.py and coordinator.py.
"""
from __future__ import annotations

from typing import Any, Optional

from .const import SOC_ACTION_CONFIRM, SOC_ACTION_SET


def soc_prompt(
    data: Any, entry_id: str, last_at_hhmm: Optional[str]
) -> Optional[dict[str, Any]]:
    """The charge_not_started push, turned into the question it really is:
    "is it still X%?" when a last value exists, "what is it?" when none
    does. Pure (no hass) so its payload is unit-testable.

    Action ids carry the entry and the window (const.SOC_ACTION_*); the
    coordinator ignores an answer for any other window. "Confirm" is only
    offered with a value to confirm. The typed reply uses the companion
    app's `behavior: textInput`, answered as `reply_text`.
    """
    window_id = data.get("window_id")
    if window_id is None:
        return None
    last_soc = data.get("last_soc")
    target = data.get("target_soc")
    target_txt = f"{target:g}%" if isinstance(target, (int, float)) else "target"
    suffix = f"|{entry_id}|{window_id}"
    actions: list[dict[str, Any]] = []
    if last_soc is not None:
        when = f" at {last_at_hhmm}" if last_at_hhmm else ""
        why = (
            "The car's data source is unreachable"
            if data.get("reason") == "source_unreachable"
            else "No fresh state-of-charge reading"
        )
        head = f"{why}. Last reading: {last_soc:g}%{when}."
        ask = (
            f"Still {last_soc:g}%? Confirm to charge to {target_txt}, "
            "or reply with the current state of charge."
        )
        actions.append({"action": SOC_ACTION_CONFIRM + suffix, "title": f"Yes, {last_soc:g}%"})
    else:
        head = "No state-of-charge reading."
        ask = f"Reply with the current state of charge to charge to {target_txt}."
    actions.append(
        {
            "action": SOC_ACTION_SET + suffix,
            "title": "Enter SoC",
            "behavior": "textInput",
            "textInputButtonTitle": "Charge",
            "textInputPlaceholder": "e.g. 55",
        }
    )
    return {
        "message": f"{head} {ask}",
        "actions": actions,
        "message_no_actions": (
            f"{head} Set 'Current SoC' on the dashboard to charge to {target_txt}."
        ),
    }


def parse_soc_reply(value: Any) -> Optional[float]:
    """A typed reply ("62", "62%", " 62,5 ") -> 62.0, or None if it isn't
    a state of charge. Free function so it is testable without HA."""
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip("%").strip().replace(",", ".")
    try:
        soc = float(text)
    except ValueError:
        return None
    if not 0.0 <= soc <= 100.0:
        return None
    return soc


def parse_soc_action(action: Any) -> Optional[tuple[str, str, str]]:
    """A companion-app action id -> (prefix, entry_id, window_id), or None
    if it isn't one of ours. The inverse of the ids soc_prompt() builds."""
    if not isinstance(action, str):
        return None
    prefix, _, rest = action.partition("|")
    if prefix not in (SOC_ACTION_CONFIRM, SOC_ACTION_SET):
        return None
    entry_id, _, window_id = rest.partition("|")
    if not entry_id or not window_id:
        return None
    return prefix, entry_id, window_id
