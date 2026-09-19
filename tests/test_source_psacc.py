"""The PSACC source: JSON shape, the normalised car_charging mapping, and
the two URLs it builds.

Everything Stellantis-specific lives in sources/psacc.py, so everything
Stellantis-specific is tested here. The shared freshness clock it delegates
to is tested in test_source_freshness.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ev_plug_charging.const import SOURCE_TYPE_PSACC, VEHICLE_COMMANDS
from ev_plug_charging.sources import SOURCE_LABELS, SOURCES, get_source_class
from ev_plug_charging.sources.base import SourceError, TelemetrySource
from ev_plug_charging.sources.psacc import PsaccSource, _vehicle_command_path, parse_vehicle_info

T0 = datetime(2026, 9, 12, 22, 0, tzinfo=timezone.utc)


def _payload(
    level=55.0, status="InProgress", plugged=True, extra_energy=None, aux_battery_soc=None
):
    energy = {"level": level, "charging": {"status": status, "plugged": plugged}}
    if extra_energy:
        energy.update(extra_energy)
    payload = {"energy": [energy]}
    if aux_battery_soc is not None:
        payload["battery"] = {"voltage": aux_battery_soc}
    return payload


# -- parsing ------------------------------------------------------------- #


def test_parses_soc_status_and_plugged():
    snap = parse_vehicle_info(_payload(level=55.0), T0, prev=None)
    assert snap.soc == 55.0
    assert snap.charging_status == "InProgress"
    assert snap.plugged is True
    assert snap.source_reachable is True


def test_car_charge_finished_is_a_separate_terminal_signal():
    """NOT the logical opposite of car_charging -- a provider can report a
    third status (Disconnected, Error, ...) that is neither "in progress"
    nor "finished"."""
    finished = parse_vehicle_info(
        _payload(status="Finished"), T0, None, "InProgress", "Finished"
    )
    assert finished.car_charge_finished is True
    assert finished.car_charging is False

    charging = parse_vehicle_info(
        _payload(status="InProgress"), T0, None, "InProgress", "Finished"
    )
    assert charging.car_charge_finished is False

    neither = parse_vehicle_info(
        _payload(status="Disconnected"), T0, None, "InProgress", "Finished"
    )
    assert neither.car_charge_finished is False
    assert neither.car_charging is False


def test_car_charge_finished_defaults_to_the_finished_string():
    snap = parse_vehicle_info(_payload(status="Finished"), T0, prev=None)
    assert snap.car_charge_finished is True


def test_car_charging_is_normalised_against_the_configured_state_string():
    """Each provider spells "charging" differently; mapping it is the
    source's job so nothing downstream has to know the vocabulary."""
    charging = parse_vehicle_info(_payload(status="InProgress"), T0, None, "InProgress")
    assert charging.car_charging is True

    idle = parse_vehicle_info(_payload(status="Disconnected"), T0, None, "InProgress")
    assert idle.car_charging is False

    # A different deployment reporting a different word entirely.
    custom = parse_vehicle_info(_payload(status="CHARGING"), T0, None, "CHARGING")
    assert custom.car_charging is True


def test_missing_energy_block_is_no_reading_not_an_error():
    snap = parse_vehicle_info({}, T0, prev=None)
    assert snap.soc is None
    assert snap.car_charging is False
    assert snap.source_reachable is True  # the source answered, just emptily


# -- 12V auxiliary battery: NOT a voltage, despite the field's name -------- #


def test_battery_voltage_field_is_parsed_as_a_percentage():
    """Load-bearing: PSA's `battery.voltage` is NOT a voltage. It is the
    12V battery's own state of charge, as a percentage. Reading it as
    volts (or scaling it to look like volts) makes every 12V health
    figure meaningless."""
    snap = parse_vehicle_info(_payload(aux_battery_soc=62.0), T0, prev=None)
    assert snap.aux_battery_soc == 62.0


def test_aux_battery_soc_survives_a_missing_energy_block():
    """Parsed from a payload sibling of energy[], not nested inside it --
    must not go blind just because energy[] is temporarily absent."""
    snap = parse_vehicle_info({"battery": {"voltage": 55.0}}, T0, prev=None)
    assert snap.soc is None
    assert snap.aux_battery_soc == 55.0


def test_aux_battery_soc_is_none_when_the_battery_block_is_absent():
    snap = parse_vehicle_info(_payload(), T0, prev=None)
    assert snap.aux_battery_soc is None


def test_non_numeric_aux_battery_voltage_is_treated_as_no_reading():
    snap = parse_vehicle_info({"battery": {"voltage": "n/a"}, "energy": []}, T0, prev=None)
    assert snap.aux_battery_soc is None


def test_non_numeric_level_is_treated_as_no_reading():
    snap = parse_vehicle_info(_payload(level="n/a"), T0, prev=None)
    assert snap.soc is None


def test_payload_timestamp_is_probed_and_used():
    payload_ts = T0 - timedelta(minutes=12)
    snap = parse_vehicle_info(
        _payload(extra_energy={"updated_at": payload_ts.isoformat()}), T0, prev=None
    )
    assert snap.soc_changed_at == payload_ts
    assert snap.used_payload_timestamp is True


