from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import urlparse

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import const as evctrl_const

DEFAULT_RECONNECT_INTERVAL = evctrl_const.DEFAULT_RECONNECT_INTERVAL
STREAM_INACTIVITY_TIMEOUT = getattr(evctrl_const, "STREAM_INACTIVITY_TIMEOUT", 120)
STREAM_NOTIFICATION_TIMEOUT = getattr(evctrl_const, "STREAM_NOTIFICATION_TIMEOUT", 300)
INFO_PAYLOAD_LOG_INTERVAL = 300
DEFAULT_PAYLOAD_LOG_LEVEL = getattr(evctrl_const, "DEFAULT_PAYLOAD_LOG_LEVEL", "off")
PAYLOAD_LOG_LEVEL_INFO = getattr(evctrl_const, "PAYLOAD_LOG_LEVEL_INFO", "info")
PAYLOAD_LOG_LEVEL_DEBUG = getattr(evctrl_const, "PAYLOAD_LOG_LEVEL_DEBUG", "debug")

_LOGGER = logging.getLogger(__name__)


class StreamInactivityError(TimeoutError):
    """Raised when a connected stream stops delivering telemetry."""


class StreamHealthMonitor:
    """Track valid telemetry and notify once for an extended outage."""

    def __init__(self, hass: HomeAssistant, stream_name: str) -> None:
        self._hass = hass
        self._stream_name = stream_name
        self._outage_started_at: float | None = None
        self._last_valid_payload_at: float | None = None
        self._notification_sent = False
        self._notification_id = f"{evctrl_const.DOMAIN}_{stream_name}_unavailable"

    def start(self) -> None:
        self._outage_started_at = time.monotonic()
        self._last_valid_payload_at = None
        self._notification_sent = False

    def stop(self) -> None:
        persistent_notification.async_dismiss(self._hass, self._notification_id)

    def note_valid_payload(self) -> None:
        self._last_valid_payload_at = time.monotonic()
        self._outage_started_at = None
        if self._notification_sent:
            persistent_notification.async_dismiss(self._hass, self._notification_id)
            _LOGGER.info("EV telemetry stream recovered")
        self._notification_sent = False

    def note_stream_interrupted(self) -> None:
        """Start an outage at the last valid payload, if one exists."""
        if self._outage_started_at is None:
            self._outage_started_at = self._last_valid_payload_at or time.monotonic()

    def notify_if_unavailable(self) -> None:
        if self._outage_started_at is None or self._notification_sent:
            return
        if time.monotonic() - self._outage_started_at < STREAM_NOTIFICATION_TIMEOUT:
            return

        persistent_notification.async_create(
            self._hass,
            (
                "No valid telemetry JSON has been received for at least "
                f"{STREAM_NOTIFICATION_TIMEOUT // 60} minutes. "
                "The integration will continue reconnecting automatically."
            ),
            title="EV Controller unavailable",
            notification_id=self._notification_id,
        )
        self._notification_sent = True
        _LOGGER.warning("No valid EV telemetry for %s seconds", STREAM_NOTIFICATION_TIMEOUT)


class EvCtrlDataUpdateCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{entry_id}-data",
            update_interval=None,
        )
        self.data: dict[str, object] = {}
        self._last_info_payload_log_at = 0.0

    def should_log_info_payload(self) -> bool:
        """Rate-limit the informational telemetry-flow message."""
        now = time.monotonic()
        if now - self._last_info_payload_log_at < INFO_PAYLOAD_LOG_INTERVAL:
            return False
        self._last_info_payload_log_at = now
        return True

    async def _async_update_data(self) -> dict[str, object]:
        return self.data


class EvCtrlWebsocketClient:
    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        coordinator: EvCtrlDataUpdateCoordinator,
        reconnect_interval: int | None = None,
        payload_log_level: str = DEFAULT_PAYLOAD_LOG_LEVEL,
        username: str = "",
        password: str = "",
    ) -> None:
        self._hass = hass
        self._url = url
        self._coordinator = coordinator
        self._session = aiohttp_client.async_get_clientsession(hass)
        self._task: asyncio.Task[None] | None = None
        self._reconnect_interval = max(
            1,
            reconnect_interval if reconnect_interval is not None else DEFAULT_RECONNECT_INTERVAL,
        )
        self._is_running = False
        self._payload_log_level = payload_log_level
        self._auth = aiohttp.BasicAuth(username, password) if username else None
        self._health = StreamHealthMonitor(hass, f"{coordinator.name}_websocket")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._is_running = True
            self._health.start()
            self._hass.add_job(self._async_start)

    @callback
    def _async_start(self) -> None:
        if self._task is None or self._task.done():
            self._task = self._hass.async_create_task(self._run())

    async def stop(self) -> None:
        self._is_running = False
        self._health.stop()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run(self) -> None:
        while self._is_running:
            try:
                _LOGGER.debug("Connecting to websocket %s", self._url)
                async with self._session.ws_connect(
                    self._url,
                    auth=self._auth,
                    heartbeat=30,
                ) as ws:
                    last_payload_at = asyncio.get_running_loop().time()
                    while self._is_running:
                        remaining = STREAM_INACTIVITY_TIMEOUT - (asyncio.get_running_loop().time() - last_payload_at)
                        if remaining <= 0:
                            raise StreamInactivityError(
                                f"No valid telemetry received for {STREAM_INACTIVITY_TIMEOUT} seconds"
                            )
                        msg = await ws.receive(timeout=remaining)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            if _process_payload(
                                self._coordinator,
                                msg.data,
                                self._payload_log_level,
                            ):
                                last_payload_at = asyncio.get_running_loop().time()
                                self._health.note_valid_payload()
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning(
                    "Websocket connection ended (%s), reconnecting in %s seconds",
                    err,
                    self._reconnect_interval,
                )
            self._health.note_stream_interrupted()
            self._health.notify_if_unavailable()
            await asyncio.sleep(self._reconnect_interval)


