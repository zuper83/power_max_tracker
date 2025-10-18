# custom_components/power_max_tracker/sensor.py

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN  # If you don't have const.py, you can inline DOMAIN = "power_max_tracker"
from .coordinator import PowerMaxCoordinator


# Fallback in case const.py is not present
try:
    DOMAIN  # type: ignore  # noqa: F401
except NameError:
    DOMAIN = "power_max_tracker"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up all sensors for a single config entry.

    IMPORTANT:
    - Entities are added EXACTLY ONCE here.
    - The coordinator MUST NOT call async_add_entities().
    """
    data = hass.data.setdefault(DOMAIN, {}).get(entry.entry_id)
    if not data or "coordinator" not in data:
        # If __init__.py stored the coordinator differently, adjust the access here.
        # Expected shape: hass.data[DOMAIN][entry.entry_id] = {"coordinator": PowerMaxCoordinator}
        raise RuntimeError("Coordinator not found in hass.data")

    coordinator: PowerMaxCoordinator = data["coordinator"]

    entities: list[SensorEntity] = []

    # Hourly average power (kW) for the ongoing hour
    entities.append(HourlyAveragePowerSensor(coordinator, entry_id=entry.entry_id))

    # Average of the max hourly averages (kW)
    entities.append(AverageMaxHourlyAveragePowerSensor(coordinator, entry_id=entry.entry_id))

    # Top-N max hourly average power sensors (kW)
    for idx in range(1, coordinator.num_max_values + 1):
        entities.append(MaxHourlyAveragePowerSensor(coordinator, index=idx, entry_id=entry.entry_id))

    # Optional source instantaneous power (W)
    entities.append(SourcePowerSensor(coordinator, entry_id=entry.entry_id))

    # Register all entities ONCE
    async_add_entities(entities, update_before_add=False)

    # Mark as added so no other part of the integration attempts to add again
    coordinator.mark_entities_added()


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class _PowerBaseSensor(CoordinatorEntity[PowerMaxCoordinator], SensorEntity):
    """Base sensor binding the entity to the PowerMaxCoordinator.

    - Provides common device_info and availability.
    - Child classes set name, unique_id, device_class, state_class, unit, etc.
    """

    _attr_has_entity_name = True  # Let HA build friendly names from device + entity

    def __init__(self, coordinator: PowerMaxCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id

        # Device information groups all sensors under one device in HA UI
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "manufacturer": "Power Max Tracker",
            "name": "Power Max Tracker",
            "model": "Hourly/Max Average Power",
        }

    @property
    def available(self) -> bool:
        """Entity is available when the coordinator is running (best-effort)."""
        # If you'd like stricter checks, base this on snapshot content.
        return True

    # Common helper to expose interval attributes for sensors that want them
    def _interval_attrs(self) -> dict[str, Any]:
        start: Optional[datetime] = self.coordinator.interval_start
        end: Optional[datetime] = self.coordinator.interval_end

        # ISO 8601 is a good default for attributes
        start_iso = start.isoformat(timespec="minutes") if start else None
        end_iso = end.isoformat(timespec="minutes") if end else None

        label: Optional[str] = None
        if start and end:
            # Example: 2025-10-04 07:00–08:00
            label = f"{start:%Y-%m-%d %H:%M}–{end:%H:%M}"

        return {
            "interval_start": start_iso,
            "interval_end": end_iso,
            "interval_label": label,
        }


# ---------------------------------------------------------------------------
# Concrete sensor entities
# ---------------------------------------------------------------------------


class HourlyAveragePowerSensor(_PowerBaseSensor):
    """Current hour's average power in kW."""

    _attr_name = "Hourly Average Power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"

    def __init__(self, coordinator: PowerMaxCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_hourly_average_power"

    @property
    def native_value(self) -> float | None:
        """Return the average power (kW) for the ongoing hour."""
        val = self.coordinator.hourly_average_kw
        if val is None:
            return None
        # Round sensibly for UI; keep more precision if your data justifies it
        return round(float(val), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose interval attributes to aid automations and dashboards."""
        return self._interval_attrs()


class AverageMaxHourlyAveragePowerSensor(_PowerBaseSensor):
    """Average of the top-N max hourly average power values (kW)."""

    _attr_name = "Average Max Hourly Average Power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"

    def __init__(self, coordinator: PowerMaxCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_avg_max_hourly_average_power"

    @property
    def native_value(self) -> float | None:
        """Return the average of the coordinator's max values list."""
        return self.coordinator.compute_average_of_max()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the underlying list to make reasoning in HA templates easier."""
        attrs = self._interval_attrs()
        # Provide the raw max list for transparency/debugging
        attrs["max_values_kw"] = list(self.coordinator.max_values_kw)
        return attrs


class MaxHourlyAveragePowerSensor(_PowerBaseSensor):
    """The Nth max hourly average power value (kW), where index starts at 1."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"

    def __init__(self, coordinator: PowerMaxCoordinator, index: int, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        if index < 1:
            raise ValueError("index must start at 1")
        self._index = index
        self._attr_name = f"Max Hourly Average Power #{index}"
        self._attr_unique_id = f"{entry_id}_max_hourly_average_power_{index}"

    @property
    def native_value(self) -> float | None:
        """Return the Nth element from the sorted max list if present."""
        values = self.coordinator.max_values_kw  # expected sorted desc
        if not values or self._index > len(values):
            return None
        return round(float(values[self._index - 1]), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose interval attributes and index for clarity."""
        attrs = self._interval_attrs()
        attrs["index"] = self._index
        return attrs


class SourcePowerSensor(_PowerBaseSensor):
    """Instantaneous source power in W (optional mirror of a live sensor)."""

    _attr_name = "Source Power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "W"

    def __init__(self, coordinator: PowerMaxCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_power_max_source"

    @property
    def native_value(self) -> float | None:
        """Return instantaneous source power in W, if provided by the coordinator."""
        val = self.coordinator.source_power_w
        if val is None:
            return None
        return round(float(val), 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose interval attributes to align with other sensors."""
        return self._interval_attrs()
