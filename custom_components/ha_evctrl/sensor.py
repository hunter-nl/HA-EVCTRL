from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_GRID_PHASES, CONF_SENSOR_PREFIX, DOMAIN
from .data import EvCtrlDataUpdateCoordinator

UNIT_NORMALIZATION = {
    "m3": "m³",
    "ft3": "ft³",
}

SESSION_PRICE_ENTITY_IDS = (
    "input_number.electricity_export_t1_price",
    "input_number.electricity_export_t2_price",
)

THREE_PHASE_P1_SENSOR_KEYS = frozenset(
    {
        "current_l2",
        "current_l3",
        "voltage_l2",
        "voltage_l3",
        "power_plus_l1",
        "power_plus_l2",
        "power_plus_l3",
        "power_min_l1",
        "power_min_l2",
        "power_min_l3",
    }
)

ONE_PHASE_P1_SENSOR_KEYS = frozenset({"current_l1", "voltage_l1"})

THREE_PHASE_P1_SENSOR_ICONS = {
    "current_l2": "mdi:current-ac",
    "current_l3": "mdi:current-ac",
    "voltage_l2": "mdi:sine-wave",
    "voltage_l3": "mdi:sine-wave",
    "power_plus_l1": "mdi:transmission-tower-export",
    "power_plus_l2": "mdi:transmission-tower-export",
    "power_plus_l3": "mdi:transmission-tower-export",
    "power_min_l1": "mdi:transmission-tower-import",
    "power_min_l2": "mdi:transmission-tower-import",
    "power_min_l3": "mdi:transmission-tower-import",
}

SENSOR_ICONS = {
    "t1_plus": "mdi:transmission-tower-export",
    "t2_plus": "mdi:transmission-tower-export",
    "t1_min": "mdi:transmission-tower-import",
    "t2_min": "mdi:transmission-tower-import",
    "power_plus": "mdi:transmission-tower-export",
    "power_min": "mdi:transmission-tower-import",
    "current_l1": "mdi:current-ac",
    "current_l2": "mdi:current-ac",
    "current_l3": "mdi:current-ac",
    "voltage_l1": "mdi:sine-wave",
    "voltage_l2": "mdi:sine-wave",
    "voltage_l3": "mdi:sine-wave",
    "power_plus_l1": "mdi:transmission-tower-export",
    "power_plus_l2": "mdi:transmission-tower-export",
    "power_plus_l3": "mdi:transmission-tower-export",
    "power_min_l1": "mdi:transmission-tower-import",
    "power_min_l2": "mdi:transmission-tower-import",
    "power_min_l3": "mdi:transmission-tower-import",
    "gas": "mdi:meter-gas-outline",
    "gas_date": "mdi:calendar-clock",
    "tariff": "mdi:cash",
    "ev_meter_total": "mdi:ev-station",
    "ev_meter_power": "mdi:flash",
    "ev_meter_current": "mdi:current-ac",
    "ev_meter_voltage_l1": "mdi:sine-wave",
    "ev_meter_power_l1": "mdi:flash",
    "ev_meter_frequency": "mdi:sine-wave",
    "relais_state": "mdi:toggle-switch",
    "evse_mode": "mdi:ev-station",
    "evse_state": "mdi:ev-station",
    "evse_charge": "mdi:current-ac",
    "evse_temp": "mdi:thermometer",
    "evse_error": "mdi:alert-circle",
    "evse_set_charge": "mdi:current-ac",
    "evse_max_current": "mdi:current-ac",
    "evse_connected": "mdi:ev-plug-type2",
    "controller_datetime": "mdi:calendar-clock",
    "controller_date": "mdi:calendar",
    "controller_time": "mdi:clock-outline",
    "sunrise": "mdi:weather-sunset-up",
    "sunset": "mdi:weather-sunset-down",
    "controller_version": "mdi:tag-text",
    "p1_last_update": "mdi:update",
    "p1_sags": "mdi:chart-line-variant",
    "p1_swells": "mdi:chart-line-variant",
    "p1_failures": "mdi:alert",
    "p1_long_failures": "mdi:alert-octagon",
    "p1_failures_log": "mdi:format-list-bulleted",
    "session_charge": "mdi:battery-charging",
    "session_meter_begin": "mdi:counter",
    "session_meter_end": "mdi:counter",
    "session_duration": "mdi:timer-outline",
    "session_start": "mdi:clock-start",
    "session_end": "mdi:clock-end",
    "session_cost": "mdi:currency-eur",
}

