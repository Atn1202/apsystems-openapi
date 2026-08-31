from __future__ import annotations
import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    async_add_entities([
        APSRefreshInvertersButton(hass, entry),
        APSRefreshInverterEnergyButton(hass, entry),
        APSRefreshStorageButton(hass, entry),
    ])


class APSRefreshInvertersButton(ButtonEntity):
    """Button to manually trigger an inverter list refresh."""

    _attr_has_entity_name = True
    _attr_name = "Scan Inverters"
    _attr_icon = "mdi:magnify-scan"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self._entry = entry
        self._sid = entry.data["sid"]
        self._attr_unique_id = f"{self._sid}_scan_inverters"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._sid)},
            "manufacturer": "APsystems",
            "name": f"APsystems {self._sid}",
        }

    async def async_press(self) -> None:
        store = self.hass.data[DOMAIN][self._entry.entry_id]
        refresh_fn = store["refresh_inverter_list"]
        coordinator = store["coordinator"]

        _LOGGER.info("Manual inverter scan triggered")
        inverters = await refresh_fn()
        _LOGGER.info("Scan complete: %d inverter(s) found", len(inverters))

        # Trigger a coordinator refresh so new sensors pick up the updated list
        await coordinator.async_request_refresh()


class APSRefreshInverterEnergyButton(ButtonEntity):
    """Button to manually trigger an inverter energy refresh."""

    _attr_has_entity_name = True
    _attr_name = "Refresh Inverter Data"
    _attr_icon = "mdi:solar-panel"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self._entry = entry
        self._sid = entry.data["sid"]
        self._attr_unique_id = f"{self._sid}_refresh_inverter_energy"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._sid)},
            "manufacturer": "APsystems",
            "name": f"APsystems {self._sid}",
        }

    async def async_press(self) -> None:
        store = self.hass.data[DOMAIN][self._entry.entry_id]
        refresh_fn = store["refresh_inverter_energy"]
        coordinator = store["coordinator"]

        _LOGGER.info("Manual inverter energy refresh triggered")
        energy = await refresh_fn()
        _LOGGER.info("Inverter energy refresh complete: %d inverter(s)", len(energy))

        await coordinator.async_request_refresh()

class APSRefreshStorageButton(ButtonEntity):
    """Button to manually fetch battery state and the previous day's balance.

    Storage data is otherwise fetched once daily at 00:30. This button exists
    so the data can be pulled on demand without waiting — and so no fetch
    happens automatically on startup, which would cost 2 API calls per restart.
    """

    _attr_has_entity_name = True
    _attr_name = "Refresh Storage Data"
    _attr_icon = "mdi:battery-sync"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self._entry = entry
        self._sid = entry.data["sid"]
        self._attr_unique_id = f"{self._sid}_refresh_storage"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._sid)},
            "manufacturer": "APsystems",
            "name": f"APsystems {self._sid}",
        }

    async def async_press(self) -> None:
        store = self.hass.data[DOMAIN][self._entry.entry_id]
        refresh_fn = store["refresh_storage"]
        coordinator = store["coordinator"]

        _LOGGER.info("Manual storage refresh triggered")
        latest = await refresh_fn()
        if latest:
            _LOGGER.info("Storage refresh complete: SoC %s%%", latest.get("soc"))
        else:
            _LOGGER.info("Storage refresh returned no data (no storage ECU?)")

        await coordinator.async_request_refresh()
