"""Shared helpers and fixtures for MPC controller tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def build_hass():
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states.get = MagicMock(return_value=None)
    return hass


def make_room(**overrides):
    room = {
        "area_id": "living_room",
        "thermostats": ["climate.living_trv"],
        "acs": [],
        "climate_mode": "auto",
        "temperature_sensor": "sensor.living_temp",
        "schedules": [],
    }
    room.update(overrides)
    # Build devices from thermostats/acs for consistency
    hst = room.get("heating_system_type", "")
    room["devices"] = [
        {"entity_id": eid, "type": "trv", "role": "auto", "heating_system_type": hst, "setpoint_mode": "proportional"}
        for eid in room.get("thermostats", [])
    ] + [
        {"entity_id": eid, "type": "ac", "role": "auto", "heating_system_type": "", "setpoint_mode": "proportional"}
        for eid in room.get("acs", [])
    ]
    return room


def _make_ac_state_for_plan(hvac_modes, current_state="heat"):
    """Create a mock AC state with given hvac_modes for heat source plan tests."""
    state = MagicMock()
    state.state = current_state
    state.attributes = {"hvac_modes": hvac_modes, "min_temp": 5.0, "max_temp": 30.0, "temperature": None}
    return state
