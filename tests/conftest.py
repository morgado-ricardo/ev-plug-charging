"""Shared test fixtures for the pure logic layer.

These tests import custom_components.ev_plug_charging.{logic,session,
rate_model,source,store,models,const} directly, with no Home Assistant
installed -- that is the point (see those modules' docstrings). This
conftest only adds the package root to sys.path and provides small builder
helpers so each scenario test stays readable.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

import pytest

from ev_plug_charging.models import (
    ChargeMode,
    Inputs,
    RateSnapshot,
)

DEFAULT_SEED_RATE = 20.2  # matches packages/ev_charging.yaml's float(20.2) default
DAY = date(2026, 9, 12)


def dt(hour: int, minute: int = 0, second: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hour, minute, second), tzinfo=timezone.utc)


class InputsBuilder:
    """A fluent builder so scenario tests read close to plain English,
    e.g. `base_inputs().at(23, 0).with_soc(60).build()`."""

    def __init__(self) -> None:
        self._kwargs = dict(
            now=dt(23, 0),
            mode=ChargeMode.SMART,
            enabled=True,
            target_soc=80.0,
            min_soc_override=15.0,
            window_start=time(23, 0),
            window_end=time(7, 0),
            window_open_edge=False,
            window_close_edge=False,
            expected_gap_min=40.0,
            power_threshold_w=50.0,
            overheat_protection=True,
            temp_limit_c=65.0,
            rescue_refresh_enabled=True,
            soc=60.0,
            soc_changed_at=dt(22, 55),
            source_reachable=True,
            car_charging=True,
            plug_switch_on=True,
            plug_power_w=1800.0,
            plug_temp_c=30.0,
            plugged=True,
            rate=RateSnapshot(minutes_per_percent=DEFAULT_SEED_RATE),
            ha_start_edge=False,
        )

    def set(self, **kwargs) -> "InputsBuilder":
        self._kwargs.update(kwargs)
        return self

    def build(self) -> Inputs:
        return Inputs(**self._kwargs)


def base_inputs() -> InputsBuilder:
    return InputsBuilder()


@pytest.fixture
def rate() -> RateSnapshot:
    return RateSnapshot(minutes_per_percent=DEFAULT_SEED_RATE)
