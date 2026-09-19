"""Decision.plug_delivering: just the plug's own contribution to
charging_active, exposed on its own for binary_sensor.*_plug_delivering_power
-- useful when diagnosing an EVSE that's on but drawing nothing, without
also having to check whether the car agrees it's charging.
"""
from __future__ import annotations

from ev_plug_charging.logic import reduce
from ev_plug_charging.models import SessionState

from conftest import base_inputs


def _tick(state, **overrides):
    inp = base_inputs().set(**overrides).build()
    return reduce(state, inp)


def test_plug_delivering_true_when_power_exceeds_the_threshold():
    _, d = _tick(
        SessionState(),
        plug_switch_on=True,
        plug_power_w=1800.0,
        power_threshold_w=50.0,
    )
    assert d.plug_delivering is True


def test_plug_delivering_false_when_power_is_below_the_threshold():
    """The plug is on but drawing nothing -- exactly the EVSE-fault case
    this entity exists to surface, independent of car_charging."""
    _, d = _tick(
        SessionState(),
        plug_switch_on=True,
        plug_power_w=10.0,
        power_threshold_w=50.0,
        car_charging=False,
    )
    assert d.plug_delivering is False


def test_plug_delivering_can_be_true_while_charge_source_is_bypass():
    """charging_active is car_charging OR plug_delivering -- a bypass
    session (car charging, plug never involved) has plug_delivering False
    even though charging_active is True."""
    _, d = _tick(
        SessionState(),
        car_charging=True,
        plug_switch_on=False,
        plug_power_w=0.0,
    )
    assert d.charging_active is True
    assert d.plug_delivering is False
