from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.components import persistent_notification
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.util.dt import now, as_local
from homeassistant.helpers.sun import get_astral_event_next, get_astral_event_date
from homeassistant.helpers.event import async_track_point_in_utc_time

from .const import (
    DOMAIN,
    PLATFORMS,
    DEFAULT_BASE_URL,
    API_LIMIT_CODE_MESSAGES,
    MONTHLY_API_QUOTA,
    recommended_scan_interval,
    estimate_monthly_calls,
)
from .api import APSClient, APSRateLimitError

import time as _time

_LOGGER = logging.getLogger(__name__)


def _notify_api_limit(hass: HomeAssistant, err: APSRateLimitError) -> None:
    """Log and raise a persistent HA notification when the API limit is hit."""
    reason = API_LIMIT_CODE_MESSAGES.get(err.code, "access limit exceeded")
    _LOGGER.error(
        "APsystems API limit exceeded (code %s: %s) on %s. Pausing API calls "
        "until the limit resets. Increase the scan interval in the integration "
        "options to stay under the 1000 calls/month quota.",
        err.code, reason, err.path,
    )
    persistent_notification.async_create(
        hass,
        title="APsystems: API limit exceeded",
        message=(
            f"The APsystems OpenAPI reported **{reason}** (code {err.code}).\n\n"
            "Data updates are paused until the limit resets. To avoid hitting "
            "the 1000 calls/month quota, increase the **scan interval** via "
            "Settings → Devices & Services → APsystems OpenAPI → Configure."
        ),
        notification_id=f"{DOMAIN}_api_limit",
    )