def test_unparseable_payload_timestamp_falls_back_to_value_change():
    snap = parse_vehicle_info(
        _payload(extra_energy={"updated_at": "not-a-date"}), T0, prev=None
    )
    assert snap.used_payload_timestamp is False
    assert snap.soc_changed_at == T0


# -- URLs ---------------------------------------------------------------- #


def _source():
    return PsaccSource(session=None, base_url="http://psacc.example/", vin="VF1TEST")


def test_vehicle_info_url_always_requests_the_cache():
    """from_cache=1 is load-bearing: without it PSACC may wake the vehicle
    to refresh, which is the 12V drain this integration exists to avoid."""
    url = _source()._vehicle_info_url()
    assert url == "http://psacc.example/get_vehicleinfo/VF1TEST?from_cache=1"
    assert "from_cache=1" in url


def test_never_sends_always_check():
    """always_check=true makes PSACC run its own enforcement loop and wake
    the car every ~5 min -- exactly the 12V drain this integration
    exists to avoid."""
    assert "always_check" not in _source()._vehicle_info_url()


def test_wakeup_url():
    assert _source()._url("/wakeup/VF1TEST") == "http://psacc.example/wakeup/VF1TEST"


# -- registry ------------------------------------------------------------ #


def test_psacc_is_registered_and_labelled():
    assert SOURCES[SOURCE_TYPE_PSACC] is PsaccSource
    assert SOURCE_TYPE_PSACC in SOURCE_LABELS


def test_every_registered_source_implements_the_contract():
    """Guards the registry as sources are added: each must be a
    TelemetrySource, declare its own source_type, and be labelled for the
    config flow."""
    for source_type, cls in SOURCES.items():
        assert issubclass(cls, TelemetrySource), source_type
        assert cls.source_type == source_type
        assert source_type in SOURCE_LABELS, f"{source_type} has no config-flow label"


def test_unknown_source_type_raises():
    try:
        get_source_class("no_such_source")
    except SourceError:
        pass
    else:
        raise AssertionError("expected SourceError for an unknown source type")


def test_psacc_supports_refresh():
    """The rescue wakeup only exists for sources that can actually ask."""
    assert PsaccSource.supports_refresh is True


def test_default_vehicle_command_capability_is_empty():
    """A source that declares nothing needs no code at all -- see
    sources/base.py's TelemetrySource.supported_commands docstring."""
    assert TelemetrySource.supported_commands == frozenset()


def test_psacc_supports_aux_battery():
    assert PsaccSource.supports_aux_battery is True


# -- the abstract vehicle-command API ---------------------------------------- #


def test_psacc_supports_every_command_in_the_vocabulary():
    """PSACC is the one source that implements all nine."""
    assert PsaccSource.supported_commands == VEHICLE_COMMANDS


def test_vehicle_command_paths_are_the_real_psacc_endpoints():
    """Pins the command->URL mapping. A typo here fails silently at
    runtime: PSACC answers 404 and the command simply never happens."""
    vin = "VF1TEST"
    assert _vehicle_command_path("wake", vin, None) == f"/wakeup/{vin}"
    assert _vehicle_command_path("lock", vin, None) == f"/lock_door/{vin}/1"
    assert _vehicle_command_path("unlock", vin, None) == f"/lock_door/{vin}/0"
    assert _vehicle_command_path("horn", vin, None) == f"/horn/{vin}/2"
    assert _vehicle_command_path("flash_lights", vin, None) == f"/lights/{vin}/10"
    assert (
        _vehicle_command_path("precondition_start", vin, None)
        == f"/preconditioning/{vin}/1"
    )
    assert (
        _vehicle_command_path("precondition_stop", vin, None)
        == f"/preconditioning/{vin}/0"
    )
    assert _vehicle_command_path("charge_start", vin, None) == f"/charge_now/{vin}/1"
    assert _vehicle_command_path("charge_stop", vin, None) == f"/charge_now/{vin}/0"


def test_vehicle_command_params_override_the_defaults():
    vin = "VF1TEST"
    assert _vehicle_command_path("horn", vin, {"count": 5}) == f"/horn/{vin}/5"
    assert (
        _vehicle_command_path("flash_lights", vin, {"duration": 30})
        == f"/lights/{vin}/30"
    )


def test_vehicle_command_path_rejects_an_unknown_command():
    try:
        _vehicle_command_path("levitate", "VF1TEST", None)
    except SourceError:
        pass
    else:
        raise AssertionError("expected SourceError for an unsupported command")


def test_every_registered_source_declares_supported_commands_as_a_subset_of_the_vocabulary():
    """Guards the registry as sources are added: a source's
    supported_commands must only ever contain capability names from the
    shared vocabulary, never something brand-specific of its own."""
    for source_type, cls in SOURCES.items():
        assert cls.supported_commands <= VEHICLE_COMMANDS, source_type
