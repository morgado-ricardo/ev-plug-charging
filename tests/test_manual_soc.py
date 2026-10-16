"""A state of charge a person supplies -- the confirm prompt's answer,
number.*_current_soc, or the set_soc service -- and the no-telemetry source
built on it.

The night that motivated this is R16 (tests/test_scenarios.py): PSACC
unreachable, the "charge NOT started" push the only thing the integration
could do. These tests cover the other way out of that night: answering the
push, after which the charge runs and stops exactly as a telemetry-fed one
would.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import time, timedelta

from ev_plug_charging.const import SOC_ACTION_CONFIRM, SOC_ACTION_SET
from ev_plug_charging.logic import effective_reading, reduce
from ev_plug_charging.models import ChargeMode, PlugAction, RateSnapshot, SessionState
from ev_plug_charging.logic import window_instance_id, window_open_at
from ev_plug_charging.prompt import (
    confirm_reading_at,
    parse_soc_action,
    parse_soc_reply,
    soc_prompt,
)
from ev_plug_charging.store import from_dict, to_dict

from conftest import base_inputs, dt

RATE = RateSnapshot(minutes_per_percent=20.2)
LAST_READING_AT = dt(18, 0)


def _unreachable(now, **overrides):
    """A tick with PSACC down and its last reading (50% at 18:00) carried
    forward, the way source.unreachable_snapshot() does."""
    kwargs = dict(
        now=now,
        soc=50.0,
        soc_changed_at=LAST_READING_AT,
        source_reachable=False,
        car_charging=False,
        plug_switch_on=False,
        plug_power_w=0.0,
        rate=RATE,
        window_start=time(23, 0),
        window_end=time(16, 0),
        target_soc=80.0,
    )
    kwargs.update(overrides)
    return base_inputs().set(**kwargs).build()


def _answer(state, soc, at):
    """What coordinator.async_set_manual_soc records."""
    return replace(state, manual_soc=soc, manual_soc_at=at)


def _drive(
    state, start, minutes, soc_answer_at=None, soc_answer=None, confirm=False, **overrides
):
    """Tick every 5 minutes, following our own plug commands. Returns
    (state, decisions).

    `confirm=True` answers the way the "Yes, still X%" button does: the
    value held when charging BEGAN (coordinator._on_notification_action
    records it at charge_started_at while a charge is running). Otherwise
    the answer is a typed value read off the car NOW."""
    now = start
    plug_on = overrides.pop("plug_on", False)
    decisions = []
    for _ in range(minutes // 5 + 1):
        if soc_answer_at is not None and now >= soc_answer_at and state.manual_soc_at is None:
            at = state.charge_started_at if confirm and state.charge_started_at else now
            state = _answer(state, soc_answer, at)
        inp = _unreachable(
            now,
            plug_switch_on=plug_on,
            plug_power_w=1800.0 if plug_on else 0.0,
            window_open_edge=(now == start),
            **overrides,
        )
        state, decision = reduce(state, inp)
        decisions.append(decision)
        if decision.plug == PlugAction.ON:
            plug_on = True
        elif decision.plug == PlugAction.OFF:
            plug_on = False
        now += timedelta(minutes=5)
    return state, decisions


# --------------------------------------------------------------------------- #
# effective_reading: which reading reduce() decides from
# --------------------------------------------------------------------------- #


def test_manual_reading_newer_than_telemetry_wins():
    state = _answer(SessionState(), 55.0, dt(23, 5))
    assert effective_reading(state, _unreachable(dt(23, 6))) == (55.0, dt(23, 5), True)


def test_newer_telemetry_supersedes_the_manual_reading():
    state = _answer(SessionState(), 55.0, dt(23, 5))
    inp = _unreachable(dt(23, 40), soc=58.0, soc_changed_at=dt(23, 30), source_reachable=True)
    assert effective_reading(state, inp) == (58.0, dt(23, 30), False)


def test_manual_reading_is_spent_by_the_charge_it_started():
    state = replace(_answer(SessionState(), 55.0, dt(23, 5)), charge_completed_at=dt(23, 50))
    assert effective_reading(state, _unreachable(dt(23, 55))) == (50.0, LAST_READING_AT, False)


# --------------------------------------------------------------------------- #
# The R16 night, answered
# --------------------------------------------------------------------------- #


def _stop(decisions):
    return next(d for d in decisions if d.plug == PlugAction.OFF)


def test_unanswered_night_starts_at_window_open_and_stops_at_target():
    state = SessionState(ha_started_at=dt(12, 0))
    state, decisions = _drive(state, dt(23, 0), 14 * 60)

    # 23:00: started on the last reading, and the question went out with
    # what the prompt needs.
    assert decisions[0].plug == PlugAction.ON
    assert decisions[0].reason == "below_target_unreachable"
    asked = [e for d in decisions for e in d.events if e.name.endswith("soc_confirm_requested")]
    assert len(asked) == 1  # once per window
    assert asked[0].data["last_soc"] == 50.0
    assert asked[0].data["window_id"] == "2026-09-12"
    assert asked[0].data["target_soc"] == 80.0

    # Nobody answered: R16's blind projection stops it near 80.
    stop = _stop(decisions)
    assert stop.reason == "target_reached_unreachable"
    assert 80.0 <= stop.projected_soc <= 81.0
    assert state.complete_notified


def test_yes_during_the_charge_confirms_the_start_value():
    """'Yes, still 50%' half an hour in means 50% when charging began --
    the anchor stays 50 and the stop lands where the unanswered night's
    did, now on the ordinary (trusted) target path."""
    state = SessionState(ha_started_at=dt(12, 0))
    state, decisions = _drive(
        state, dt(23, 0), 14 * 60, soc_answer_at=dt(23, 30), soc_answer=50.0, confirm=True
    )
    assert state.anchor.soc == 50 and state.anchor.corrected
    stop = _stop(decisions)
    assert stop.reason == "target_reached"
    assert stop.soc_origin == "manual"
    assert 80.0 <= stop.projected_soc <= 81.0

    _, unanswered = _drive(SessionState(ha_started_at=dt(12, 0)), dt(23, 0), 14 * 60)
    assert abs(
        decisions.index(stop) - unanswered.index(_stop(unanswered))
    ) <= 1, "confirming the start value must not move the stop"


def test_typed_value_during_the_charge_corrects_the_start():
    """A typed 55 at 23:30 is what the car shows NOW, so the start is
    back-solved from it: ~25 minutes of charging at the capped rate before
    23:30 puts the start near 54, and the stop comes earlier than the
    unanswered night's."""
    state = SessionState(ha_started_at=dt(12, 0))
    state, decisions = _drive(
        state, dt(23, 0), 14 * 60, soc_answer_at=dt(23, 30), soc_answer=55.0
    )
    assert state.anchor.soc == 54 and state.anchor.corrected
    stop = _stop(decisions)
    assert stop.reason == "target_reached"
    assert 80.0 <= stop.projected_soc <= 81.0

    _, unanswered = _drive(SessionState(ha_started_at=dt(12, 0)), dt(23, 0), 14 * 60)
    assert decisions.index(stop) < unanswered.index(_stop(unanswered))


