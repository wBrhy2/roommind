"""Integration: store save/load round-trips with real RoomMindStore."""

from __future__ import annotations

import pytest

from .conftest import ROOM_LIVING


class TestStoreRoundTrip:
    @pytest.mark.asyncio
    async def test_save_and_load_room(self, real_store):
        await real_store.async_load()
        await real_store.async_save_room("living_room", ROOM_LIVING)

        rooms = real_store.get_rooms()
        assert "living_room" in rooms
        assert rooms["living_room"]["comfort_temp"] == 21.0

    @pytest.mark.asyncio
    async def test_update_room_merges(self, real_store):
        await real_store.async_load()
        await real_store.async_save_room("living_room", ROOM_LIVING)
        await real_store.async_update_room("living_room", {"comfort_temp": 22.0})

        rooms = real_store.get_rooms()
        assert rooms["living_room"]["comfort_temp"] == 22.0
        assert rooms["living_room"]["eco_temp"] == 17.0

    @pytest.mark.asyncio
    async def test_settings_merge(self, real_store):
        await real_store.async_load()
        await real_store.async_save_settings({"outdoor_temp_sensor": "sensor.outdoor"})
        await real_store.async_save_settings({"presence_enabled": True})

        settings = real_store.get_settings()
        assert settings["outdoor_temp_sensor"] == "sensor.outdoor"
        assert settings["presence_enabled"] is True

    @pytest.mark.asyncio
    async def test_thermal_data_persists(self, real_store):
        await real_store.async_load()
        thermal = {"living_room": {"alpha": 0.5, "beta_h": 0.3}}
        await real_store.async_save_thermal_data(thermal)

        assert real_store._store.async_save.called
        assert real_store.get_thermal_data() == thermal
