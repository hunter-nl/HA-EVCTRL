from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import const as evctrl_const

DOMAIN = evctrl_const.DOMAIN
CONF_WS_URL = evctrl_const.CONF_WS_URL
CONF_USERNAME = evctrl_const.CONF_USERNAME
CONF_PASSWORD = evctrl_const.CONF_PASSWORD
CONF_RECONNECT_INTERVAL = evctrl_const.CONF_RECONNECT_INTERVAL
CONF_SENSOR_PREFIX = evctrl_const.CONF_SENSOR_PREFIX
CONF_PAYLOAD_LOG_LEVEL = getattr(evctrl_const, "CONF_PAYLOAD_LOG_LEVEL", "payload_log_level")
DEFAULT_RECONNECT_INTERVAL = evctrl_const.DEFAULT_RECONNECT_INTERVAL
DEFAULT_SENSOR_PREFIX = evctrl_const.DEFAULT_SENSOR_PREFIX
DEFAULT_PAYLOAD_LOG_LEVEL = getattr(evctrl_const, "DEFAULT_PAYLOAD_LOG_LEVEL", "off")
PAYLOAD_LOG_LEVEL_OPTIONS = getattr(
    evctrl_const,
    "PAYLOAD_LOG_LEVEL_OPTIONS",
    ("off", "info", "debug"),
)

VALID_STREAM_SCHEMES = {"http", "https", "ws", "wss"}


def _validate_stream_url(value: str) -> str:
    """Validate and normalize a WebSocket or Server-Sent Events URL."""
    stream_url = value.strip()
    parsed_url = urlparse(stream_url)
    if not stream_url or parsed_url.scheme.lower() not in VALID_STREAM_SCHEMES:
        raise vol.Invalid("invalid_stream_url")
    if not parsed_url.netloc:
        raise vol.Invalid("invalid_stream_url")
    return stream_url


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_WS_URL): _validate_stream_url,
        vol.Optional(CONF_USERNAME, default=""): str,
        vol.Optional(CONF_PASSWORD, default=""): selector.TextSelector({"type": "password"}),
        vol.Optional(CONF_RECONNECT_INTERVAL, default=DEFAULT_RECONNECT_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=300)
        ),
        vol.Optional(CONF_SENSOR_PREFIX, default=DEFAULT_SENSOR_PREFIX): str,
    }
)


class EvCtrlFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle initial configuration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                stream_url = _validate_stream_url(user_input[CONF_WS_URL])
            except vol.Invalid:
                errors[CONF_WS_URL] = "invalid_stream_url"
            else:
                sensor_prefix = user_input.get(CONF_SENSOR_PREFIX, DEFAULT_SENSOR_PREFIX).strip()
                return self.async_create_entry(
                    title=sensor_prefix or DEFAULT_SENSOR_PREFIX,
                    data={
                        CONF_WS_URL: stream_url,
                        CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                    options={
                        CONF_RECONNECT_INTERVAL: user_input[CONF_RECONNECT_INTERVAL],
                        CONF_SENSOR_PREFIX: sensor_prefix or DEFAULT_SENSOR_PREFIX,
                        CONF_PAYLOAD_LOG_LEVEL: DEFAULT_PAYLOAD_LOG_LEVEL,
                    },
                )

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return EvCtrlOptionsFlowHandler()


class EvCtrlOptionsFlowHandler(config_entries.OptionsFlow):
    """Manage settings for an existing EV controller."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Handle settings changes."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                stream_url = _validate_stream_url(user_input[CONF_WS_URL])
            except vol.Invalid:
                errors[CONF_WS_URL] = "invalid_stream_url"
            else:
                user_input[CONF_WS_URL] = stream_url
                user_input[CONF_SENSOR_PREFIX] = user_input[CONF_SENSOR_PREFIX].strip() or DEFAULT_SENSOR_PREFIX
                user_input[CONF_USERNAME] = user_input[CONF_USERNAME].strip()
                if not user_input[CONF_PASSWORD]:
                    user_input[CONF_PASSWORD] = self.config_entry.options.get(
                        CONF_PASSWORD,
                        self.config_entry.data.get(CONF_PASSWORD, ""),
                    )
                return self.async_create_entry(title="", data=user_input)

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_WS_URL,
                    default=self.config_entry.options.get(
                        CONF_WS_URL,
                        self.config_entry.data.get(CONF_WS_URL, ""),
                    ),
                ): str,
                vol.Optional(
                    CONF_USERNAME,
                    default=self.config_entry.options.get(
                        CONF_USERNAME,
                        self.config_entry.data.get(CONF_USERNAME, ""),
                    ),
                ): str,
                vol.Optional(CONF_PASSWORD, default=""): selector.TextSelector({"type": "password"}),
                vol.Optional(
                    CONF_SENSOR_PREFIX,
                    default=self.config_entry.options.get(CONF_SENSOR_PREFIX, DEFAULT_SENSOR_PREFIX),
                ): str,
                vol.Optional(
                    CONF_RECONNECT_INTERVAL,
                    default=self.config_entry.options.get(CONF_RECONNECT_INTERVAL, DEFAULT_RECONNECT_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=300)),
                vol.Optional(
                    CONF_PAYLOAD_LOG_LEVEL,
                    default=self.config_entry.options.get(CONF_PAYLOAD_LOG_LEVEL, DEFAULT_PAYLOAD_LOG_LEVEL),
                ): vol.In(PAYLOAD_LOG_LEVEL_OPTIONS),
            }
        )

        return self.async_show_form(step_id="init", data_schema=data_schema, errors=errors)