def test_plug_already_on_by_hand_is_kept_and_still_asked_once():
    state = SessionState(ha_started_at=dt(12, 0))
    _, decisions = _drive(state, dt(23, 0), 60, plug_on=True)
    assert decisions[0].plug == PlugAction.UNCHANGED
    assert decisions[0].reason == "below_target_unreachable"
    asked = [e for d in decisions for e in d.events if e.name.endswith("soc_confirm_requested")]
    assert len(asked) == 1


def test_the_answer_is_not_reused_by_the_next_window():
    state = SessionState(ha_started_at=dt(12, 0))
    state, _ = _drive(state, dt(23, 0), 14 * 60, soc_answer_at=dt(23, 5), soc_answer=50.0)

    # Next evening, PSACC still down: ask again rather than charging a
    # second time from last night's 50%.
    next_day = dt(23, 0) + timedelta(days=1)
    state, decision = reduce(state, _unreachable(next_day, window_open_edge=True))
    assert decision.plug == PlugAction.UNCHANGED
    assert decision.reason == "soc_untrustworthy"
    assert any(e.name.endswith("charge_not_started") for e in decision.events)


def test_answering_mid_charge_corrects_the_anchor_once():
    """A manual plug-on before answering (what really happened in R16):
    the anchor is captured from the stale 50% and is provisional. The
    answer is a fresh reading, so session.py's one-shot correction moves
    it -- and a second answer does not move it again."""
    state = SessionState(ha_started_at=dt(12, 0))
    start = dt(23, 10)
    state, _ = reduce(state, _unreachable(start, plug_switch_on=True, plug_power_w=1800.0))
    assert state.anchor.soc == 50.0 and state.anchor.provisional

    answer_at = start + timedelta(minutes=30)
    state = _answer(state, 62.0, answer_at)
    state, decision = reduce(
        state, _unreachable(answer_at, plug_switch_on=True, plug_power_w=1800.0)
    )
    assert state.anchor.corrected and not state.anchor.provisional
    # 62 at +30 min, back-solved at the capped rate -> ~61 at the start.
    assert state.anchor.soc == 61
    assert decision.soc_origin == "manual"

    later = answer_at + timedelta(minutes=30)
    state = _answer(state, 70.0, later)
    state, _ = reduce(state, _unreachable(later, plug_switch_on=True, plug_power_w=1800.0))
    assert state.anchor.soc == 61


