"""Tests for temperature unit conversion utilities."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.const import UnitOfTemperature

from custom_components.roommind.utils.temp_utils import (
    _is_fahrenheit,
    celsius_delta_to_ha,
    celsius_to_ha_temp,
    ha_temp_to_celsius,
    ha_temp_unit_str,
)


def _make_hass(unit: UnitOfTemperature) -> MagicMock:
    """Return a mock hass with the given temperature unit."""
    hass = MagicMock()
    hass.config.units.temperature_unit = unit
    return hass


def _make_entity_state(unit_of_measurement: str | None = None) -> MagicMock:
    """Return a mock entity state with optional unit_of_measurement attribute."""
    state = MagicMock()
    attrs: dict = {}
    if unit_of_measurement is not None:
        attrs["unit_of_measurement"] = unit_of_measurement
    state.attributes = attrs
    return state


# ---------------------------------------------------------------------------
# ha_temp_to_celsius
# ---------------------------------------------------------------------------


class TestHaTempToCelsius:
    """Tests for ha_temp_to_celsius."""

    def test_celsius_passthrough(self):
        """Celsius value is returned unchanged."""
        hass = _make_hass(UnitOfTemperature.CELSIUS)
        assert ha_temp_to_celsius(hass, 21.0) == 21.0

    def test_fahrenheit_to_celsius(self):
        """Fahrenheit value is converted to Celsius."""
        hass = _make_hass(UnitOfTemperature.FAHRENHEIT)
        # 68°F = 20°C
        assert ha_temp_to_celsius(hass, 68.0) == pytest.approx(20.0)

    def test_fahrenheit_freezing_point(self):
        """32°F converts to 0°C."""
        hass = _make_hass(UnitOfTemperature.FAHRENHEIT)
        assert ha_temp_to_celsius(hass, 32.0) == pytest.approx(0.0)

    def test_fahrenheit_boiling_point(self):
        """212°F converts to 100°C."""
        hass = _make_hass(UnitOfTemperature.FAHRENHEIT)
        assert ha_temp_to_celsius(hass, 212.0) == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# celsius_to_ha_temp
# ---------------------------------------------------------------------------


class TestCelsiusToHaTemp:
    """Tests for celsius_to_ha_temp."""

    def test_celsius_passthrough(self):
        """Celsius value is returned unchanged."""
        hass = _make_hass(UnitOfTemperature.CELSIUS)
        assert celsius_to_ha_temp(hass, 21.0) == 21.0

    def test_celsius_to_fahrenheit(self):
        """Celsius value is converted to Fahrenheit."""
        hass = _make_hass(UnitOfTemperature.FAHRENHEIT)
        # 20°C = 68°F
        assert celsius_to_ha_temp(hass, 20.0) == pytest.approx(68.0)

    def test_zero_celsius_to_fahrenheit(self):
        """0°C converts to 32°F."""
        hass = _make_hass(UnitOfTemperature.FAHRENHEIT)
        assert celsius_to_ha_temp(hass, 0.0) == pytest.approx(32.0)

    def test_100_celsius_to_fahrenheit(self):
        """100°C converts to 212°F."""
        hass = _make_hass(UnitOfTemperature.FAHRENHEIT)
        assert celsius_to_ha_temp(hass, 100.0) == pytest.approx(212.0)


# ---------------------------------------------------------------------------
# celsius_delta_to_ha
# ---------------------------------------------------------------------------


class TestCelsiusDeltaToHa:
    """Tests for celsius_delta_to_ha."""

    def test_celsius_passthrough(self):
        """Delta is returned unchanged in Celsius mode."""
        hass = _make_hass(UnitOfTemperature.CELSIUS)
        assert celsius_delta_to_ha(hass, 2.0) == 2.0

    def test_delta_to_fahrenheit(self):
        """Delta is scaled by 9/5 for Fahrenheit (no offset)."""
        hass = _make_hass(UnitOfTemperature.FAHRENHEIT)
        # 2°C delta = 3.6°F delta
        assert celsius_delta_to_ha(hass, 2.0) == pytest.approx(3.6)

    def test_zero_delta(self):
        """Zero delta is zero in any unit."""
        hass = _make_hass(UnitOfTemperature.FAHRENHEIT)
        assert celsius_delta_to_ha(hass, 0.0) == 0.0


# ---------------------------------------------------------------------------
# ha_temp_unit_str
# ---------------------------------------------------------------------------


class TestHaTempUnitStr:
    """Tests for ha_temp_unit_str."""

    def test_celsius_returns_degree_c(self):
        hass = _make_hass(UnitOfTemperature.CELSIUS)
        assert ha_temp_unit_str(hass) == "\u00b0C"

    def test_fahrenheit_returns_degree_f(self):
        hass = _make_hass(UnitOfTemperature.FAHRENHEIT)
        assert ha_temp_unit_str(hass) == "\u00b0F"


# ---------------------------------------------------------------------------
# Round-trip consistency
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Verify that converting to Celsius and back preserves the value."""

    def test_fahrenheit_round_trip(self):
        """ha_temp_to_celsius -> celsius_to_ha_temp recovers the original value."""
        hass = _make_hass(UnitOfTemperature.FAHRENHEIT)
        original = 72.5  # °F
        celsius = ha_temp_to_celsius(hass, original)
        recovered = celsius_to_ha_temp(hass, celsius)
        assert recovered == pytest.approx(original)

    def test_celsius_round_trip(self):
        """In Celsius mode, both functions are identity."""
        hass = _make_hass(UnitOfTemperature.CELSIUS)
        original = 21.5
        assert celsius_to_ha_temp(hass, ha_temp_to_celsius(hass, original)) == original