class EvCtrlEventSourceClient:
    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        coordinator: EvCtrlDataUpdateCoordinator,
        reconnect_interval: int | None = None,
        payload_log_level: str = DEFAULT_PAYLOAD_LOG_LEVEL,
        username: str = "",
        password: str = "",
    ) -> None:
        self._hass = hass
        self._url = url
        self._coordinator = coordinator
        self._session = aiohttp_client.async_get_clientsession(hass)
        self._task: asyncio.Task[None] | None = None
        self._headers = {"Accept": "text/event-stream"}
        self._auth = aiohttp.BasicAuth(username, password) if username else None
        self._reconnect_interval = max(
            1,
            reconnect_interval if reconnect_interval is not None else DEFAULT_RECONNECT_INTERVAL,
        )
        self._is_running = False
        self._payload_log_level = payload_log_level
        self._health = StreamHealthMonitor(hass, f"{coordinator.name}_sse")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._is_running = True
            self._health.start()
            self._hass.add_job(self._async_start)

    @callback
    def _async_start(self) -> None:
        if self._task is None or self._task.done():
            self._task = self._hass.async_create_task(self._run())

    async def stop(self) -> None:
        self._is_running = False
        self._health.stop()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run(self) -> None:
        while self._is_running:
            try:
                _LOGGER.debug("Connecting to SSE stream %s", self._url)
                timeout = aiohttp.ClientTimeout(total=None)
                async with self._session.get(
                    self._url,
                    headers=self._headers,
                    auth=self._auth,
                    timeout=timeout,
                ) as response:
                    response.raise_for_status()
                    await self._read_events(response)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning(
                    "Event stream connection dropped (%s), reconnecting in %s seconds",
                    err,
                    self._reconnect_interval,
                )
            self._health.note_stream_interrupted()
            self._health.notify_if_unavailable()
            await asyncio.sleep(self._reconnect_interval)

    async def _read_events(self, response: aiohttp.ClientResponse) -> None:
        pending_data: list[str] = []
        last_payload_at = asyncio.get_running_loop().time()
        while self._is_running:
            remaining = STREAM_INACTIVITY_TIMEOUT - (asyncio.get_running_loop().time() - last_payload_at)
            if remaining <= 0:
                raise StreamInactivityError(f"No valid telemetry received for {STREAM_INACTIVITY_TIMEOUT} seconds")
            try:
                line_bytes = await asyncio.wait_for(response.content.readline(), timeout=remaining)
            except TimeoutError as err:
                raise StreamInactivityError(
                    f"No valid telemetry received for {STREAM_INACTIVITY_TIMEOUT} seconds"
                ) from err
            if not line_bytes:
                break
            try:
                line = line_bytes.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                continue
            if not line:
                if pending_data:
                    if await self._dispatch_event(pending_data):
                        last_payload_at = asyncio.get_running_loop().time()
                        self._health.note_valid_payload()
                    pending_data = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                pending_data.append(line[5:].lstrip())
        if pending_data:
            await self._dispatch_event(pending_data)

    async def _dispatch_event(self, pending_data: list[str]) -> bool:
        payload = "\n".join(pending_data).strip()
        if not payload:
            return False
        return _process_payload(self._coordinator, payload, self._payload_log_level)


def _process_payload(
    coordinator: EvCtrlDataUpdateCoordinator,
    payload: str,
    payload_log_level: str,
) -> bool:
    if payload_log_level == PAYLOAD_LOG_LEVEL_DEBUG:
        _LOGGER.debug("Incoming EV payload [debug]: %s", payload)

    try:
        data = json.loads(payload)
    except ValueError as err:
        _LOGGER.warning("Invalid JSON from stream: %s", err)
        return False

    if payload_log_level == PAYLOAD_LOG_LEVEL_INFO and coordinator.should_log_info_payload():
        _LOGGER.debug("Incoming valid EV telemetry [info]")
    elif payload_log_level == PAYLOAD_LOG_LEVEL_DEBUG and isinstance(data, dict):
        _LOGGER.debug("Incoming EV payload keys: %s", ", ".join(sorted(data.keys())))

    if not isinstance(data, dict):
        _LOGGER.warning("Ignoring telemetry payload that is not a JSON object")
        return False

    coordinator.async_set_updated_data(data)
    return True


def _is_websocket_url(url: str) -> bool:
    scheme = urlparse(url.strip()).scheme.lower()
    return scheme in {"ws", "wss"}


def create_evctrl_client(
    hass: HomeAssistant,
    url: str,
    coordinator: EvCtrlDataUpdateCoordinator,
    reconnect_interval: int | None = None,
    payload_log_level: str = DEFAULT_PAYLOAD_LOG_LEVEL,
    username: str = "",
    password: str = "",
) -> EvCtrlWebsocketClient | EvCtrlEventSourceClient:
    normalized_url = url.strip()
    parsed_url = urlparse(normalized_url)
    if parsed_url.scheme.lower() not in {"http", "https", "ws", "wss"} or not parsed_url.netloc:
        raise ValueError("Stream URL must be an absolute HTTP(S) or WebSocket URL")
    if _is_websocket_url(normalized_url):
        return EvCtrlWebsocketClient(
            hass,
            normalized_url,
            coordinator,
            reconnect_interval,
            payload_log_level,
            username,
            password,
        )
    return EvCtrlEventSourceClient(
        hass,
        normalized_url,
        coordinator,
        reconnect_interval,
        payload_log_level,
        username,
        password,
    )