FAILURE_LOG_ENTRY_PATTERN = re.compile(r"\((\d{12})([SW])\)\((\d+)\*s\)")
FAILURE_LOG_COUNT_PATTERN = re.compile(r"^\((\d+)\)")


def _normalize_key_name(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


@dataclass(frozen=True, kw_only=True)
class EvCtrlSensorEntityDescription(SensorEntityDescription):
    group: str
    key_path: tuple[str, ...]
    unit_path: tuple[str, ...] | None = None
    key_paths: tuple[tuple[str, ...], ...] = ()
    unit_paths: tuple[tuple[str, ...], ...] = ()


GROUP_P1 = "p1"
GROUP_EV_METER = "ev_meter"
GROUP_EV_CONTROLLER = "ev_controller"
GROUP_EV_SESSION = "ev_session"

GROUP_METADATA: dict[str, dict[str, str]] = {
    GROUP_P1: {
        "name": "P1",
        "model": "P1 Meter",
    },
    GROUP_EV_METER: {
        "name": "EV Meter",
        "model": "EV Energy Meter",
    },
    GROUP_EV_CONTROLLER: {
        "name": "EV Controller",
        "model": "ESP32 EV Controller",
    },
    GROUP_EV_SESSION: {
        "name": "EV Charge Session",
        "model": "Charge Session",
    },
}


SENSOR_DESCRIPTIONS: tuple[EvCtrlSensorEntityDescription, ...] = (
    EvCtrlSensorEntityDescription(
        key="t1_plus",
        name="P1 Energy Delivered T1",
        group=GROUP_P1,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        key_path=("T1Plus", "value"),
        unit_path=("T1Plus", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="t2_plus",
        name="P1 Energy Delivered T2",
        group=GROUP_P1,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        key_path=("T2Plus", "value"),
        unit_path=("T2Plus", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="t1_min",
        name="P1 Energy Returned T1",
        group=GROUP_P1,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        key_path=("T1Min", "value"),
        unit_path=("T1Min", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="t2_min",
        name="P1 Energy Returned T2",
        group=GROUP_P1,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        key_path=("T2Min", "value"),
        unit_path=("T2Min", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="power_plus",
        name="P1 Power Delivered",
        group=GROUP_P1,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("PowerPlus", "value"),
        unit_path=("PowerPlus", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="power_min",
        name="P1 Power Returned",
        group=GROUP_P1,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("PowerMin", "value"),
        unit_path=("PowerMin", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="current_l1",
        name="P1 Current L1",
        group=GROUP_P1,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("CurrentL1", "value"),
        unit_path=("CurrentL1", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="voltage_l1",
        name="P1 Voltage L1",
        group=GROUP_P1,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("VoltageL1", "value"),
        unit_path=("VoltageL1", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="current_l2",
        name="P1 Current L2",
        icon="mdi:current-ac",
        group=GROUP_P1,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("CurrentL2", "value"),
        unit_path=("CurrentL2", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="current_l3",
        name="P1 Current L3",
        icon="mdi:current-ac",
        group=GROUP_P1,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("CurrentL3", "value"),
        unit_path=("CurrentL3", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="voltage_l2",
        name="P1 Voltage L2",
        icon="mdi:sine-wave",
        group=GROUP_P1,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("VoltageL2", "value"),
        unit_path=("VoltageL2", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="voltage_l3",
        name="P1 Voltage L3",
        icon="mdi:sine-wave",
        group=GROUP_P1,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("VoltageL3", "value"),
        unit_path=("VoltageL3", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="power_plus_l1",
        name="P1 Power Delivered L1",
        group=GROUP_P1,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("PowerPlusL1", "value"),
        unit_path=("PowerPlusL1", "unit"),
        key_paths=(("PowerPlusL1",), ("PowerL1Plus", "value"), ("PowerL1Plus",)),
        unit_paths=(("PowerL1Plus", "unit"),),
    ),
    EvCtrlSensorEntityDescription(
        key="power_plus_l2",
        name="P1 Power Delivered L2",
        icon="mdi:transmission-tower-export",
        group=GROUP_P1,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("PowerPlusL2", "value"),
        unit_path=("PowerPlusL2", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="power_plus_l3",
        name="P1 Power Delivered L3",
        icon="mdi:transmission-tower-export",
        group=GROUP_P1,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("PowerPlusL3", "value"),
        unit_path=("PowerPlusL3", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="power_min_l1",
        name="P1 Power Returned L1",
        group=GROUP_P1,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("PowerMinL1", "value"),
        unit_path=("PowerMinL1", "unit"),
        key_paths=(("PowerMinL1",), ("PowerL1Min", "value"), ("PowerL1Min",)),
        unit_paths=(("PowerL1Min", "unit"),),
    ),
    EvCtrlSensorEntityDescription(
        key="power_min_l2",
        name="P1 Power Returned L2",
        icon="mdi:transmission-tower-import",
        group=GROUP_P1,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("PowerMinL2", "value"),
        unit_path=("PowerMinL2", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="power_min_l3",
        name="P1 Power Returned L3",
        icon="mdi:transmission-tower-import",
        group=GROUP_P1,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("PowerMinL3", "value"),
        unit_path=("PowerMinL3", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="gas",
        name="Gas Meter",
        group=GROUP_P1,
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        key_path=("Gas", "value"),
        unit_path=("Gas", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="gas_date",
        name="Gas Reading Timestamp",
        group=GROUP_P1,
        key_path=("GasDate",),
        key_paths=(
            ("GasDateTime",),
            ("Gas", "DateTime"),
            ("Gas", "Date"),
            ("GasDate",),
        ),
    ),
    EvCtrlSensorEntityDescription(
        key="tariff",
        name="Current Tariff",
        group=GROUP_P1,
        key_path=("Tariff",),
    ),
    EvCtrlSensorEntityDescription(
        key="ev_meter_total",
        name="EV Meter Total",
        group=GROUP_EV_METER,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        key_path=("EVMeter", "Total", "value"),
        unit_path=("EVMeter", "Total", "unit"),
        key_paths=(
            ("EVMeter", "Meter", "value"),
            ("EVMeter", "Meter"),
            ("EVMeter", "Total"),
            ("Meter", "Total", "value"),
            ("Meter", "Total"),
            ("EVMeterTotal", "value"),
            ("EVMeterTotal",),
        ),
        unit_paths=(
            ("EVMeter", "Meter", "unit"),
            ("Meter", "Total", "unit"),
            ("EVMeterTotal", "unit"),
        ),
    ),
    EvCtrlSensorEntityDescription(
        key="ev_meter_power",
        name="EV Meter Power",
        group=GROUP_EV_METER,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("EVMeter", "Power", "value"),
        unit_path=("EVMeter", "Power", "unit"),
        key_paths=(
            ("EVMeter", "PowerL1", "value"),
            ("EVMeter", "PowerL1"),
            ("EVMeter", "Power"),
            ("Meter", "Power", "value"),
            ("Meter", "Power"),
            ("EVMeterPower", "value"),
            ("EVMeterPower",),
        ),
        unit_paths=(
            ("EVMeter", "PowerL1", "unit"),
            ("Meter", "Power", "unit"),
            ("EVMeterPower", "unit"),
        ),
    ),
    EvCtrlSensorEntityDescription(
        key="ev_meter_current",
        name="EV Meter Current",
        group=GROUP_EV_METER,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("EVMeter", "Current", "value"),
        unit_path=("EVMeter", "Current", "unit"),
        key_paths=(
            ("EVMeter", "CurrentL1", "value"),
            ("EVMeter", "CurrentL1"),
            ("EVMeter", "Current"),
            ("Meter", "Current", "value"),
            ("Meter", "Current"),
            ("Current", "L1", "value"),
            ("EVMeterCurrent", "value"),
            ("EVMeterCurrent",),
        ),
        unit_paths=(
            ("EVMeter", "CurrentL1", "unit"),
            ("Meter", "Current", "unit"),
            ("Current", "L1", "unit"),
            ("EVMeterCurrent", "unit"),
        ),
    ),
    EvCtrlSensorEntityDescription(
        key="ev_meter_voltage_l1",
        name="EV Meter Voltage L1",
        group=GROUP_EV_METER,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("EVMeter", "VoltageL1", "value"),
        unit_path=("EVMeter", "VoltageL1", "unit"),
        key_paths=(
            ("EVMeter", "Voltage", "value"),
            (
                "EVMeter",
                "Voltage",
            ),
        ),
        unit_paths=(("EVMeter", "Voltage", "unit"),),
    ),
    EvCtrlSensorEntityDescription(
        key="ev_meter_power_l1",
        name="EV Meter Power L1",
        group=GROUP_EV_METER,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("EVMeter", "PowerL1", "value"),
        unit_path=("EVMeter", "PowerL1", "unit"),
        key_paths=(("EVMeter", "Power", "value"),),
        unit_paths=(("EVMeter", "Power", "unit"),),
    ),
    EvCtrlSensorEntityDescription(
        key="ev_meter_frequency",
        name="EV Meter Frequency",
        group=GROUP_EV_METER,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("EVMeter", "Frequency", "value"),
        unit_path=("EVMeter", "Frequency", "unit"),
        key_paths=(
            (
                "EVMeter",
                "Frequency",
            ),
        ),
    ),
    EvCtrlSensorEntityDescription(
        key="relais_state",
        name="Relais State",
        group=GROUP_EV_CONTROLLER,
        key_path=("RelaisState",),
    ),
    EvCtrlSensorEntityDescription(
        key="evse_mode",
        name="EVSE Mode",
        group=GROUP_EV_CONTROLLER,
        key_path=("EVSE", "Mode"),
    ),
    EvCtrlSensorEntityDescription(
        key="evse_state",
        name="EVSE State",
        group=GROUP_EV_CONTROLLER,
        key_path=("EVSE", "State"),
    ),
    EvCtrlSensorEntityDescription(
        key="evse_charge",
        name="EVSE Charge Current",
        group=GROUP_EV_CONTROLLER,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("EVSE", "Charge", "value"),
        unit_path=("EVSE", "Charge", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="evse_temp",
        name="EVSE Temperature",
        group=GROUP_EV_CONTROLLER,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("EVSE", "Temp", "value"),
        unit_path=("EVSE", "Temp", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="evse_error",
        name="EVSE Error",
        group=GROUP_EV_CONTROLLER,
        key_path=("EVSE", "Error"),
    ),
    EvCtrlSensorEntityDescription(
        key="evse_set_charge",
        name="EVSE Set Charge Current",
        group=GROUP_EV_CONTROLLER,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("EVSE", "SetCharge", "value"),
        unit_path=("EVSE", "SetCharge", "unit"),
        key_paths=(
            ("EVSE", "Charge", "value"),
            ("EVSE", "Charge"),
            ("EVSE", "SetCharge"),
            ("EVSE", "SetCurrent", "value"),
            ("EVSE", "SetCurrent"),
            ("SetCharge", "value"),
            ("SetCharge",),
        ),
        unit_paths=(
            ("EVSE", "Charge", "unit"),
            ("EVSE", "SetCurrent", "unit"),
            ("SetCharge", "unit"),
        ),
    ),
    EvCtrlSensorEntityDescription(
        key="evse_max_current",
        name="EVSE Max Current",
        group=GROUP_EV_CONTROLLER,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("EVSE", "MaxCurrent", "value"),
        unit_path=("EVSE", "MaxCurrent", "unit"),
        key_paths=(
            ("EVSE", "Charge", "value"),
            ("EVSE", "Charge"),
            ("EVSE", "MaxCurrent"),
            ("EVSE", "MaxCharge", "value"),
            ("EVSE", "MaxCharge"),
            ("MaxCurrent", "value"),
            ("MaxCurrent",),
        ),
        unit_paths=(
            ("EVSE", "Charge", "unit"),
            ("EVSE", "MaxCharge", "unit"),
            ("MaxCurrent", "unit"),
        ),
    ),
    EvCtrlSensorEntityDescription(
        key="evse_connected",
        name="EVSE Connected",
        group=GROUP_EV_CONTROLLER,
        key_path=("EVSE", "Connected"),
    ),
    EvCtrlSensorEntityDescription(
        key="controller_datetime",
        name="Controller Date Time",
        group=GROUP_EV_CONTROLLER,
        key_path=("DateTime",),
        key_paths=(("System", "DateTime"), ("Datetime",), ("Now",), ("Date",)),
    ),
    EvCtrlSensorEntityDescription(
        key="controller_date",
        name="Controller Date",
        group=GROUP_EV_CONTROLLER,
        key_path=("Date",),
        key_paths=(("System", "Date"),),
    ),
    EvCtrlSensorEntityDescription(
        key="controller_time",
        name="Controller Time",
        group=GROUP_EV_CONTROLLER,
        key_path=("Time",),
        key_paths=(("System", "Time"),),
    ),
    EvCtrlSensorEntityDescription(
        key="sunrise",
        name="Sunrise",
        group=GROUP_EV_CONTROLLER,
        key_path=("Sunrise",),
        key_paths=(("Sun", "Rise"), ("SunRise",), ("Solar", "Sunrise")),
    ),
    EvCtrlSensorEntityDescription(
        key="sunset",
        name="Sunset",
        group=GROUP_EV_CONTROLLER,
        key_path=("Sunset",),
        key_paths=(("Sun", "Set"), ("SunSet",), ("Solar", "Sunset")),
    ),
    EvCtrlSensorEntityDescription(
        key="controller_version",
        name="Controller Version",
        group=GROUP_EV_CONTROLLER,
        key_path=("Version",),
        key_paths=(("System", "Version"), ("Firmware", "Version")),
    ),
    EvCtrlSensorEntityDescription(
        key="p1_last_update",
        name="P1 Last Update",
        group=GROUP_P1,
        key_path=("P1LastUpdate",),
        key_paths=(
            ("P1", "LastUpdate"),
            ("PowerDate",),
            ("P1Date",),
            ("P1Time",),
        ),
    ),
    EvCtrlSensorEntityDescription(
        key="p1_sags",
        name="P1 Sags",
        group=GROUP_P1,
        key_path=("Sags",),
        key_paths=(
            ("sags",),
            ("PowerQuality", "Sags"),
            ("PowerQuality", "Sags", "value"),
            ("VoltageSags",),
            ("Sags", "value"),
        ),
    ),
    EvCtrlSensorEntityDescription(
        key="p1_swells",
        name="P1 Swells",
        group=GROUP_P1,
        key_path=("Swells",),
        key_paths=(
            ("swells",),
            ("PowerQuality", "Swells"),
            ("PowerQuality", "Swells", "value"),
            ("VoltageSwells",),
            ("Swells", "value"),
        ),
    ),
    EvCtrlSensorEntityDescription(
        key="p1_failures",
        name="P1 Failures",
        group=GROUP_P1,
        key_path=("Failures",),
        key_paths=(
            ("failures",),
            ("PowerQuality", "Failures"),
            ("PowerQuality", "Failures", "value"),
            ("Failures", "value"),
        ),
    ),
    EvCtrlSensorEntityDescription(
        key="p1_long_failures",
        name="P1 Long Failures",
        group=GROUP_P1,
        key_path=("LongFailures",),
        key_paths=(
            ("long_failures",),
            ("PowerQuality", "LongFailures"),
            ("PowerQuality", "LongFailures", "value"),
            ("LongFailures", "value"),
        ),
    ),
    EvCtrlSensorEntityDescription(
        key="p1_failures_log",
        name="P1 Failures Log",
        group=GROUP_P1,
        key_path=("FailuresLog",),
        key_paths=(
            ("failure_log",),
            ("PowerQuality", "FailuresLog"),
            ("Failures", "Log"),
            ("PowerFailuresLog",),
            ("PowerQuality", "Log"),
        ),
    ),
    EvCtrlSensorEntityDescription(
        key="session_charge",
        name="Session Charge",
        group=GROUP_EV_SESSION,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        key_path=("Session", "Charge", "value"),
        unit_path=("Session", "Charge", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="session_meter_begin",
        name="Session Meter Start",
        group=GROUP_EV_SESSION,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        key_path=("Session", "MeterBegin", "value"),
        unit_path=("Session", "MeterBegin", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="session_meter_end",
        name="Session Meter End",
        group=GROUP_EV_SESSION,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        key_path=("Session", "MeterEnd", "value"),
        unit_path=("Session", "MeterEnd", "unit"),
    ),
    EvCtrlSensorEntityDescription(
        key="session_duration",
        name="Session Duration",
        group=GROUP_EV_SESSION,
        key_path=("Session", "Duration"),
        key_paths=(
            ("Session", "Duration", "value"),
            ("Session", "Time"),
            ("SessionDuration",),
        ),
    ),
    EvCtrlSensorEntityDescription(
        key="session_start",
        name="Session Start",
        group=GROUP_EV_SESSION,
        key_path=("Session", "Start"),
        key_paths=(("Session", "Begin"),),
    ),
    EvCtrlSensorEntityDescription(
        key="session_end",
        name="Session End",
        group=GROUP_EV_SESSION,
        key_path=("Session", "End"),
    ),
    EvCtrlSensorEntityDescription(
        key="session_cost",
        name="Session Cost",
        group=GROUP_EV_SESSION,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        key_path=("Session", "Cost", "value"),
        unit_path=("Session", "Cost", "unit"),
        key_paths=(("Session", "Cost"), ("SessionCost", "value"), ("SessionCost",)),
        unit_paths=(("SessionCost", "unit"),),
    ),
)


class EvCtrlSensor(CoordinatorEntity, SensorEntity):
    entity_description: EvCtrlSensorEntityDescription

    def __init__(
        self,
        coordinator: EvCtrlDataUpdateCoordinator,
        description: EvCtrlSensorEntityDescription,
        prefix: str,
        entry_id: str,
        grid_phases: int,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        assert isinstance(description.name, str)
        self._attr_name = description.name
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_icon = SENSOR_ICONS[description.key]
        if description.key in THREE_PHASE_P1_SENSOR_KEYS:
            self._attr_entity_registry_enabled_default = grid_phases == 3
        group_meta = GROUP_METADATA[description.group]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{description.group}")},
            name=f"{prefix} {group_meta['name']}",
            manufacturer="HA EV Control",
            model=group_meta["model"],
        )

    @property
    def native_value(self) -> Any | None:
        if self.entity_description.key == "controller_datetime":
            return self._compose_datetime(("Date",), ("Time",), ("P1DST",))
        if self.entity_description.key == "gas_date":
            return self._compose_datetime(("GasDate",), ("GasTime",), ("GasDST",))
        if self.entity_description.key == "p1_last_update":
            return self._compose_datetime(("P1Date",), ("P1Time",), ("P1DST",))
        if self.entity_description.key == "session_duration":
            return self._session_duration()
        if self.entity_description.key == "session_cost":
            return self._session_cost()
        value = self._extract_first(
            self.entity_description.key_path,
            self.entity_description.key_paths,
        )
        if self.entity_description.key == "p1_failures_log":
            return _format_failure_log(value)
        return value

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.key == "session_cost":
            return "EUR"
        if self.entity_description.unit_path is None:
            return self.entity_description.native_unit_of_measurement
        unit = self._extract_first(
            self.entity_description.unit_path,
            self.entity_description.unit_paths,
        )
        if not isinstance(unit, str):
            return unit
        return UNIT_NORMALIZATION.get(unit, unit)

    async def async_added_to_hass(self) -> None:
        """Refresh the session cost when one of its price helpers changes."""
        await super().async_added_to_hass()
        if self.entity_description.key != "session_cost":
            return
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                SESSION_PRICE_ENTITY_IDS,
                self._async_price_changed,
            )
        )

    @callback
    def _async_price_changed(self, _event) -> None:
        self.async_write_ha_state()

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
        if isinstance(node, list):
            return " | ".join(str(item) for item in node)
        if not isinstance(node, dict):
            return node
        if "value" in node:
            return node.get("value")
        if "Value" in node:
            return node.get("Value")
        date_value = node.get("date") or node.get("Date")
        time_value = node.get("time") or node.get("Time")
        if date_value is not None and time_value is not None:
            return f"{date_value} {time_value}"
        datetime_value = node.get("datetime") or node.get("DateTime")
        if datetime_value is not None:
            return datetime_value
        if len(node) == 1:
            single_value = next(iter(node.values()))
            return self._normalize_node(single_value)
        return str(node)

    def _compose_datetime(
        self,
        date_path: tuple[str, ...],
        time_path: tuple[str, ...],
        zone_path: tuple[str, ...] | None = None,
    ) -> str | None:
        date_value = self._extract_value(date_path)
        time_value = self._extract_value(time_path)
        zone_value = self._extract_value(zone_path) if zone_path else None
        if date_value and time_value and zone_value:
            return f"{date_value} {time_value} {zone_value}"
        if date_value and time_value:
            return f"{date_value} {time_value}"
        if date_value:
            return str(date_value)
        return None

    def _session_duration(self) -> str | None:
        start = self._extract_value(("Session", "Start"))
        end = self._extract_value(("Session", "End"))
        if not start or not end:
            return self._extract_first(
                self.entity_description.key_path,
                self.entity_description.key_paths,
            )
        try:
            start_dt = datetime.strptime(str(start), "%Y-%m-%d %H:%M:%S")
            end_dt = datetime.strptime(str(end), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

        delta = end_dt - start_dt
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return None
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02}"

    def _session_cost(self) -> float | None:
        charge = self._extract_value(("Session", "Charge", "value"))
        if charge is None:
            return None
        prices = [self.hass.states.get(entity_id) for entity_id in SESSION_PRICE_ENTITY_IDS]
        if any(price is None for price in prices):
            return None
        try:
            charge_value = Decimal(str(charge))
            price_values = [Decimal(price.state) for price in prices if price is not None]
        except InvalidOperation, ValueError:
            return None
        if len(price_values) != len(SESSION_PRICE_ENTITY_IDS):
            return None
        average_price = sum(price_values, Decimal(0)) / Decimal(2)
        return float((charge_value * average_price).quantize(Decimal("0.01"), rounding=ROUND_CEILING))


async def async_setup_entry(
    hass,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    prefix = entry_data[CONF_SENSOR_PREFIX]
    coordinator = entry_data["coordinator"]
    grid_phases = entry_data[CONF_GRID_PHASES]
    _sync_entity_registry_icons(hass, entry.entry_id)
    _sync_phase_entity_registry(hass, entry.entry_id, grid_phases)

    async_add_entities(
        EvCtrlSensor(coordinator, description, prefix, entry.entry_id, grid_phases)
        for description in SENSOR_DESCRIPTIONS
    )


def _sync_phase_entity_registry(hass, entry_id: str, grid_phases: int) -> None:
    """Keep integration-disabled phase entities aligned with the grid setting."""
    registry = er.async_get(hass)
    for key in THREE_PHASE_P1_SENSOR_KEYS | ONE_PHASE_P1_SENSOR_KEYS:
        unique_id = f"{entry_id}_{key}"
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is None:
            continue
        registry_entry = registry.async_get(entity_id)
        if registry_entry is None:
            continue
        updates: dict[str, Any] = {}
        if icon := THREE_PHASE_P1_SENSOR_ICONS.get(key):
            updates["original_icon"] = icon
        if registry_entry.name is None:
            updates["original_name"] = next(
                description.name for description in SENSOR_DESCRIPTIONS if description.key == key
            )
        if key in THREE_PHASE_P1_SENSOR_KEYS and grid_phases == 1 and registry_entry.disabled_by is None:
            updates["disabled_by"] = er.RegistryEntryDisabler.INTEGRATION
        elif registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
            updates["disabled_by"] = None
        if updates:
            registry.async_update_entity(entity_id, **updates)


def _sync_entity_registry_icons(hass, entry_id: str) -> None:
    """Persist integration icons, including for disabled entities."""
    registry = er.async_get(hass)
    for key, icon in SENSOR_ICONS.items():
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_{key}")
        if entity_id is None:
            continue
        registry_entry = registry.async_get(entity_id)
        if registry_entry is None or registry_entry.original_icon == icon:
            continue
        registry.async_update_entity(entity_id, original_icon=icon)


def _format_failure_log(value: Any | None) -> str | None:
    """Format DSMR power failure events into a readable sensor state."""
    if not isinstance(value, str):
        return None if value is None else str(value)

    entries = FAILURE_LOG_ENTRY_PATTERN.findall(value)
    if not entries:
        return value

    count_match = FAILURE_LOG_COUNT_PATTERN.match(value)
    count = count_match.group(1) if count_match else str(len(entries))
    lines = [f"FAILURES LOG ({count})"]
    for timestamp, season_code, duration in entries:
        try:
            occurred_at = datetime.strptime(f"20{timestamp}", "%Y%m%d%H%M%S")
        except ValueError:
            return value
        season = "Summer" if season_code == "S" else "Winter"
        lines.append(f"{occurred_at:%Y-%m-%d %H:%M:%S} {season} -> {int(duration)} sec")
    return "\n".join(lines)
