# Battery ideas from SensorsIot/Energy-Management

Notes kept for a future work session. Nothing here is implemented yet.

Source: <https://github.com/SensorsIot/Energy-Management>, read at commit
`6e8789c` (2026-10-10). The battery logic lives in
`energy-manager/src/battery_optimizer.py`, `energy-manager/src/longevity.py`,
`energy-manager/src/flows_daily.py` and chapter 4 of
`energy-manager/Documents/energy-manager-fsd.md`.

## Why most of it does not port directly

Energy-Management controls a Huawei LUNA 2000 battery (10 kWh, 5 kW) over
the local network. Every 15 minutes it writes three inverter settings: max
discharge power, max charge power and end-of-charge SoC.

This integration only reads. The APsystems OpenAPI has GET endpoints only,
a 1000-calls-a-month limit, and SoC at best hourly (`poll_storage_hourly`).
So their control rules cannot run here. What carries over is the battery
model, some measurements, and testing habits.

## Their battery logic in brief

Three independent rules, each re-run every 15 minutes from the live SoC:

- **Hold discharge during off-peak** (FSD 4.2.2, `calculate_decision`).
  Simulate the next 48 h twice: free discharge, and no discharge during
  off-peak slots. Sum the peak-rate grid import of each run; the lower
  wins. A tie goes to free discharge. An active hold is released only once
  the free-discharge run keeps SoC 2 points above the reserve, so it does
  not flip back and forth. The comparison stops at the first point where
  both runs are full again. Inputs: pessimistic PV (P10) and median load
  (P50).
- **Export-peak shaving** (FSD 4.2.3, `should_charge_now`). Pick the
  highest-surplus 15-min slots of the rest of the day until their energy,
  capped at the charge power, fills the free capacity. Charge only in those
  slots, so the battery absorbs the midday peak instead of filling at 9 am.
  Guards: runs only if the battery would fill even on P10 PV, never holds
  back below 20 % SoC, switches off when the forecast is stale, and the
  shave-or-not choice is made once a day.
- **Charge ceiling** (FSD 4.2.4, `battery_longevity`). Cap LFP at 90 %. If
  SoC has not reached 99 % in 7 days, allow a full charge so the BMS can
  recalibrate its SoC estimate (LFP cells have a flat voltage curve).

## Ideas, best first

### 1. Replay the off-peak hold on our own history

Mode 3 (Time-Based Control) lets the user set a schedule in the EMA app,
so the rule can be applied by hand even if we cannot automate it. The
archive (`<config>/apsystems_openapi/storage/<eid>/minutely/`) holds the
5-minute produced/consumed curve for every day.

Replay their two simulations (`simulate_soc`, battery on vs. battery off
during off-peak) on what actually happened, and report per month the kWh
of peak-rate import that a "no discharge during off-peak" schedule would
have saved. That answers whether Time-Based Control is worth enabling, and
in which months.

- Needs: off-peak/peak windows as options, usable capacity and efficiency
  (see idea 2), max charge/discharge power.
- Start each day from a real SoC reading (`latest/*.jsonl`; the 00:30 one
  is enough without the hourly poll).
- The replay knows each day in advance, so its saving is an upper bound.

### 2. Measure the battery values they assume

They hard-code 95 % charge and 95 % discharge efficiency and 10 kWh. We
can measure these from data we already store, at zero API cost:

- **Usable capacity**: the `/systems/inverters` call already returns a
  `capacity` field (kWh) for the storage ECU (`type == 2`). Setup in
  `__init__.py` keeps the `eid` but drops `capacity`.
- **Round-trip efficiency**: total discharged ÷ total charged over a
  window where SoC ends where it started. This includes standby and BMS
  losses.
- **Equivalent full cycles**: total discharged ÷ capacity. Battery
  warranties are stated in this number.

### 3. Daily autarky and self-consumption

From their `flows_daily.compute_flows`. They found that ratios computed
from the grid balance break on days the battery cycles, so they use the
metered load. Our position is better: the device's six daily totals
balance to within a few Wh, and `storage_statistics.hourly_from_minutely`
already rejects days that do not.

- autarky = 1 − imported / consumed
- self-consumption = (produced − exported) / produced

Their self-consumption formula assumes the battery charges from solar
only. Time-Based Control can charge from the grid, so use the
produced − exported version.

### 4. "Days since last full charge" sensor

From their recalibration rule, if the pack is LFP. We cannot force a full
charge, but we can warn in winter. Reliable only with the hourly SoC poll.
Without it, a rough sign of a full battery is export while charging is
near zero in Self-Consumption mode.

### 5. Lesson for any future "SoC at sunrise" projection

They found that adding up the 90th-percentile load forecast for every
15-min slot overnight predicted 11.3 kWh against 6.7 kWh actual on
average (worst night 10.3 kWh), over 30 nights. The planner then expected
an empty battery every night and blocked discharge even at 98–100 % SoC.
They switched to median load and take the safety margin from pessimistic
PV instead. Do the same if we project from the archived consumption
curves.

### 6. Tests for the curve-to-hourly conversion

Each of their calculation functions has its own pytest file, linked from
their spec. Our equivalent, `storage_statistics.hourly_from_minutely`,
has no tests. Cases to cover from saved payloads: gaps in the curve,
daylight-saving days, correction factors outside 0.90–1.15, and days whose
totals do not balance.

## Already covered here

- Marking SoC and mode unavailable after 3 h / 26 h is the same idea as
  their stale-forecast switch-off.
- Archived day files are written through a temp file and `os.replace`, so
  a crash cannot leave a half-written day; the latest-readings log skips a
  torn last line.

## Does not transfer

- Export-peak shaving and their car-charging coordination need control
  over charge power.
- Their rules for writing inverter settings (FSD 4.7.1: read the current
  value before writing, re-apply every 60 s, re-read before each retry)
  only matter with a write path. If local control of the APsystems battery
  ever becomes possible, copy those first.

## Suggested order

2 and 3 first: small, no API calls, and idea 1 needs the numbers from 2.
Then 1.
