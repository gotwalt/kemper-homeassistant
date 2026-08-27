"""The base entity: device identity, naming and availability in one place."""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import KemperCoordinator


class KemperEntity(CoordinatorEntity[KemperCoordinator]):
    """One reading from one Profiler."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: KemperCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_id}_{key}"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        """Available while the readings are live.

        Which is not the same as while the socket is up: a dropped session is
        rebuilt underneath these entities, and for the length of that grace
        what they hold is a few seconds old rather than wrong. Going
        unavailable for every blink would say the opposite, loudly, in the
        logbook.
        """
        coordinator = self.coordinator
        return super().available and coordinator.data is not None and coordinator.readings_live
