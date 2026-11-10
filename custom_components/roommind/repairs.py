"""Repair flows for the RoomMind integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant


class RestartRequiredFixFlow(RepairsFlow):
    """Handler for restart-required repair."""

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        """Ask the user to confirm a restart."""
        return await self.async_step_confirm_restart()

    async def async_step_confirm_restart(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        """Handle the confirm step."""
        if user_input is not None:
            await self.hass.services.async_call("homeassistant", "restart")
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="confirm_restart",
            data_schema=vol.Schema({}),
        )


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, str] | None) -> RepairsFlow:
    """Create flow."""
    return RestartRequiredFixFlow()
