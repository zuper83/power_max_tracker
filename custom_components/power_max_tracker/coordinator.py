# custom_components/power_max_tracker/coordinator.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta, datetime
from typing import Any, Iterable, Optional, Set

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry

DOMAIN = "power_max_tracker"

# Defaults / options keys
CONF_NUM_MAX_VALUES = "num_max_values"
CONF_UPDATE_INTERVAL_SECONDS = "update_interval_seconds"

DEFAULT_NUM_MAX_VALUES = 3
DEFAULT_UPDATE_INTERVAL = timedelta(minutes=1)


@dataclass
class PowerMaxSnapshot:
    """Immutable snapshot of values the sensors will expose.

    All values are optional so sensors can decide how to render 'unknown'
    (returning None) if data is not ready yet.
    """

    # Current hourly average power (kW) for the ongoing hour
    hourly_average_kw: Optional[float] = None

    # The N max hourly average power values (kW) found so far for the period
    max_values_kw: list[float] = field(default_factory=list)

    # Optional source instantaneous power (W) that some UIs like to show
    source_power_w: Optional[float] = None

    # Interval bookkeeping for attributes (start/end of the current hour)
    interval_start: Optional[datetime] = None
    interval_end: Optional[datetime] = None

    # Any additional attributes the sensors might want to surface
    extra: dict[str, Any] = field(default_factory=dict)


class PowerMaxCoordinator(DataUpdateCoordinator[PowerMaxSnapshot]):
    """Central coordinator for the Power Max Tracker integration.

    Responsibilities:
    - Hold configuration (e.g. number of max values).
    - Own the current 'snapshot' of values that sensors read.
    - Provide safe guards so entity registration only happens ONCE per entry.
    - DO NOT create or add entities from here; that belongs in sensor.py.
    """

    def __init__(self, hass: HomeAssistant, logger: logging.Logger, entry: ConfigEntry) -> None:
        self.hass = hass
        self.logger = logger
        self.entry = entry

        # Read options with sane fallbacks
        self.num_max_values: int = int(
            (entry.options or {}).get(CONF_NUM_MAX_VALUES, DEFAULT_NUM_MAX_VALUES)
        )

        update_seconds = (entry.options or {}).get(CONF_UPDATE_INTERVAL_SECONDS)
        if update_seconds is not None:
            try:
                update_interval = timedelta(seconds=int(update_seconds))
            except (TypeError, ValueError):
                update_interval = DEFAULT_UPDATE_INTERVAL
        else:
            update_interval = DEFAULT_UPDATE_INTERVAL

        # "Add once" guards to avoid duplicate entity registration
        self._entities_added: bool = False
        self._added_entity_ids: Set[str] = set()

        # Live data (always exposed as an immutable snapshot via .data)
        self._snapshot = PowerMaxSnapshot()

        super().__init__(
            hass=hass,
            logger=logger,
            name=f"{DOMAIN} Coordinator ({entry.entry_id})",
            update_interval=update_interval,
        )

    # ---------------------------------------------------------------------
    # Entity add guards
    # ---------------------------------------------------------------------

    def mark_entities_added(self) -> None:
        """Mark that platforms (e.g., sensor.py) have added entities once.

        Call this exactly once from sensor.py after async_add_entities(...).
        """
        self._entities_added = True
        self.logger.debug("Entities marked as added for entry_id=%s", self.entry.entry_id)

    @property
    def entities_added(self) -> bool:
        """Return True if entities have been added already."""
        return self._entities_added

    def should_add(self, unique_id: str) -> bool:
        """Return False if we've already attempted to add this unique_id.

        This is a best-effort guard to prevent accidental double additions.
        """
        if unique_id in self._added_entity_ids:
            self.logger.debug(
                "Skipping add for already known unique_id=%s (entry_id=%s)",
                unique_id,
                self.entry.entry_id,
            )
            return False
        self._added_entity_ids.add(unique_id)
        return True

    # ---------------------------------------------------------------------
    # Data API for sensors
    # ---------------------------------------------------------------------

    @property
    def max_values_kw(self) -> list[float]:
        """Return the top-N max hourly averages (kW)."""
        return list(self._snapshot.max_values_kw)

    @property
    def hourly_average_kw(self) -> Optional[float]:
        """Return the current hour's average power (kW)."""
        return self._snapshot.hourly_average_kw

    @property
    def source_power_w(self) -> Optional[float]:
        """Return current instantaneous source power (W), if available."""
        return self._snapshot.source_power_w

    @property
    def interval_start(self) -> Optional[datetime]:
        """Start of the current interval (usually the hour)."""
        return self._snapshot.interval_start

    @property
    def interval_end(self) -> Optional[datetime]:
        """End of the current interval (usually the hour)."""
        return self._snapshot.interval_end

    def compute_average_of_max(self) -> Optional[float]:
        """Compute the average of the max values (kW), used by avg sensor.

        Returns:
            Rounded float with 2 decimals, or None if no data yet.
        """
        values = self._snapshot.max_values_kw
        if not values:
            return None
        return round(sum(values) / len(values), 2)

    # ---------------------------------------------------------------------
    # Mutation helpers (to be called by logic/statistics code)
    # ---------------------------------------------------------------------

    def set_num_max_values(self, n: int) -> None:
        """Update how many max values to track and trim/extend storage accordingly."""
        n = max(1, int(n))
        self.num_max_values = n
        trimmed = sorted(self._snapshot.max_values_kw, reverse=True)[:n]
        self._snapshot = PowerMaxSnapshot(
            hourly_average_kw=self._snapshot.hourly_average_kw,
            max_values_kw=trimmed,
            source_power_w=self._snapshot.source_power_w,
            interval_start=self._snapshot.interval_start,
            interval_end=self._snapshot.interval_end,
            extra=self._snapshot.extra,
        )
        self.async_set_updated_data(self._snapshot)

    def update_hourly_average(
        self,
        avg_kw: Optional[float],
        interval_start: Optional[datetime],
        interval_end: Optional[datetime],
    ) -> None:
        """Set the current hour's average (kW) and hour bounds.

        Sensors should call this when the statistics for the ongoing hour change.
        """
        # Normalize values (defensive programming)
        if avg_kw is not None:
            try:
                avg_kw = float(avg_kw)
                if avg_kw < 0:
                    avg_kw = 0.0
            except (TypeError, ValueError):
                avg_kw = None

        self._snapshot = PowerMaxSnapshot(
            hourly_average_kw=avg_kw,
            max_values_kw=self._snapshot.max_values_kw,
            source_power_w=self._snapshot.source_power_w,
            interval_start=interval_start,
            interval_end=interval_end,
            extra=self._snapshot.extra,
        )
        self.async_set_updated_data(self._snapshot)

    def consider_for_max_values(self, candidate_kw: Optional[float]) -> None:
        """Push a new candidate hourly average (kW) into the top-N list if it belongs there."""
        if candidate_kw is None:
            return
        try:
            val = float(candidate_kw)
        except (TypeError, ValueError):
            return
        if val < 0:
            val = 0.0

        new_list = list(self._snapshot.max_values_kw)
        new_list.append(val)
        new_list = sorted(new_list, reverse=True)[: self.num_max_values]

        if new_list != self._snapshot.max_values_kw:
            self._snapshot = PowerMaxSnapshot(
                hourly_average_kw=self._snapshot.hourly_average_kw,
                max_values_kw=new_list,
                source_power_w=self._snapshot.source_power_w,
                interval_start=self._snapshot.interval_start,
                interval_end=self._snapshot.interval_end,
                extra=self._snapshot.extra,
            )
            self.async_set_updated_data(self._snapshot)

    def set_source_power_w(self, watts: Optional[float]) -> None:
        """Optionally store instantaneous source power (W) for UI visibility."""
        if watts is not None:
            try:
                watts = float(watts)
            except (TypeError, ValueError):
                watts = None

        self._snapshot = PowerMaxSnapshot(
            hourly_average_kw=self._snapshot.hourly_average_kw,
            max_values_kw=self._snapshot.max_values_kw,
            source_power_w=watts,
            interval_start=self._snapshot.interval_start,
            interval_end=self._snapshot.interval_end,
            extra=self._snapshot.extra,
        )
        self.async_set_updated_data(self._snapshot)

    def set_extra_attr(self, key: str, value: Any) -> None:
        """Set an arbitrary extra attribute (namespaced by caller to avoid clashes)."""
        extra = dict(self._snapshot.extra)
        extra[key] = value
        self._snapshot = PowerMaxSnapshot(
            hourly_average_kw=self._snapshot.hourly_average_kw,
            max_values_kw=self._snapshot.max_values_kw,
            source_power_w=self._snapshot.source_power_w,
            interval_start=self._snapshot.interval_start,
            interval_end=self._snapshot.interval_end,
            extra=extra,
        )
        self.async_set_updated_data(self._snapshot)

    # ---------------------------------------------------------------------
    # Coordinator update hook
    # ---------------------------------------------------------------------

    async def _async_update_data(self) -> PowerMaxSnapshot:
        """Periodic refresh.

        This coordinator does not fetch from an external API. Instead,
        it exists to:
          - publish a consistent snapshot to sensors
          - drive periodic HA updates so sensors refresh in the UI

        If you need to recompute anything on a schedule (e.g., hour rollover),
        do it here and return a new snapshot.
        """
        # Example: force interval_end to align to the end of the current hour if not set
        snap = self._snapshot

        # You could add hour-boundary checks here if your logic needs it.
        # For now, we simply return the latest snapshot as-is.
        return snap


# -------------------------------------------------------------------------
# Factory to create the coordinator in __init__.py or sensor.py if needed
# -------------------------------------------------------------------------

def create_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> PowerMaxCoordinator:
    """Helper to create the coordinator with a namespaced logger."""
    logger = logging.getLogger(f"{DOMAIN}.coordinator")
    return PowerMaxCoordinator(hass=hass, logger=logger, entry=entry)
