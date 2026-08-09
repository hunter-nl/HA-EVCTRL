from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_SENSOR_PREFIX, DOMAIN
from .data import EvCtrlDataUpdateCoordinator
from .sensor import GROUP_EV_CONTROLLER, GROUP_METADATA, _normalize_key_name


@dataclass(frozen=True, kw_only=True)
class EvCtrlBinarySensorEntityDescription(BinarySensorEntityDescription):
    group: str
    key_path: tuple[str, ...]
    key_paths: tuple[tuple[str, ...], ...] = ()


BINARY_SENSOR_DESCRIPTIONS: tuple[EvCtrlBinarySensorEntityDescription, ...] = (
    EvCtrlBinarySensorEntityDescription(
        key="door_state",
        name="Door State",
        device_class=BinarySensorDeviceClass.DOOR,
        group=GROUP_EV_CONTROLLER,
        key_path=("DoorState",),
    ),
    EvCtrlBinarySensorEntityDescription(
        key="update_available",
        name="Update Available",
        device_class=BinarySensorDeviceClass.UPDATE,
        group=GROUP_EV_CONTROLLER,
        key_path=("UpdateAvailable",),
        key_paths=(("System", "UpdateAvailable"), ("Firmware", "UpdateAvailable")),
    ),
)


class EvCtrlBinarySensor(CoordinatorEntity, BinarySensorEntity):
    entity_description: EvCtrlBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: EvCtrlDataUpdateCoordinator,
        description: EvCtrlBinarySensorEntityDescription,
        prefix: str,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_name = f"{prefix} {description.name}"
        self._attr_unique_id = f"{entry_id}_{description.key}"
        group_meta = GROUP_METADATA[description.group]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{description.group}")},
            name=f"{prefix} {group_meta['name']}",
            manufacturer="HA EV Control",
            model=group_meta["model"],
        )

    @property
    def is_on(self) -> bool | None:
        value = self._extract_first(
            self.entity_description.key_path,
            self.entity_description.key_paths,
        )
        return self._to_bool(value)

    def _extract_first(
        self,
        path: tuple[str, ...],
        fallback_paths: tuple[tuple[str, ...], ...],
    ) -> Any | None:
        value = self._extract_value(path)
        if value is not None:
            return value
        for candidate in fallback_paths:
            value = self._extract_value(candidate)
            if value is not None:
                return value
        return None

    def _extract_value(self, path: tuple[str, ...]) -> Any | None:
        data = self.coordinator.data
        if not data:
            return None
        node: Any = data
        for key in path:
            if not isinstance(node, dict):
                return None
            resolved_key = self._resolve_key(node, key)
            if resolved_key is None:
                return None
            node = node.get(resolved_key)
            if node is None:
                return None
        return self._normalize_node(node)

    def _resolve_key(self, node: dict[str, Any], key: str) -> str | None:
        if key in node:
            return key
        normalized_target = _normalize_key_name(key)
        for existing_key in node:
            if _normalize_key_name(existing_key) == normalized_target:
                return existing_key
        return None

    def _normalize_node(self, node: Any) -> Any | None:
        if not isinstance(node, dict):
            return node
        if "value" in node:
            return node.get("value")
        if "Value" in node:
            return node.get("Value")
        if len(node) == 1:
            return next(iter(node.values()))
        return None

    def _to_bool(self, value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if not isinstance(value, str):
            return None

        normalized = value.strip().lower().replace("_", " ")
        truthy = {"on", "true", "1", "open", "opened", "door open"}
        falsy = {"off", "false", "0", "closed", "close", "door closed"}

        if normalized in truthy:
            return True
        if normalized in falsy:
            return False
        return None


async def async_setup_entry(
    hass,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    prefix = entry_data[CONF_SENSOR_PREFIX]
    coordinator = entry_data["coordinator"]

    async_add_entities(
        EvCtrlBinarySensor(coordinator, description, prefix, entry.entry_id)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )
