"""Decision.plug_delivering: whether OUR plug is drawing current, exposed
on its own for binary_sensor.*_plug_delivering_power -- useful when
diagnosing an EVSE that's on but drawing nothing, without also having to
check whether the car agrees it's charging.

Also pins that plug_delivering IS charging_active (plug power is the sole
authority on whether a charge is happening on our plug) -- car status
alone is not, see test_bypass_leaves_charging_active_and_plug_delivering_false.
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


def test_bypass_leaves_charging_active_and_plug_delivering_false():
    """OUR plug's power draw is the sole authority on charging_active -- a
    bypass session (car charging, plug never involved) is still classified
    BYPASS (classify_source takes car_charging and plug_delivering
    independently) but must NOT drive charging_active, the projection, or
    a plug-side completion. Pins the incident this guards against: a car
    API latching "charging" with nothing plugged into our socket used to
    make charging_active true on car status alone, which could drive the
    projection to a false target_reached."""
    _, d = _tick(
        SessionState(),
        car_charging=True,
        plug_switch_on=False,
        plug_power_w=0.0,
    )
    assert d.charging_active is False
    assert d.plug_delivering is False
    assert d.charge_source.value == "bypass"
