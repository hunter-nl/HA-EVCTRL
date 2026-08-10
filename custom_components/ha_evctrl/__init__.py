from __future__ import annotations

import contextlib
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, callback

from . import const as evctrl_const
from . import data as evctrl_data

DOMAIN = evctrl_const.DOMAIN
CONF_WS_URL = evctrl_const.CONF_WS_URL
CONF_RECONNECT_INTERVAL = evctrl_const.CONF_RECONNECT_INTERVAL
CONF_SENSOR_PREFIX = evctrl_const.CONF_SENSOR_PREFIX
CONF_PAYLOAD_LOG_LEVEL = getattr(evctrl_const, "CONF_PAYLOAD_LOG_LEVEL", "payload_log_level")
CONF_GRID_PHASES = evctrl_const.CONF_GRID_PHASES
DEFAULT_RECONNECT_INTERVAL = evctrl_const.DEFAULT_RECONNECT_INTERVAL
DEFAULT_SENSOR_PREFIX = evctrl_const.DEFAULT_SENSOR_PREFIX
DEFAULT_PAYLOAD_LOG_LEVEL = getattr(evctrl_const, "DEFAULT_PAYLOAD_LOG_LEVEL", "off")
DEFAULT_GRID_PHASES = evctrl_const.DEFAULT_GRID_PHASES

PLATFORMS = ["sensor", "binary_sensor"]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    coordinator = evctrl_data.EvCtrlDataUpdateCoordinator(hass, entry.entry_id)
    stream_url = entry.options.get(CONF_WS_URL, entry.data[CONF_WS_URL]).strip()
    reconnect_interval = entry.options.get(CONF_RECONNECT_INTERVAL, DEFAULT_RECONNECT_INTERVAL)
    payload_log_level = entry.options.get(CONF_PAYLOAD_LOG_LEVEL, DEFAULT_PAYLOAD_LOG_LEVEL)
    client = evctrl_data.create_evctrl_client(
        hass,
        stream_url,
        coordinator,
        reconnect_interval,
        payload_log_level,
    )
    _LOGGER.info(
        "Using %s transport for %s",
        client.__class__.__name__,
        stream_url,
    )
    if payload_log_level != "off":
        _LOGGER.warning(
            "Payload logging is enabled (%s) for %s",
            payload_log_level,
            stream_url,
        )

    integration_data = {
        "client": client,
        "start_unsub": None,
        "coordinator": coordinator,
        CONF_SENSOR_PREFIX: entry.options.get(CONF_SENSOR_PREFIX, DEFAULT_SENSOR_PREFIX),
        CONF_GRID_PHASES: entry.options.get(CONF_GRID_PHASES, DEFAULT_GRID_PHASES),
    }
    hass.data[DOMAIN][entry.entry_id] = integration_data

    if hass.is_running:
        client.start()
    else:

        @callback
        def _async_start_client(_event) -> None:
            # async_listen_once removes its listener before calling us.  Do not
            # try to remove that already-consumed listener during unload.
            integration_data["start_unsub"] = None
            client.start()

        integration_data["start_unsub"] = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED,
            _async_start_client,
        )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    integration_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
    start_unsub = integration_data.get("start_unsub")
    if start_unsub is not None:
        with contextlib.suppress(ValueError):
            start_unsub()
    client = integration_data.get("client")
    if client is not None:
        await client.stop()

    if not hass.data.get(DOMAIN):
        hass.data.pop(DOMAIN, None)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