def test_no_soc_stale_push_while_the_person_is_the_feed():
    state = SessionState(ha_started_at=dt(12, 0))
    _, decisions = _drive(state, dt(23, 0), 4 * 60, soc_answer_at=dt(23, 5), soc_answer=50.0)
    assert not any(d.soc_stale for d in decisions)
    assert not any(e.name.endswith("soc_stale") for d in decisions for e in d.events)


def test_raw_telemetry_not_the_manual_value_is_what_persists_for_the_freshness_clock():
    """prev_soc seeds source.py's freshness clock after a restart. If the
    manual 62 landed there, PSACC's unchanged cached 50 would look like a
    fresh change on the first poll and override the person's answer."""
    state = _answer(SessionState(ha_started_at=dt(12, 0)), 62.0, dt(23, 5))
    state, _ = reduce(state, _unreachable(dt(23, 6)))
    assert state.prev_soc == 50.0
    assert state.prev_soc_changed_at == LAST_READING_AT
    assert state.prev_manual_soc_at == dt(23, 5)


def test_manual_reading_survives_a_restart():
    state = _answer(SessionState(), 62.0, dt(23, 5))
    restored = from_dict(to_dict(replace(state, prev_manual_soc_at=dt(23, 5))))
    assert restored.manual_soc == 62.0
    assert restored.manual_soc_at == dt(23, 5)
    assert restored.prev_manual_soc_at == dt(23, 5)


def test_old_stored_state_without_manual_fields_still_loads():
    data = to_dict(SessionState())
    for key in ("manual_soc", "manual_soc_at", "prev_manual_soc_at"):
        data.pop(key)
    assert from_dict(data).manual_soc is None


# --------------------------------------------------------------------------- #
# No-telemetry source (source_reports_live=False)
# --------------------------------------------------------------------------- #


def _no_telemetry(now, **overrides):
    kwargs = dict(
        soc=None,
        soc_changed_at=None,
        source_reachable=True,
        source_reports_live=False,
    )
    kwargs.update(overrides)
    return _unreachable(now, **kwargs)


def test_no_telemetry_asks_for_a_reading_then_charges_and_announces_it():
    state = SessionState(ha_started_at=dt(12, 0))
    state, decision = reduce(state, _no_telemetry(dt(23, 0), window_open_edge=True))
    asked = [e for e in decision.events if e.name.endswith("charge_not_started")]
    assert asked and asked[0].data["reason"] == "no_reading"
    assert asked[0].data["last_soc"] is None

    state = _answer(state, 40.0, dt(23, 2))
    state, decision = reduce(state, _no_telemetry(dt(23, 2)))
    assert decision.plug == PlugAction.ON

    # No car status ever arrives, so Smart's car-keyed "charge started"
    # would never fire; the plug-power path announces it instead.
    state, _ = reduce(state, _no_telemetry(dt(23, 3), plug_switch_on=True, plug_power_w=1800.0))
    state, decision = reduce(
        state, _no_telemetry(dt(23, 4), plug_switch_on=True, plug_power_w=1800.0)
    )
    started = [e for e in decision.events if e.name.endswith("charge_started")]
    assert len(started) == 1
    assert started[0].data["mode"] == ChargeMode.SMART.value
    assert started[0].data["soc"] == 40.0


def test_live_source_smart_mode_does_not_get_the_plug_side_started_push():
    state = SessionState(ha_started_at=dt(12, 0))
    events = []
    for minute in (0, 1, 2):
        state, decision = reduce(
            state,
            base_inputs().set(now=dt(23, minute), car_charging=False, rate=RATE).build(),
        )
        events.extend(decision.events)
    assert not any(e.name.endswith("charge_started") for e in events)


