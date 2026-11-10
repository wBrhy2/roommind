"""Config flow for RoomMind integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class RoomMindConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle the config flow for RoomMind."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, str] | None = None) -> ConfigFlowResult:
        """Handle the initial step – just confirm setup."""
        if user_input is not None:
            # Prevent multiple instances
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="RoomMind", data={})

        return self.async_show_form(step_id="user")