# ---------------------------------------------------------------------------
# Entity-level unit detection (_is_fahrenheit with entity_id)
# ---------------------------------------------------------------------------


class TestEntityLevelUnitDetection:
    """Tests for _is_fahrenheit entity-level unit_of_measurement path."""

    def test_entity_fahrenheit_overrides_global_celsius(self):
        """Entity with °F overrides global Celsius config."""
        hass = _make_hass(UnitOfTemperature.CELSIUS)
        hass.states.get.return_value = _make_entity_state("°F")
        assert _is_fahrenheit(hass, "sensor.temp_living") is True

    def test_entity_no_unit_falls_back_to_global(self):
        """Entity without unit_of_measurement falls back to global config."""
        hass = _make_hass(UnitOfTemperature.FAHRENHEIT)
        hass.states.get.return_value = _make_entity_state()  # no uom attribute
        assert _is_fahrenheit(hass, "sensor.temp_living") is True

        hass_c = _make_hass(UnitOfTemperature.CELSIUS)
        hass_c.states.get.return_value = _make_entity_state()
        assert _is_fahrenheit(hass_c, "sensor.temp_living") is False

    def test_entity_celsius_explicit(self):
        """Entity with °C overrides global Fahrenheit config."""
        hass = _make_hass(UnitOfTemperature.FAHRENHEIT)
        hass.states.get.return_value = _make_entity_state("°C")
        assert _is_fahrenheit(hass, "sensor.temp_living") is False

    def test_entity_state_none_falls_back_to_global(self):
        """hass.states.get returns None, falls back to global config."""
        hass = _make_hass(UnitOfTemperature.FAHRENHEIT)
        hass.states.get.return_value = None
        assert _is_fahrenheit(hass, "sensor.temp_living") is True

        hass_c = _make_hass(UnitOfTemperature.CELSIUS)
        hass_c.states.get.return_value = None
        assert _is_fahrenheit(hass_c, "sensor.temp_living") is False

    def test_ha_temp_to_celsius_with_entity_id(self):
        """ha_temp_to_celsius uses entity-level unit when entity_id is passed."""
        # Global is Celsius, but entity reports °F. 68°F should convert to 20°C.
        hass = _make_hass(UnitOfTemperature.CELSIUS)
        hass.states.get.return_value = _make_entity_state("°F")
        assert ha_temp_to_celsius(hass, 68.0, entity_id="sensor.temp_living") == pytest.approx(20.0)

        # Global is Fahrenheit, but entity reports °C. Value should pass through.
        hass2 = _make_hass(UnitOfTemperature.FAHRENHEIT)
        hass2.states.get.return_value = _make_entity_state("°C")
        assert ha_temp_to_celsius(hass2, 21.0, entity_id="sensor.temp_living") == 21.0

    def test_celsius_to_ha_temp_with_entity_id(self):
        """celsius_to_ha_temp uses only global config (no entity_id parameter).

        This documents that the reverse conversion always relies on the global
        HA unit system, not per-entity attributes.
        """
        hass = _make_hass(UnitOfTemperature.FAHRENHEIT)
        # Even though an entity has °C, celsius_to_ha_temp uses global F config.
        hass.states.get.return_value = _make_entity_state("°C")
        assert celsius_to_ha_temp(hass, 20.0) == pytest.approx(68.0)

        hass_c = _make_hass(UnitOfTemperature.CELSIUS)
        assert celsius_to_ha_temp(hass_c, 20.0) == 20.0