def _clear_api_limit_notification(hass: HomeAssistant) -> None:
    """Dismiss the API-limit notification after a successful API call."""
    persistent_notification.async_dismiss(hass, f"{DOMAIN}_api_limit")


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry so changed options take effect immediately.

    Without this, an option changed in the UI is persisted to entry.options but
    not applied until Home Assistant restarts.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    # Credentials and the system identity are written once, at setup, and live
    # in entry.data. The tunables below are editable through the options flow,
    # which writes to entry.options — so they must be read from the merged view,
    # with options winning. Reading entry.data alone silently ignored every
    # option the user had set (notably poll_pv, which then fell back to its
    # True default and kept cloud PV polling enabled).
    data = entry.data
    conf = {**entry.data, **entry.options}

    session = async_get_clientsession(hass)
    client = APSClient(
        app_id=data["app_id"],
        app_secret=data["app_secret"],
        sid=data["sid"],
        base_url=data.get("base_url", DEFAULT_BASE_URL),
        session=session,
    )

    # Store the last fetched data for use during night hours
    last_data = {"summary": None, "hourly": None, "date": None}
    solar_active = {"is_active": False}

    # Inverter tracking state
    inverter_cache = {
        "list": None,                # parsed list of inverter dicts
        "list_fetched_ts": 0,        # epoch when list was last fetched
        "energy": {},                # uid -> energy data dict
        "energy_date": None,         # date string of last fetch
    }

    # Summary tracking state (fetched once per day near end of solar hours)
    summary_cache = {
        "data": None,
        "fetched_date": None,   # date string of last fetch
    }

    # Batch power tracking state (fetched once per day at 11 PM)
    batch_power_cache = {
        "data": {},             # eid -> {time: [...], power: {uid-ch: [...]}}
        "fetched_date": None,   # date string of last fetch
    }

    # Storage (battery) tracking state. Fetched once per day just after
    # midnight for the *previous* day, so the overnight discharge is included.
    storage_cache = {
        "eid": None,            # storage-activated ECU (the PCS serial)
        "latest": None,         # /storage/latest payload
        "period": None,         # /storage/period payload for the previous day
        "fetched_date": None,   # date the period data covers
    }

    def update_solar_state():
        """Check if we're currently in solar hours (30 min after sunrise to sunset)."""
        current_time = as_local(now())
        today = current_time.date()

        # Get sunrise and sunset for today
        sunrise = get_astral_event_date(hass, "sunrise", today)
        sunset = get_astral_event_date(hass, "sunset", today)

        if sunrise and sunset:
            # Add 30 minute buffer after sunrise (panels need time to ramp up)
            sunrise_with_buffer = sunrise + timedelta(minutes=30)
            solar_active["is_active"] = sunrise_with_buffer <= current_time <= sunset
            solar_active["sunset"] = sunset
            _LOGGER.debug(
                "Solar state updated: active=%s (current=%s, start=%s, end=%s)",
                solar_active["is_active"], current_time, sunrise_with_buffer, sunset
            )
        else:
            # Fallback if sun calculation fails
            hour = current_time.hour
            solar_active["is_active"] = 7 <= hour <= 20

    async def refresh_inverter_list():
        """Fetch the inverter list from the API (called by button or first run)."""
        try:
            inv_resp = await client.get_inverters()
            if isinstance(inv_resp, dict) and inv_resp.get("code") == 0:
                raw = inv_resp.get("data", [])
                parsed = []
                for ecu in (raw if isinstance(raw, list) else []):
                    eid = ecu.get("eid")
                    if ecu.get("type") == 2:      # 2 = ECU with storage activated
                        storage_cache["eid"] = eid
                    for inv in ecu.get("inverter", []):
                        parsed.append({
                            "eid": eid,
                            "uid": inv.get("uid"),
                            "type": inv.get("type"),
                        })
                inverter_cache["list"] = parsed
                inverter_cache["list_fetched_ts"] = _time.time()
                _LOGGER.info("Discovered %d inverter(s)", len(parsed))
                return parsed
            else:
                _LOGGER.warning("Inverter list API error: %s", inv_resp)
        except APSRateLimitError as exc:
            _notify_api_limit(hass, exc)
        except Exception as exc:
            _LOGGER.warning("Error fetching inverter list: %s", exc)

        if inverter_cache["list"] is None:
            inverter_cache["list"] = []
        return inverter_cache["list"]

    async def refresh_inverter_energy():
        """Fetch energy data for all inverters (called by button or daily schedule)."""
        date_str = as_local(now()).date().isoformat()
        inv_energy = {}
        for inv in (inverter_cache["list"] or []):
            uid = inv["uid"]
            try:
                resp = await client.get_inverter_energy(uid, date_str, energy_level="minutely")
                if isinstance(resp, dict) and resp.get("code") == 0:
                    inv_energy[uid] = resp.get("data", {})
                elif isinstance(resp, dict) and resp.get("code") == 1001:
                    _LOGGER.debug("No energy data yet for inverter %s (code 1001)", uid)
                else:
                    _LOGGER.warning("Inverter energy error for %s: %s", uid, resp)
            except APSRateLimitError as exc:
                _notify_api_limit(hass, exc)
                break  # limit hit — stop hammering the API for remaining inverters
            except Exception as exc:
                _LOGGER.warning("Failed to fetch energy for inverter %s: %s", uid, exc)

        inverter_cache["energy"] = inv_energy
        inverter_cache["energy_date"] = date_str
        _LOGGER.info("Inverter energy fetched for %s (%d inverters)", date_str, len(inv_energy))
        return inv_energy

    async def refresh_batch_power():
        """Fetch batch power data for all ECUs (one call per ECU, covers all inverters)."""
        date_str = as_local(now()).date().isoformat()

        # Group inverters by ECU
        ecus = set()
        for inv in (inverter_cache["list"] or []):
            eid = inv.get("eid")
            if eid:
                ecus.add(eid)

        batch_data = {}
        for eid in ecus:
            try:
                resp = await client.get_inverter_batch_power(eid, date_str)
                if isinstance(resp, dict) and resp.get("code") == 0:
                    batch_data[eid] = resp.get("data", {})
                else:
                    _LOGGER.warning("Batch power error for ECU %s: %s", eid, resp)
            except APSRateLimitError as exc:
                _notify_api_limit(hass, exc)
                break  # limit hit — stop hammering the API for remaining ECUs
            except Exception as exc:
                _LOGGER.warning("Failed to fetch batch power for ECU %s: %s", eid, exc)

        batch_power_cache["data"] = batch_data
        batch_power_cache["fetched_date"] = date_str
        _LOGGER.info("Batch power fetched for %s (%d ECUs)", date_str, len(batch_data))

        # Push updated data into the coordinator so sensors see it immediately
        if coordinator.data is not None:
            coordinator.data["batch_power"] = batch_power_cache["data"]
            coordinator.data["batch_power_date"] = batch_power_cache["fetched_date"]
            coordinator.async_set_updated_data(coordinator.data)

        return batch_data

    async def refresh_storage():
        """Fetch battery state and the previous day's full energy balance.

        Two calls. Runs just after midnight so the previous day is complete,
        including the overnight discharge a pre-midnight fetch would miss.
        """
        eid = storage_cache["eid"]
        if not eid:
            _LOGGER.debug("No storage-activated ECU found; skipping storage fetch")
            return None

        yesterday = (as_local(now()).date() - timedelta(days=1)).isoformat()
        try:
            latest = await client.get_storage_latest(eid)
            if isinstance(latest, dict) and latest.get("code") == 0:
                storage_cache["latest"] = latest.get("data", {})
            else:
                _LOGGER.warning("Storage latest error: %s", latest)

            period = await client.get_storage_period(eid, yesterday, energy_level="minutely")
            if isinstance(period, dict) and period.get("code") == 0:
                storage_cache["period"] = period.get("data", {})
                storage_cache["fetched_date"] = yesterday
                _LOGGER.info("Storage data fetched for %s", yesterday)
            elif isinstance(period, dict) and period.get("code") == 1001:
                _LOGGER.debug("No storage data yet for %s (code 1001)", yesterday)
            else:
                _LOGGER.warning("Storage period error: %s", period)
        except APSRateLimitError as exc:
            _notify_api_limit(hass, exc)
        except Exception as exc:
            _LOGGER.warning("Error fetching storage data: %s", exc)

        if coordinator.data is not None:
            coordinator.data["storage_latest"] = storage_cache["latest"]
            coordinator.data["storage_period"] = storage_cache["period"]
            coordinator.data["storage_date"] = storage_cache["fetched_date"]
            coordinator.async_set_updated_data(coordinator.data)

        return storage_cache["latest"]

    async def _async_update():
        """Fetch data from API only during solar hours."""
        try:
            update_solar_state()

            # ── Discover inverters on first run only ──
            if inverter_cache["list"] is None:
                await refresh_inverter_list()

            # ── Night-time path: return cached data ──
            if not solar_active["is_active"]:
                _LOGGER.debug("Outside solar hours, returning cached data")
                if last_data["summary"]:
                    cached = dict(last_data)
                    cached["solar_active"] = False
                    cached.setdefault("inverters", inverter_cache["list"] or [])
                    cached.setdefault("inverter_energy", inverter_cache["energy"])
                    cached.setdefault("inverter_energy_date", inverter_cache["energy_date"])
                    cached.setdefault("batch_power", batch_power_cache["data"])
                    cached.setdefault("batch_power_date", batch_power_cache["fetched_date"])
                    cached.setdefault("storage_latest", storage_cache["latest"])
                    cached.setdefault("storage_period", storage_cache["period"])
                    cached.setdefault("storage_date", storage_cache["fetched_date"])
                    return cached

                return {
                    "summary": None,
                    "hourly": None,
                    "date": as_local(now()).date().isoformat(),
                    "solar_active": False,
                    "inverters": inverter_cache["list"] or [],
                    "inverter_energy": inverter_cache["energy"],
                    "inverter_energy_date": inverter_cache["energy_date"],
                    "batch_power": batch_power_cache["data"],
                    "batch_power_date": batch_power_cache["fetched_date"],
                    "storage_latest": storage_cache["latest"],
                    "storage_period": storage_cache["period"],
                    "storage_date": storage_cache["fetched_date"],
                }

            # ── PV polling disabled: return cached shape without API calls ──
            if not poll_pv:
                _LOGGER.debug("PV polling disabled; skipping cloud inverter fetches")
                result = dict(last_data)
                result["solar_active"] = True
                result["date"] = as_local(now()).date().isoformat()
                result.setdefault("summary", None)
                result.setdefault("hourly", None)
                result["inverters"] = inverter_cache["list"] or []
                result["inverter_energy"] = inverter_cache["energy"]
                result["inverter_energy_date"] = inverter_cache["energy_date"]
                result["batch_power"] = batch_power_cache["data"]
                result["batch_power_date"] = batch_power_cache["fetched_date"]
                result["storage_latest"] = storage_cache["latest"]
                result["storage_period"] = storage_cache["period"]
                result["storage_date"] = storage_cache["fetched_date"]
                return result

            # ── Solar-hours: fetch hourly (every cycle) ──
            date_str = as_local(now()).date().isoformat()
            hourly = await client.get_system_energy_hourly(date_str)

            # A successful call means we're under the limit again — clear any notice
            _clear_api_limit_notification(hass)

            if hourly.get("code") != 0:
                _LOGGER.warning("APsystems hourly error: %s", hourly)
                hourly = {"code": 0, "data": []}

            # ── Summary: fetch once per day near end of solar hours ──
            need_summary = summary_cache["data"] is None  # first run
            if not need_summary and summary_cache["fetched_date"] != date_str:
                # Haven't fetched today yet — wait until last cycle before sunset
                sunset_time = solar_active.get("sunset")
                current_time = as_local(now())
                if sunset_time and current_time + timedelta(seconds=scan_interval) >= sunset_time:
                    need_summary = True
                    _LOGGER.debug("Near end of solar day, fetching daily summary")

            if need_summary:
                summary = await client.get_system_summary()
                if summary.get("code") != 0:
                    _LOGGER.warning("APsystems summary error: %s", summary)
                    if summary_cache["data"] is None:
                        raise UpdateFailed(f"APsystems summary error: {summary}")
                else:
                    summary_cache["data"] = summary
                    summary_cache["fetched_date"] = date_str
                    _LOGGER.info("Daily summary fetched for %s", date_str)

            result = {"summary": summary_cache["data"], "hourly": hourly, "date": date_str, "solar_active": True}

            # ── Inverter energy: fetch once per day at 12:30 ──
            current_time = as_local(now())
            past_1230 = current_time.hour > 12 or (current_time.hour == 12 and current_time.minute >= 30)
            if poll_pv and past_1230 and inverter_cache["energy_date"] != date_str:
                await refresh_inverter_energy()

            result["inverters"] = inverter_cache["list"] or []
            result["inverter_energy"] = inverter_cache["energy"]
            result["inverter_energy_date"] = inverter_cache["energy_date"]
            result["batch_power"] = batch_power_cache["data"]
            result["batch_power_date"] = batch_power_cache["fetched_date"]
            result["storage_latest"] = storage_cache["latest"]
            result["storage_period"] = storage_cache["period"]
            result["storage_date"] = storage_cache["fetched_date"]

            last_data.update(result)
            return result

        except APSRateLimitError as e:
            _notify_api_limit(hass, e)
            # Serve cached data so entities stay available instead of going
            # unavailable, which would hide the real cause from the user.
            if last_data["summary"]:
                cached = dict(last_data)
                cached["solar_active"] = solar_active.get("is_active", False)
                cached.setdefault("inverters", inverter_cache["list"] or [])
                cached.setdefault("inverter_energy", inverter_cache["energy"])
                cached.setdefault("inverter_energy_date", inverter_cache["energy_date"])
                cached.setdefault("batch_power", batch_power_cache["data"])
                cached.setdefault("batch_power_date", batch_power_cache["fetched_date"])
                cached.setdefault("storage_latest", storage_cache["latest"])
                cached.setdefault("storage_period", storage_cache["period"])
                cached.setdefault("storage_date", storage_cache["fetched_date"])
                return cached
            raise UpdateFailed(str(e)) from e
        except Exception as e:
            raise UpdateFailed(str(e)) from e

    # Default 30-minute interval (~960 API calls/month with 6 inverters)
    scan_interval = int(conf.get("scan_interval", 1800))  # Default 30 minutes

    # When False, skip all cloud PV polling. Useful when inverter data is already
    # available locally (e.g. via a local ECU integration): the storage endpoints
    # are then the only reason to call the API, cutting usage from ~800 to ~60
    # calls/month.
    poll_pv = conf.get("poll_pv", True)

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=entry,
        name=f"{DOMAIN}_coordinator",
        update_method=_async_update,
        update_interval=timedelta(seconds=scan_interval),
    )

    await coordinator.async_config_entry_first_refresh()

    # ── Auto scan-interval ──────────────────────────────────────────────────
    # Once inverters are discovered, size the polling interval to the site's
    # longest day (from HA's configured latitude) and the inverter/ECU count so
    # the busiest month stays under the 1000 calls/month quota.
    inverters = inverter_cache["list"] or []
    num_inverters = len(inverters)
    num_ecus = len({inv.get("eid") for inv in inverters if inv.get("eid")}) or 1
    has_storage = bool(storage_cache.get("eid"))
    latitude = hass.config.latitude

    if not poll_pv:
        # With cloud PV polling off a coordinator cycle makes no API call at
        # all, so the quota places no constraint on the interval. Leave the
        # configured value alone rather than "optimising" a number that has no
        # effect, and report the usage that actually remains.
        est = estimate_monthly_calls(
            scan_interval, latitude or 0.0, num_inverters, num_ecus,
            poll_pv=False, has_storage=has_storage,
        )
        _LOGGER.info(
            "Cloud PV polling disabled; scan interval left at %ds — "
            "est. %d calls/month (quota %d)",
            scan_interval, est, MONTHLY_API_QUOTA,
        )
    elif conf.get("auto_scan_interval", True):
        if num_inverters and latitude is not None:
            recommended = recommended_scan_interval(
                latitude, num_inverters, num_ecus, has_storage=has_storage
            )
            est = estimate_monthly_calls(
                recommended, latitude, num_inverters, num_ecus,
                has_storage=has_storage,
            )
            _LOGGER.info(
                "Auto scan-interval: %ds (%.0f min) for %d inverter(s)/%d ECU(s)"
                "%s at lat %.2f — est. %d calls/month (quota %d)",
                recommended, recommended / 60, num_inverters, num_ecus,
                " + storage" if has_storage else "",
                latitude, est, MONTHLY_API_QUOTA,
            )
            scan_interval = recommended
            coordinator.update_interval = timedelta(seconds=recommended)
        else:
            _LOGGER.debug(
                "Auto scan-interval skipped (inverters=%d, latitude=%s); "
                "using configured %ds",
                num_inverters, latitude, scan_interval,
            )

    # ── Auto-remove stale inverter devices ──────────────────────────────────
    # When an inverter no longer appears in the API response, remove its device
    # (and its entities) from the registry so the UI doesn't show stale tiles.
    def _prune_stale_inverter_devices() -> None:
        inverters = (coordinator.data or {}).get("inverters")
        # Only prune once we have a valid (non-empty) inverter list, otherwise a
        # transient empty response would wipe all inverter devices.
        if not inverters:
            return

        sid = data["sid"]
        # Include the storage device: it is not an inverter, so without this
        # the pruner treats it as stale and deletes it on every update.
        live_ids = {sid} | {inv["uid"] for inv in inverters if inv.get("uid")}
        if storage_cache.get("eid"):
            live_ids.add(f"{sid}_storage")

        device_reg = dr.async_get(hass)
        for device in dr.async_entries_for_config_entry(device_reg, entry.entry_id):
            device_ids = {ident for (domain, ident) in device.identifiers if domain == DOMAIN}
            if device_ids and device_ids.isdisjoint(live_ids):
                _LOGGER.info(
                    "Removing stale APsystems inverter device %s (no longer reported)",
                    ", ".join(sorted(device_ids)),
                )
                device_reg.async_update_device(
                    device.id, remove_config_entry_id=entry.entry_id
                )

    # Prune now and on every subsequent coordinator update.
    _prune_stale_inverter_devices()
    entry.async_on_unload(coordinator.async_add_listener(_prune_stale_inverter_devices))

    # Reload the entry when options change, so a new poll_pv / scan_interval
    # takes effect without a Home Assistant restart.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # ── Timed schedules ─────────────────────────────────────────────────────
    # Every timer below re-arms itself after firing, so it survives past the
    # first day. Each pending handle is kept here and cancelled on unload:
    # without that, a reload (now routine, since options changes trigger one)
    # would leave the previous entry's timers running alongside the new ones,
    # duplicating the daily API calls once per reload.
    scheduled_unsubs: dict[str, object] = {}

    def _arm(key: str, target, action) -> None:
        """Schedule `action` at `target`, replacing any pending timer for `key`."""
        if (previous := scheduled_unsubs.pop(key, None)) is not None:
            previous()
        scheduled_unsubs[key] = async_track_point_in_utc_time(hass, action, target)

    @callback
    def _cancel_scheduled() -> None:
        while scheduled_unsubs:
            _, unsub = scheduled_unsubs.popitem()
            unsub()

    entry.async_on_unload(_cancel_scheduled)

    def _next_sun_target(kind: str):
        """Next sunrise/sunset plus 30 minutes, guaranteed to be in the future.

        get_astral_event_next can return an event that the 30-minute offset
        pushes into the past — and at the moment a handler re-arms itself it
        may still return the event that just fired. Either would schedule a
        point in the past, which fires immediately and spins.
        """
        event_time = get_astral_event_next(hass, kind)
        if not event_time:
            return None
        target = event_time + timedelta(minutes=30)
        while target <= now():
            target += timedelta(days=1)
        return target

    async def _handle_sun_event(event, kind: str, reschedule) -> None:
        """Update solar state, refresh if we just entered solar hours, re-arm."""
        _LOGGER.info("Sun event triggered (%s): %s", kind, event)
        update_solar_state()
        if solar_active["is_active"]:
            # Trigger an immediate update when entering solar hours
            await coordinator.async_request_refresh()
        await reschedule(now())

    # Track sunrise event (with 30 minute delay)
    async def schedule_sunrise_update(now_time):
        """Schedule update 30 minutes after the next sunrise."""
        target = _next_sun_target("sunrise")
        if target is None:
            _LOGGER.warning("No upcoming sunrise at this latitude; trigger not scheduled")
            return

        async def _run_sunrise(event):
            await _handle_sun_event(event, "sunrise", schedule_sunrise_update)

        _arm("sunrise", target, _run_sunrise)
        _LOGGER.info("Scheduled update for 30 min after sunrise: %s", target)

    # Track sunset event
    async def schedule_sunset_update(now_time):
        """Schedule update 30 minutes after the next sunset."""
        target = _next_sun_target("sunset")
        if target is None:
            _LOGGER.warning("No upcoming sunset at this latitude; trigger not scheduled")
            return

        async def _run_sunset(event):
            await _handle_sun_event(event, "sunset", schedule_sunset_update)

        _arm("sunset", target, _run_sunset)
        _LOGGER.info("Scheduled update for 30 minutes after sunset: %s", target)

    # Schedule batch power fetch at 11 PM daily
    async def schedule_batch_power(now_time):
        """Schedule batch power fetch at 11 PM local time, re-scheduling for the next day."""
        local_now = as_local(now())
        target = local_now.replace(hour=23, minute=0, second=0, microsecond=0)
        if local_now >= target:
            target += timedelta(days=1)

        async def _run_batch(event):
            await refresh_batch_power()
            # Re-schedule for the next day
            await schedule_batch_power(now())

        _arm("batch_power", target, _run_batch)
        _LOGGER.info("Scheduled batch power fetch at %s", target)

    # Schedule storage fetch at 00:30 daily — the previous day is then complete,
    # including overnight battery discharge.
    async def schedule_storage(now_time):
        local_now = as_local(now())
        target = local_now.replace(hour=0, minute=30, second=0, microsecond=0)
        if local_now >= target:
            target += timedelta(days=1)

        async def _run_storage(event):
            await refresh_storage()
            await schedule_storage(now())

        _arm("storage", target, _run_storage)
        _LOGGER.info("Scheduled storage fetch at %s", target)

    # Schedule midnight coordinator refresh to reset daily sensors
    async def schedule_midnight_refresh(now_time):
        """Schedule a coordinator refresh at midnight to reset daily sensors."""
        local_now = as_local(now())
        target = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

        async def _run_midnight(event):
            _LOGGER.info("Midnight refresh: resetting daily sensor data")
            await coordinator.async_request_refresh()
            await schedule_midnight_refresh(now())

        _arm("midnight", target, _run_midnight)
        _LOGGER.info("Scheduled midnight refresh at %s", target)

    # Schedule the initial sun events
    await schedule_sunrise_update(now())
    await schedule_sunset_update(now())
    if poll_pv:
        await schedule_batch_power(now())
    await schedule_midnight_refresh(now())
    await schedule_storage(now())

    # Store everything needed for sensors and button
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "refresh_inverter_list": refresh_inverter_list,
        "refresh_inverter_energy": refresh_inverter_energy,
        "refresh_batch_power": refresh_batch_power,
        "refresh_storage": refresh_storage,
        "storage_cache": storage_cache,
        # Platforms read this to decide whether cloud-PV-only entities are
        # worth creating at all.
        "poll_pv": poll_pv,
        "sun_handlers": {
            "sunrise": schedule_sunrise_update,
            "sunset": schedule_sunset_update
        }
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow manual deletion of an inverter device from the UI.

    The main system device cannot be removed (delete the integration instead).
    An inverter device may be removed when it is no longer reported by the API;
    if it is still live it will simply be re-created on the next update.
    """
    sid = entry.data["sid"]
    device_ids = {ident for (domain, ident) in device.identifiers if domain == DOMAIN}

    # Never allow removing the top-level system device via this dialog.
    if sid in device_ids:
        return False

    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    coordinator = store["coordinator"] if store else None
    live_uids = {
        inv["uid"]
        for inv in ((coordinator.data or {}).get("inverters", []) if coordinator else [])
        if inv.get("uid")
    }

    # Allow removal only if none of this device's inverters are still reported.
    return device_ids.isdisjoint(live_uids)    

    # Summary tracking state (fetched once per day near end of solar hours)
    summary_cache = {
        "data": None,
        "fetched_date": None,   # date string of last fetch
    }

    # Batch power tracking state (fetched once per day at 11 PM)
    batch_power_cache = {
        "data": {},             # eid -> {time: [...], power: {uid-ch: [...]}}
        "fetched_date": None,   # date string of last fetch
    }

    # Storage (battery) tracking state. Fetched once per day just after
    # midnight for the *previous* day, so the overnight discharge is included.
    storage_cache = {
        "eid": None,            # storage-activated ECU (the PCS serial)
        "latest": None,         # /storage/latest payload
        "period": None,         # /storage/period payload for the previous day
        "fetched_date": None,   # date the period data covers
    }

    def update_solar_state():
        """Check if we're currently in solar hours (30 min after sunrise to sunset)."""
        current_time = as_local(now())
        today = current_time.date()

        # Get sunrise and sunset for today
        sunrise = get_astral_event_date(hass, "sunrise", today)
        sunset = get_astral_event_date(hass, "sunset", today)

        if sunrise and sunset:
            # Add 30 minute buffer after sunrise (panels need time to ramp up)
            sunrise_with_buffer = sunrise + timedelta(minutes=30)
            solar_active["is_active"] = sunrise_with_buffer <= current_time <= sunset
            solar_active["sunset"] = sunset
            _LOGGER.debug(
                "Solar state updated: active=%s (current=%s, start=%s, end=%s)",
                solar_active["is_active"], current_time, sunrise_with_buffer, sunset
            )
        else:
            # Fallback if sun calculation fails
            hour = current_time.hour
            solar_active["is_active"] = 7 <= hour <= 20

    async def refresh_inverter_list():
        """Fetch the inverter list from the API (called by button or first run)."""
        try:
            inv_resp = await client.get_inverters()
            if isinstance(inv_resp, dict) and inv_resp.get("code") == 0:
                raw = inv_resp.get("data", [])
                parsed = []
                for ecu in (raw if isinstance(raw, list) else []):
                    eid = ecu.get("eid")
                    if ecu.get("type") == 2:      # 2 = ECU with storage activated
                        storage_cache["eid"] = eid
                    for inv in ecu.get("inverter", []):
                        parsed.append({
                            "eid": eid,
                            "uid": inv.get("uid"),
                            "type": inv.get("type"),
                        })
                inverter_cache["list"] = parsed
                inverter_cache["list_fetched_ts"] = _time.time()
                _LOGGER.info("Discovered %d inverter(s)", len(parsed))
                return parsed
            else:
                _LOGGER.warning("Inverter list API error: %s", inv_resp)
        except APSRateLimitError as exc:
            _notify_api_limit(hass, exc)
        except Exception as exc:
            _LOGGER.warning("Error fetching inverter list: %s", exc)

        if inverter_cache["list"] is None:
            inverter_cache["list"] = []
        return inverter_cache["list"]

    async def refresh_inverter_energy():
        """Fetch energy data for all inverters (called by button or daily schedule)."""
        date_str = as_local(now()).date().isoformat()
        inv_energy = {}
        for inv in (inverter_cache["list"] or []):
            uid = inv["uid"]
            try:
                resp = await client.get_inverter_energy(uid, date_str, energy_level="minutely")
                if isinstance(resp, dict) and resp.get("code") == 0:
                    inv_energy[uid] = resp.get("data", {})
                elif isinstance(resp, dict) and resp.get("code") == 1001:
                    _LOGGER.debug("No energy data yet for inverter %s (code 1001)", uid)
                else:
                    _LOGGER.warning("Inverter energy error for %s: %s", uid, resp)
            except APSRateLimitError as exc:
                _notify_api_limit(hass, exc)
                break  # limit hit — stop hammering the API for remaining inverters
            except Exception as exc:
                _LOGGER.warning("Failed to fetch energy for inverter %s: %s", uid, exc)

        inverter_cache["energy"] = inv_energy
        inverter_cache["energy_date"] = date_str
        _LOGGER.info("Inverter energy fetched for %s (%d inverters)", date_str, len(inv_energy))
        return inv_energy

    async def refresh_batch_power():
        """Fetch batch power data for all ECUs (one call per ECU, covers all inverters)."""
        date_str = as_local(now()).date().isoformat()

        # Group inverters by ECU
        ecus = set()
        for inv in (inverter_cache["list"] or []):
            eid = inv.get("eid")
            if eid:
                ecus.add(eid)

        batch_data = {}
        for eid in ecus:
            try:
                resp = await client.get_inverter_batch_power(eid, date_str)
                if isinstance(resp, dict) and resp.get("code") == 0:
                    batch_data[eid] = resp.get("data", {})
                else:
                    _LOGGER.warning("Batch power error for ECU %s: %s", eid, resp)
            except APSRateLimitError as exc:
                _notify_api_limit(hass, exc)
                break  # limit hit — stop hammering the API for remaining ECUs
            except Exception as exc:
                _LOGGER.warning("Failed to fetch batch power for ECU %s: %s", eid, exc)

        batch_power_cache["data"] = batch_data
        batch_power_cache["fetched_date"] = date_str
        _LOGGER.info("Batch power fetched for %s (%d ECUs)", date_str, len(batch_data))

        # Push updated data into the coordinator so sensors see it immediately
        if coordinator.data is not None:
            coordinator.data["batch_power"] = batch_power_cache["data"]
            coordinator.data["batch_power_date"] = batch_power_cache["fetched_date"]
            coordinator.async_set_updated_data(coordinator.data)

        return batch_data

    async def refresh_storage():
        """Fetch battery state and the previous day's full energy balance.

        Two calls. Runs just after midnight so the previous day is complete,
        including the overnight discharge a pre-midnight fetch would miss.
        """
        eid = storage_cache["eid"]
        if not eid:
            _LOGGER.debug("No storage-activated ECU found; skipping storage fetch")
            return None

        yesterday = (as_local(now()).date() - timedelta(days=1)).isoformat()
        try:
            latest = await client.get_storage_latest(eid)
            if isinstance(latest, dict) and latest.get("code") == 0:
                storage_cache["latest"] = latest.get("data", {})
            else:
                _LOGGER.warning("Storage latest error: %s", latest)

            period = await client.get_storage_period(eid, yesterday, energy_level="minutely")
            if isinstance(period, dict) and period.get("code") == 0:
                storage_cache["period"] = period.get("data", {})
                storage_cache["fetched_date"] = yesterday
                _LOGGER.info("Storage data fetched for %s", yesterday)
            elif isinstance(period, dict) and period.get("code") == 1001:
                _LOGGER.debug("No storage data yet for %s (code 1001)", yesterday)
            else:
                _LOGGER.warning("Storage period error: %s", period)
        except APSRateLimitError as exc:
            _notify_api_limit(hass, exc)
        except Exception as exc:
            _LOGGER.warning("Error fetching storage data: %s", exc)

        if coordinator.data is not None:
            coordinator.data["storage_latest"] = storage_cache["latest"]
            coordinator.data["storage_period"] = storage_cache["period"]
            coordinator.data["storage_date"] = storage_cache["fetched_date"]
            coordinator.async_set_updated_data(coordinator.data)

        return storage_cache["latest"]

    async def _async_update():
        """Fetch data from API only during solar hours."""
        try:
            update_solar_state()

            # ── Discover inverters on first run only ──
            if inverter_cache["list"] is None:
                await refresh_inverter_list()

            # ── Night-time path: return cached data ──
            if not solar_active["is_active"]:
                _LOGGER.debug("Outside solar hours, returning cached data")
                if last_data["summary"]:
                    cached = dict(last_data)
                    cached["solar_active"] = False
                    cached.setdefault("inverters", inverter_cache["list"] or [])
                    cached.setdefault("inverter_energy", inverter_cache["energy"])
                    cached.setdefault("inverter_energy_date", inverter_cache["energy_date"])
                    cached.setdefault("batch_power", batch_power_cache["data"])
                    cached.setdefault("batch_power_date", batch_power_cache["fetched_date"])
                    cached.setdefault("storage_latest", storage_cache["latest"])
                    cached.setdefault("storage_period", storage_cache["period"])
                    cached.setdefault("storage_date", storage_cache["fetched_date"])
                    return cached

                return {
                    "summary": None,
                    "hourly": None,
                    "date": as_local(now()).date().isoformat(),
                    "solar_active": False,
                    "inverters": inverter_cache["list"] or [],
                    "inverter_energy": inverter_cache["energy"],
                    "inverter_energy_date": inverter_cache["energy_date"],
                    "batch_power": batch_power_cache["data"],
                    "batch_power_date": batch_power_cache["fetched_date"],
                    "storage_latest": storage_cache["latest"],
                    "storage_period": storage_cache["period"],
                    "storage_date": storage_cache["fetched_date"],
                }

            # ── PV polling disabled: return cached shape without API calls ──
            if not poll_pv:
                _LOGGER.debug("PV polling disabled; skipping cloud inverter fetches")
                result = dict(last_data)
                result["solar_active"] = True
                result["date"] = as_local(now()).date().isoformat()
                result.setdefault("summary", None)
                result.setdefault("hourly", None)
                result["inverters"] = inverter_cache["list"] or []
                result["inverter_energy"] = inverter_cache["energy"]
                result["inverter_energy_date"] = inverter_cache["energy_date"]
                result["batch_power"] = batch_power_cache["data"]
                result["batch_power_date"] = batch_power_cache["fetched_date"]
                result["storage_latest"] = storage_cache["latest"]
                result["storage_period"] = storage_cache["period"]
                result["storage_date"] = storage_cache["fetched_date"]
                return result

            # ── Solar-hours: fetch hourly (every cycle) ──
            date_str = as_local(now()).date().isoformat()
            hourly = await client.get_system_energy_hourly(date_str)

            # A successful call means we're under the limit again — clear any notice
            _clear_api_limit_notification(hass)

            if hourly.get("code") != 0:
                _LOGGER.warning("APsystems hourly error: %s", hourly)
                hourly = {"code": 0, "data": []}

            # ── Summary: fetch once per day near end of solar hours ──
            need_summary = summary_cache["data"] is None  # first run
            if not need_summary and summary_cache["fetched_date"] != date_str:
                # Haven't fetched today yet — wait until last cycle before sunset
                sunset_time = solar_active.get("sunset")
                current_time = as_local(now())
                if sunset_time and current_time + timedelta(seconds=scan_interval) >= sunset_time:
                    need_summary = True
                    _LOGGER.debug("Near end of solar day, fetching daily summary")

            if need_summary:
                summary = await client.get_system_summary()
                if summary.get("code") != 0:
                    _LOGGER.warning("APsystems summary error: %s", summary)
                    if summary_cache["data"] is None:
                        raise UpdateFailed(f"APsystems summary error: {summary}")
                else:
                    summary_cache["data"] = summary
                    summary_cache["fetched_date"] = date_str
                    _LOGGER.info("Daily summary fetched for %s", date_str)

            result = {"summary": summary_cache["data"], "hourly": hourly, "date": date_str, "solar_active": True}

            # ── Inverter energy: fetch once per day at 12:30 ──
            current_time = as_local(now())
            past_1230 = current_time.hour > 12 or (current_time.hour == 12 and current_time.minute >= 30)
            if poll_pv and past_1230 and inverter_cache["energy_date"] != date_str:
                await refresh_inverter_energy()

            result["inverters"] = inverter_cache["list"] or []
            result["inverter_energy"] = inverter_cache["energy"]
            result["inverter_energy_date"] = inverter_cache["energy_date"]
            result["batch_power"] = batch_power_cache["data"]
            result["batch_power_date"] = batch_power_cache["fetched_date"]
            result["storage_latest"] = storage_cache["latest"]
            result["storage_period"] = storage_cache["period"]
            result["storage_date"] = storage_cache["fetched_date"]

            last_data.update(result)
            return result

        except APSRateLimitError as e:
            _notify_api_limit(hass, e)
            # Serve cached data so entities stay available instead of going
            # unavailable, which would hide the real cause from the user.
            if last_data["summary"]:
                cached = dict(last_data)
                cached["solar_active"] = solar_active.get("is_active", False)
                cached.setdefault("inverters", inverter_cache["list"] or [])
                cached.setdefault("inverter_energy", inverter_cache["energy"])
                cached.setdefault("inverter_energy_date", inverter_cache["energy_date"])
                cached.setdefault("batch_power", batch_power_cache["data"])
                cached.setdefault("batch_power_date", batch_power_cache["fetched_date"])
                cached.setdefault("storage_latest", storage_cache["latest"])
                cached.setdefault("storage_period", storage_cache["period"])
                cached.setdefault("storage_date", storage_cache["fetched_date"])
                return cached
            raise UpdateFailed(str(e)) from e
        except Exception as e:
            raise UpdateFailed(str(e)) from e

    # Default 30-minute interval (~960 API calls/month with 6 inverters)
    scan_interval = int(conf.get("scan_interval", 1800))  # Default 30 minutes

    # When False, skip all cloud PV polling. Useful when inverter data is already
    # available locally (e.g. via a local ECU integration): the storage endpoints
    # are then the only reason to call the API, cutting usage from ~800 to ~60
    # calls/month.
    poll_pv = conf.get("poll_pv", True)

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=entry,
        name=f"{DOMAIN}_coordinator",
        update_method=_async_update,
        update_interval=timedelta(seconds=scan_interval),
    )

    await coordinator.async_config_entry_first_refresh()

    # ── Auto scan-interval ──────────────────────────────────────────────────
    # Once inverters are discovered, size the polling interval to the site's
    # longest day (from HA's configured latitude) and the inverter/ECU count so
    # the busiest month stays under the 1000 calls/month quota.
    if conf.get("auto_scan_interval", True):
        inverters = inverter_cache["list"] or []
        num_inverters = len(inverters)
        num_ecus = len({inv.get("eid") for inv in inverters if inv.get("eid")}) or 1
        latitude = hass.config.latitude

        if num_inverters and latitude is not None:
            recommended = recommended_scan_interval(latitude, num_inverters, num_ecus)
            est = estimate_monthly_calls(recommended, latitude, num_inverters, num_ecus)
            _LOGGER.info(
                "Auto scan-interval: %ds (%.0f min) for %d inverter(s)/%d ECU(s) "
                "at lat %.2f — est. %d calls/month (quota %d)",
                recommended, recommended / 60, num_inverters, num_ecus,
                latitude, est, MONTHLY_API_QUOTA,
            )
            scan_interval = recommended
            coordinator.update_interval = timedelta(seconds=recommended)
        else:
            _LOGGER.debug(
                "Auto scan-interval skipped (inverters=%d, latitude=%s); "
                "using configured %ds",
                num_inverters, latitude, scan_interval,
            )

    # ── Auto-remove stale inverter devices ──────────────────────────────────
    # When an inverter no longer appears in the API response, remove its device
    # (and its entities) from the registry so the UI doesn't show stale tiles.
    def _prune_stale_inverter_devices() -> None:
        inverters = (coordinator.data or {}).get("inverters")
        # Only prune once we have a valid (non-empty) inverter list, otherwise a
        # transient empty response would wipe all inverter devices.
        if not inverters:
            return

        sid = data["sid"]
        # Include the storage device: it is not an inverter, so without this
        # the pruner treats it as stale and deletes it on every update.
        live_ids = {sid} | {inv["uid"] for inv in inverters if inv.get("uid")}
        if storage_cache.get("eid"):
            live_ids.add(f"{sid}_storage")

        device_reg = dr.async_get(hass)
        for device in dr.async_entries_for_config_entry(device_reg, entry.entry_id):
            device_ids = {ident for (domain, ident) in device.identifiers if domain == DOMAIN}
            if device_ids and device_ids.isdisjoint(live_ids):
                _LOGGER.info(
                    "Removing stale APsystems inverter device %s (no longer reported)",
                    ", ".join(sorted(device_ids)),
                )
                device_reg.async_update_device(
                    device.id, remove_config_entry_id=entry.entry_id
                )

    # Prune now and on every subsequent coordinator update.
    _prune_stale_inverter_devices()
    entry.async_on_unload(coordinator.async_add_listener(_prune_stale_inverter_devices))

    # Reload the entry when options change, so a new poll_pv / scan_interval
    # takes effect without a Home Assistant restart.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # ── Timed schedules ─────────────────────────────────────────────────────
    # Every timer below re-arms itself after firing, so it survives past the
    # first day. Each pending handle is kept here and cancelled on unload:
    # without that, a reload (now routine, since options changes trigger one)
    # would leave the previous entry's timers running alongside the new ones,
    # duplicating the daily API calls once per reload.
    scheduled_unsubs: dict[str, object] = {}

    def _arm(key: str, target, action) -> None:
        """Schedule `action` at `target`, replacing any pending timer for `key`."""
        if (previous := scheduled_unsubs.pop(key, None)) is not None:
            previous()
        scheduled_unsubs[key] = async_track_point_in_utc_time(hass, action, target)

    @callback
    def _cancel_scheduled() -> None:
        while scheduled_unsubs:
            _, unsub = scheduled_unsubs.popitem()
            unsub()

    entry.async_on_unload(_cancel_scheduled)

    def _next_sun_target(kind: str):
        """Next sunrise/sunset plus 30 minutes, guaranteed to be in the future.

        get_astral_event_next can return an event that the 30-minute offset
        pushes into the past — and at the moment a handler re-arms itself it
        may still return the event that just fired. Either would schedule a
        point in the past, which fires immediately and spins.
        """
        event_time = get_astral_event_next(hass, kind)
        if not event_time:
            return None
        target = event_time + timedelta(minutes=30)
        while target <= now():
            target += timedelta(days=1)
        return target

    async def _handle_sun_event(event, kind: str, reschedule) -> None:
        """Update solar state, refresh if we just entered solar hours, re-arm."""
        _LOGGER.info("Sun event triggered (%s): %s", kind, event)
        update_solar_state()
        if solar_active["is_active"]:
            # Trigger an immediate update when entering solar hours
            await coordinator.async_request_refresh()
        await reschedule(now())

    # Track sunrise event (with 30 minute delay)
    async def schedule_sunrise_update(now_time):
        """Schedule update 30 minutes after the next sunrise."""
        target = _next_sun_target("sunrise")
        if target is None:
            _LOGGER.warning("No upcoming sunrise at this latitude; trigger not scheduled")
            return

        async def _run_sunrise(event):
            await _handle_sun_event(event, "sunrise", schedule_sunrise_update)

        _arm("sunrise", target, _run_sunrise)
        _LOGGER.info("Scheduled update for 30 min after sunrise: %s", target)

    # Track sunset event
    async def schedule_sunset_update(now_time):
        """Schedule update 30 minutes after the next sunset."""
        target = _next_sun_target("sunset")
        if target is None:
            _LOGGER.warning("No upcoming sunset at this latitude; trigger not scheduled")
            return

        async def _run_sunset(event):
            await _handle_sun_event(event, "sunset", schedule_sunset_update)

        _arm("sunset", target, _run_sunset)
        _LOGGER.info("Scheduled update for 30 minutes after sunset: %s", target)

    # Schedule batch power fetch at 11 PM daily
    async def schedule_batch_power(now_time):
        """Schedule batch power fetch at 11 PM local time, re-scheduling for the next day."""
        local_now = as_local(now())
        target = local_now.replace(hour=23, minute=0, second=0, microsecond=0)
        if local_now >= target:
            target += timedelta(days=1)

        async def _run_batch(event):
            await refresh_batch_power()
            # Re-schedule for the next day
            await schedule_batch_power(now())

        _arm("batch_power", target, _run_batch)
        _LOGGER.info("Scheduled batch power fetch at %s", target)

    # Schedule storage fetch at 00:30 daily — the previous day is then complete,
    # including overnight battery discharge.
    async def schedule_storage(now_time):
        local_now = as_local(now())
        target = local_now.replace(hour=0, minute=30, second=0, microsecond=0)
        if local_now >= target:
            target += timedelta(days=1)

        async def _run_storage(event):
            await refresh_storage()
            await schedule_storage(now())

        _arm("storage", target, _run_storage)
        _LOGGER.info("Scheduled storage fetch at %s", target)

    # Schedule midnight coordinator refresh to reset daily sensors
    async def schedule_midnight_refresh(now_time):
        """Schedule a coordinator refresh at midnight to reset daily sensors."""
        local_now = as_local(now())
        target = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

        async def _run_midnight(event):
            _LOGGER.info("Midnight refresh: resetting daily sensor data")
            await coordinator.async_request_refresh()
            await schedule_midnight_refresh(now())

        _arm("midnight", target, _run_midnight)
        _LOGGER.info("Scheduled midnight refresh at %s", target)

    # Schedule the initial sun events
    await schedule_sunrise_update(now())
    await schedule_sunset_update(now())
    if poll_pv:
        await schedule_batch_power(now())
    await schedule_midnight_refresh(now())
    await schedule_storage(now())

    # Store everything needed for sensors and button
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "refresh_inverter_list": refresh_inverter_list,
        "refresh_inverter_energy": refresh_inverter_energy,
        "refresh_batch_power": refresh_batch_power,
        "refresh_storage": refresh_storage,
        "storage_cache": storage_cache,
        # Platforms read this to decide whether cloud-PV-only entities are
        # worth creating at all.
        "poll_pv": poll_pv,
        "sun_handlers": {
            "sunrise": schedule_sunrise_update,
            "sunset": schedule_sunset_update
        }
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow manual deletion of an inverter device from the UI.

    The main system device cannot be removed (delete the integration instead).
    An inverter device may be removed when it is no longer reported by the API;
    if it is still live it will simply be re-created on the next update.
    """
    sid = entry.data["sid"]
    device_ids = {ident for (domain, ident) in device.identifiers if domain == DOMAIN}

    # Never allow removing the top-level system device via this dialog.
    if sid in device_ids:
        return False

    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    coordinator = store["coordinator"] if store else None
    live_uids = {
        inv["uid"]
        for inv in ((coordinator.data or {}).get("inverters", []) if coordinator else [])
        if inv.get("uid")
    }

    # Allow removal only if none of this device's inverters are still reported.
    return device_ids.isdisjoint(live_uids)
