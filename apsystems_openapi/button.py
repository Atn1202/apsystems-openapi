from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    store = hass.data[DOMAIN][entry.entry_id]
    sid = entry.data["sid"]

    # Always available: discovering the inverter list is also how the storage
    # ECU is found, so it stays useful even with cloud PV polling disabled.
    buttons: list[ButtonEntity] = [APSRefreshInvertersButton(entry, sid)]

    # The inverter-energy fetch is a cloud PV call. With poll_pv off it can only
    # spend quota re-fetching data the local ECU integration already serves, so
    # the button would be a trap rather than a convenience.
    if store.get("poll_pv", True):
        buttons.append(APSRefreshInverterEnergyButton(entry, sid))

    # Mirror sensor.py: storage controls exist only on a system that has a
    # storage-activated ECU.
    if (store.get("storage_cache") or {}).get("eid"):
        buttons.append(APSRefreshStorageButton(entry, sid))

    async_add_entities(buttons)


class _APSButtonBase(ButtonEntity):
    """Shared identity and device wiring for the manual-refresh buttons.

    These are maintenance controls rather than part of the system's normal
    readout, so they are categorised as configuration entities and grouped on
    the top-level APsystems device.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, sid: str, unique_suffix: str):
        self._entry = entry
        self._sid = sid
        self._attr_unique_id = f"{sid}_{unique_suffix}"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._sid)},
            "manufacturer": "APsystems",
            "name": f"APsystems {self._sid}",
        }

    @property
    def _store(self) -> dict:
        return self.hass.data[DOMAIN][self._entry.entry_id]


class APSRefreshInvertersButton(_APSButtonBase):
    """Button to manually trigger an inverter list refresh."""

    _attr_name = "Scan Inverters"
    _attr_icon = "mdi:magnify-scan"

    def __init__(self, entry: ConfigEntry, sid: str):
        super().__init__(entry, sid, "scan_inverters")

    async def async_press(self) -> None:
        store = self._store
        _LOGGER.info("Manual inverter scan triggered")
        inverters = await store["refresh_inverter_list"]()
        _LOGGER.info("Scan complete: %d inverter(s) found", len(inverters or []))
        # Trigger a coordinator refresh so new sensors pick up the updated list
        await store["coordinator"].async_request_refresh()


class APSRefreshInverterEnergyButton(_APSButtonBase):
    """Button to manually trigger an inverter energy refresh.

    Only created when poll_pv is enabled — see async_setup_entry.
    """

    _attr_name = "Refresh Inverter Data"
    _attr_icon = "mdi:solar-panel"

    def __init__(self, entry: ConfigEntry, sid: str):
        super().__init__(entry, sid, "refresh_inverter_energy")

    async def async_press(self) -> None:
        store = self._store
        _LOGGER.info("Manual inverter energy refresh triggered")
        energy = await store["refresh_inverter_energy"]()
        _LOGGER.info("Inverter energy refresh complete: %d inverter(s)", len(energy or {}))
        await store["coordinator"].async_request_refresh()


class APSRefreshStorageButton(_APSButtonBase):
    """Button to manually fetch battery state and the previous day's balance.

    Storage data is otherwise fetched once daily at 00:30. This button exists
    so the data can be pulled on demand without waiting — and so no fetch
    happens automatically on startup, which would cost 2 API calls per restart.
    """

    _attr_name = "Refresh Storage Data"
    _attr_icon = "mdi:battery-sync"

    def __init__(self, entry: ConfigEntry, sid: str):
        super().__init__(entry, sid, "refresh_storage")

    async def async_press(self) -> None:
        store = self._store
        _LOGGER.info("Manual storage refresh triggered")
        latest = await store["refresh_storage"]()
        if latest:
            _LOGGER.info("Storage refresh complete: SoC %s%%", latest.get("soc"))
        else:
            _LOGGER.info("Storage refresh returned no data (no storage ECU?)")
        await store["coordinator"].async_request_refresh()