# --------------------------------------------------------------------------- #
# The prompt itself (prompt.py)
# --------------------------------------------------------------------------- #


def _not_started(**overrides):
    data = {
        "reason": "source_unreachable",
        "last_soc": 50.0,
        "last_soc_at": "2026-09-12T18:00:00+00:00",
        "target_soc": 80.0,
        "window_id": "2026-09-12",
    }
    data.update(overrides)
    return data


def test_prompt_offers_confirm_and_free_text_when_a_value_exists():
    prompt = soc_prompt(_not_started(), "entry1", "19:00")
    assert "Last reading: 50% at 19:00" in prompt["message"]
    assert "charge to 80%" in prompt["message"]
    confirm, enter = prompt["actions"]
    assert confirm == {"action": f"{SOC_ACTION_CONFIRM}|entry1|2026-09-12", "title": "Yes, 50%"}
    assert enter["action"] == f"{SOC_ACTION_SET}|entry1|2026-09-12"
    assert enter["behavior"] == "textInput"
    assert "Current SoC" in prompt["message_no_actions"]


def test_prompt_has_no_confirm_without_a_value():
    prompt = soc_prompt(_not_started(reason="no_reading", last_soc=None), "entry1", None)
    assert [a["action"].split("|")[0] for a in prompt["actions"]] == [SOC_ACTION_SET]
    assert prompt["message"].startswith("No state-of-charge reading.")


def test_prompt_needs_a_window():
    assert soc_prompt({"last_soc": 50.0}, "entry1", None) is None


def test_action_ids_round_trip():
    prompt = soc_prompt(_not_started(), "entry1", None)
    parsed = [parse_soc_action(a["action"]) for a in prompt["actions"]]
    assert parsed == [
        (SOC_ACTION_CONFIRM, "entry1", "2026-09-12"),
        (SOC_ACTION_SET, "entry1", "2026-09-12"),
    ]
    assert parse_soc_action("SOMETHING_ELSE|entry1|x") is None
    assert parse_soc_action(f"{SOC_ACTION_SET}|entry1") is None
    assert parse_soc_action(None) is None


def test_reply_parsing():
    assert parse_soc_reply("62") == 62.0
    assert parse_soc_reply(" 62% ") == 62.0
    assert parse_soc_reply("62,5") == 62.5
    assert parse_soc_reply("101") is None
    assert parse_soc_reply("-1") is None
    assert parse_soc_reply("sixty") is None
    assert parse_soc_reply(None) is None


def test_prompt_while_already_charging_says_so():
    prompt = soc_prompt(_not_started(), "entry1", "18:00", charging=True)
    assert prompt["message"].startswith("Charging from the last reading, 50% at 18:00")
    assert "stop at 80%" in prompt["message"]
    assert "what the car shows now" in prompt["message"]
    assert [a["title"] for a in prompt["actions"]] == ["Yes, 50%", "Enter SoC"]
    assert "what the car shows now" in prompt["message_no_actions"]


def test_prompt_names_a_reading_older_than_the_last_charge():
    prompt = soc_prompt(_not_started(reason="reading_predates_last_charge"), "entry1", None)
    assert prompt["message"].startswith("The last reading is older than the last charge.")


def test_yes_is_true_when_the_question_was_asked_not_when_tapped():
    opened = dt(23, 0)
    tapped = dt(23, 40)
    # Charging since the window opened: true at the start.
    assert confirm_reading_at(
        tapped, charging=True, charge_started_at=dt(23, 0), window_open_at=opened
    ) == dt(23, 0)
    # Plug switched on by hand BEFORE the window: the question came at
    # window open, so that is when "still 50%" was vouched for.
    assert confirm_reading_at(
        tapped, charging=True, charge_started_at=dt(21, 0), window_open_at=opened
    ) == opened
    # Nothing running: simply now.
    assert confirm_reading_at(
        tapped, charging=False, charge_started_at=dt(23, 0), window_open_at=opened
    ) == tapped


def test_window_open_at_inverts_window_instance_id():
    start, end = time(23, 0), time(7, 0)
    for now in (dt(23, 30), dt(23, 30) + timedelta(hours=5)):  # both sides of midnight
        wid = window_instance_id(now, start, end)
        assert window_open_at(wid, start, now.tzinfo) == dt(23, 0)
