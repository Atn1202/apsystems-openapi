# APsystems OpenAPI – Home Assistant Custom Integration

# FORKED FROM https://github.com/n0liver/HAAC to add storage endpoints data, who in turned formed from https://blackhole.nmrc.org/solar/apsystems-openapi

This is a custom [Home Assistant](https://www.home-assistant.io/) integration for pulling **lifetime** and **daily** solar production data from the [APsystems OpenAPI](https://file.apsystemsema.com:8083/apsystems/resource/openapi/Apsystems_OpenAPI_User_Manual_End_User_EN.pdf). It is designed to integrate with the **Energy dashboard** and also provide today's production plus hourly production breakdowns and per-inverter telemetry, pulling data into Home Assistant from your APsystems account on their [Energy Monitoring & Analysis (EMA) System](https://www.apsystemsema.com/ema/index.action).

A quick note: I was originally using a modified version of a similar tool but it had multiple bugs, seemed to work better in the EU for  some reason, and required a lot of modifications to make it work on my system, so I opted to rewrite the entire thing from scratch. So far all of the errors I was getting have gone away. Please note this was tested on Home Assistant 2025.8.0 so while it might work on different versions, some of the referenced menu items in HA might be different than what is referenced here.

---

## Features

- **Lifetime total kWh** (monotonic increasing) for use in the Energy dashboard
- **Today’s total kWh** (resets daily) for quick daily monitoring
- **Per-inverter sensors** — each micro-inverter is discovered automatically and exposed as its own device with sensors for AC power, DC power (ch 1 & 2), DC voltage (ch 1 & 2), DC current (ch 1 & 2), AC voltage, grid frequency and temperature
- Peak values and full hourly time series exposed as sensor attributes
- Hourly production values for the current day, exposed as attributes
- **Automatic polling interval** — sizes the scan interval to your site's latitude and inverter/ECU count so the busiest month of the year stays under the 1000 calls/month API quota (can be disabled in favour of a fixed interval)
- **Solar-hours-only polling** with sunrise/sunset awareness to conserve API calls
- **API budget awareness** — logs monthly call estimates and raises a persistent Home Assistant notification when the API reports the limit was exceeded
- **Tolerant JSON parsing** — accepts valid JSON even when the API returns an unexpected content-type (e.g. `application/octet-stream`)
- **Automatic stale-inverter cleanup** — inverters no longer reported by the API are removed from the device registry, and can also be removed manually from the UI
- Manual refresh buttons for the inverter list and inverter energy data
- Ships its own **brand icon** (via a local `brand/` folder, Home Assistant 2026.3+) so the integration logo shows in the UI without a brands-repo submission
- Uses **Home Assistant Config Flow** with an options flow (no YAML required)
- Includes debug logging for easy troubleshooting

---

## Installation

0. _Prerequisties:_ Using your account on the [APsystems EMA](https://www.apsystemsema.com/ema/index.action) site, you will need to create an API account as you will need an APP ID, APP Secret, and System ID (SID) for this integration to work. While you can copy the custom code into Home Assistant using the File Editor dashboard, it is much easier if you've installed something like [Studio Code Server](https://github.com/hassio-addons/addon-vscode).

1. Copy the `apsystems_openapi` folder and its contents into:

config/custom_components/

Your structure should look like:

```
config/
	custom_components/
		apsystems_openapi/
			__init__.py
			api.py
			button.py
			config_flow.py
			const.py
			manifest.json
			sensor.py
			brand/
				icon.png
				icon@2x.png
			translations/
				en.json
```

2. Restart Home Assistant.

3. Go to **Settings → Devices & Services → Add Integration**.

4. Search for **APsystems OpenAPI**.

5. Enter:
- **App ID**: Provided by APsystems after API access approval
- **App Secret**: Provided by APsystems
- **System ID (SID)**: Found in your EMA portal
- **API base URL**: Typically the default `https://api.apsystemsema.com:9282` will work, adjust for your region if needed
- **Automatically optimise polling interval**: When enabled (default), the integration sizes the polling interval automatically to stay within the API quota based on your Home Assistant latitude and the number of inverters/ECUs discovered
- **Polling interval (seconds)**: The fixed interval used when automatic optimisation is disabled. Range 1800–7200 (30 min – 2 h), default 1800 (30 min)
- **Sunrise offset / Sunset offset**: Minutes to wait after sunrise before starting, and after sunset before stopping, API calls (0–120, default 30 each)

6. Save and wait for the first update.

You can change the **automatic optimisation** and **polling interval** at any time later via **Settings → Devices & Services → APsystems OpenAPI → Configure**.

---

## Polling schedule

To stay within the API budget, different data is fetched on different schedules:

| Data | When fetched | API calls |
| ---- | ------------ | --------- |
| Hourly system energy | Every poll during solar hours | 1 per cycle |
| Daily summary (lifetime/month/year) | Once per day near the end of solar hours | 1/day |
| Per-inverter energy | Once per day at 12:30 | 1 per inverter |
| Batch power (per ECU) | Once per day at 23:00 | 1 per ECU |
| Inverter list discovery | First run, then on demand via the **Scan Inverters** button | 1 |

Solar hours run from 30 minutes after sunrise (plus your configured offset) until sunset, so no API calls are made overnight.

---

## Inverter Sensors

On first startup the integration calls the APsystems inverter discovery endpoint and creates a set of sensors per micro-inverter. Each inverter appears as its own device in Home Assistant, linked to the parent system.

| Sensor (entity name) | Description |
| -------------------- | ----------- |
| `Power` | Latest AC output power (W) |
| `DC Power Ch1` / `DC Power Ch2` | Latest DC power per channel (W) |
| `DC Voltage Ch1` / `DC Voltage Ch2` | Latest DC voltage per channel (V) |
| `DC Current Ch1` / `DC Current Ch2` | Latest DC current per channel (A) |
| `AC Voltage` | Latest grid voltage (V) |
| `Frequency` | Latest grid frequency (Hz) |
| `Temperature` | Latest inverter temperature (°C) |

The `Power` sensor also exposes the following **attributes**:

| Attribute | Description |
| --------- | ----------- |
| `dc_channel1_power_w` | Latest DC channel 1 power (W) |
| `dc_channel2_power_w` | Latest DC channel 2 power (W) |
| `dc_channel1_peak_w` | Peak DC channel 1 power today (W) |
| `dc_channel2_peak_w` | Peak DC channel 2 power today (W) |
| `ac_power_peak_w` | Peak AC output today (W) |
| `hourly_ac_power` | Full time series of AC power readings |
| `hourly_dc_p1` / `hourly_dc_p2` | Full time series of DC channel power |
| `hourly_times` | Timestamps for the time series |
| `inverter_type` | Model type reported by the API |
| `ecu_id` | ECU the inverter is connected to |

Inverter telemetry is fetched on a slower schedule than system data to conserve your API budget.

### Removing inverters

If an inverter is decommissioned and no longer appears in the API response, its device (and all of its sensors) is **removed automatically** on the next update. You can also remove a stale inverter manually from its device page (**⋮ → Delete**). A device that is still being reported by the API cannot be deleted manually — it would simply be re-created on the next poll.

### Manual refresh buttons

Two buttons are exposed on the main system device:

| Button | Action |
| ------ | ------ |
| `Scan Inverters` | Re-fetches the inverter list (picks up newly added inverters) |
| `Refresh Inverter Data` | Forces an immediate per-inverter energy fetch |

---

## Adding to the Energy Dashboard

1. Go to **Settings → Dashboards → Energy**.
2. Under **Solar production**, select the **Total Energy** sensor of your APsystems system (e.g. `sensor.apsystems_<sid>_total_energy`).
- This sensor has:
  - `device_class: energy`
  - `state_class: total_increasing`
  - Unit: `kWh`
3. Save changes.

> **WARNING:** Do not use the **Today Energy** sensor in the Energy dashboard — it resets daily and will break the Energy graph.  
> You *can* use it in Lovelace cards for at-a-glance daily totals.

---

## Debug Logging

This integration includes detailed debug logging in the code (`_LOGGER.debug` calls) for installation and troubleshooting.  
To enable:

```
# add to configuration.yaml
logger:
  default: warning
  logs:
    custom_components.apsystems_openapi: debug
    aiohttp.client: info
```

After restart, look for log lines like:

```
APS GET /user/api/v2/systems/summary/XXXX params=None s2s_preview=...
APS https://api.apsystemsema.com:9282/... → 200 {"code":0,"data":{...}}
```

## Recommended cleanup after successful setup

- Leave debug logging calls in the code — it is harmless unless enabled in configuration.yaml.
- In configuration.yaml, comment out the logger: section above once the integration is stable to reduce log noise.
- If you want quieter code long-term, you can comment out specific `_LOGGER.debug` calls, but keeping them is fine; Home Assistant will not output them unless debug is enabled. The `_LOGGER.debug` calls are in:
  - `api.py` → request/response preview lines
  - `__init__.py` → coordinator refresh timings


## Troubleshooting

| Symptom | Possible Cause | Fix |
| ------- | -------------- | --- |
| code:4000 in logs | Wrong signing string (RequestPath must be last segment), wrong App ID/Secret, or clock drift | Check that your HA host clock is correct; verify credentials; sign only last URL segment |
| code:5000 from hourly endpoint | No hourly data available yet or transient API error | Usually resolves on next update |
| code:2005 / 7001 / 7002 / 7003 + "API limit exceeded" notification | Monthly quota or server rate limit reached | Leave automatic optimisation on, or increase the scan interval via **Configure**; data resumes when the limit resets |
| Old APsystems integration causing conflicts | Old domain/folder still present | Remove the old folder in custom_components/, delete old integration entry, disable old items in Energy dashboard if present, restart HA |

You can also download and use `apsystems_testcreds.py` (you might have to `pip install aiohttp` first) to help with troubleshooting credential usage outside of Home Assistant. Simply edit it and add your credentials, or use the command line. It is located [here](apsystems_test_apps/apsystems_testcreds.py).

```
$ python3 apsystems_testcreds.py --help
usage: apsystems_testcreds.py [-h] [--app-id APP_ID] [--app-secret APP_SECRET]
                              [--sid SID] [--base-url BASE_URL] [--date DATE]

APsystems OpenAPI credential + endpoint test

options:
  -h, --help            show this help message and exit
  --app-id APP_ID
  --app-secret APP_SECRET
  --sid SID
  --base-url BASE_URL
  --date DATE           YYYY-MM-DD for hourly test (default: today)
```

A self-contained unit test for the JSON content-type handling (no credentials or network required) can be run with:

```
$ python3 apsystems_test_apps/test_json_mimetype.py
```

## Credits

Based on the official [APsystems OpenAPI User Manual](https://file.apsystemsema.com:8083/apsystems/resource/openapi/Apsystems_OpenAPI_User_Manual_End_User_EN.pdf).

Built and tested with APsystems EMA accounts.

## API Budget

APsystems enforces a limit of **1000 API calls per month**. During solar hours the integration makes:

- **1 call per poll** for hourly system energy
- **1 call per day** for the daily summary
- **1 call per inverter per day** for per-inverter energy (at 12:30)
- **1 call per ECU per day** for batch power (at 23:00)
- **1 call per day** for inverter list discovery (first run / on demand)

Because the length of the solar window varies seasonally, the **busiest month is summer**. With automatic optimisation enabled, the integration solves for the smallest interval that keeps the worst-case month under the quota (with head-room) using your latitude and inverter/ECU count, clamped to the 30 min – 2 h range.

Rough worst-case guidance (6 inverters, 1 ECU, ~11–15 h summer days):

| Scan interval | Summer (L≈15 h) | Shoulder (L≈11 h) | Winter (L≈8 h) |
|:-------------:|:---------------:|:-----------------:|:--------------:|
| 1800 s (30 min) | ~1170 ⚠️ over quota | ~930 | ~720 |
| 3600 s (60 min) | ~720 | ~600 | ~510 |

The default fixed interval of 30 min can exceed the quota on long summer days, which is exactly why automatic optimisation is enabled by default. After discovery the integration logs the chosen interval and its estimated monthly usage, e.g.:

```
Auto scan-interval: 3600s (60 min) for 6 inverter(s)/1 ECU(s) at lat 51.50 — est. 690 calls/month (quota 1000)
```

---

## Disclaimer

This is not an official APsystems integration. Use at your own risk. Be mindful of APsystems API request limits — the automatic interval is tuned to stay under 1000 calls/month, but you remain responsible for your own API usage.
