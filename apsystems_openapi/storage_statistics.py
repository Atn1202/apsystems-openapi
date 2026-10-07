"""Hourly long-term statistics built from the archived storage curves.

The 00:30 job archives the previous day's /storage/period "minutely" payload
(storage_archive.py). This module turns those files into hourly *external*
statistics — one per balance term — which the Energy dashboard can use as a
battery source and which statistics-graph cards can chart.

How the curve is read (verified against the SIG Shelly's 5-minute statistics,
6 Oct 2026):

* Each point "HH:MM" is the AVERAGE power over the 5 minutes ENDING at HH:MM,
  and its `energy` value is exactly that power × 5 minutes. So a point belongs
  to the hour of (HH:MM − 5 min): "07:00" is 06:55–07:00, in the 06:00 hour.
  The API's own "hourly" level files it under 07:00 — one slot late per hour,
  which moves charge across tariff boundaries; that is why it is not used.
  "00:00" (really 23:55–24:00 of the previous day) is kept in hour 0.
* The point list has gaps (277 of 288 points on 6 Oct). A missing slot is
  filled with the mean power of the points either side.
* Even filled, a sampled curve sums 1–3 % short of the device's own day
  counters (`today`), which close the six-term balance to a few Wh. Each term
  is therefore scaled so its 24 hours add up to the device total: the curve
  gives the shape, the counter gives the amount. A day whose counters do not
  balance, or that would need an implausible scale factor, is not imported.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import logging

_LOGGER = logging.getLogger(__name__)

TERMS = ("charge", "discharge", "produced", "consumed", "imported", "exported")
TERM_NAMES = {
    "charge": "battery charged",
    "discharge": "battery discharged",
    "produced": "solar produced",
    "consumed": "house consumed",
    "imported": "grid imported",
    "exported": "grid exported",
}

SLOT_MIN = 5
SCALE_MIN, SCALE_MAX = 0.90, 1.15   # plausible curve → counter factors
SCALE_CHECK_KWH = 0.5               # below this total, noise dominates the factor
BALANCE_TOLERANCE_KWH = 0.05        # the counters close to a few Wh when healthy


class CurveError(ValueError):
    """The day's payload cannot be turned into trustworthy hourly values."""


def _minutes(label: str) -> int:
    hh, mm = label.split(":")
    return int(hh) * 60 + int(mm)


def _hour_of(slot_end_min: int) -> int:
    """Hour bucket for the 5-minute slot ending at slot_end_min."""
    return 0 if slot_end_min == 0 else (slot_end_min - SLOT_MIN) // 60


def hourly_from_minutely(data: dict) -> tuple[dict[str, list[float]], dict[str, float]]:
    """24 hourly kWh values per term from one day's "minutely" payload.

    Returns (hours, factors): hours[term] is a list of 24 kWh values summing
    to data["today"][term]; factors[term] is the scale applied. Raises
    CurveError when the payload is unusable.
    """
    try:
        labels = list(data["time"])
        power = {t: [float(x) for x in data["power"][t]] for t in TERMS}
        energy = {t: [float(x) for x in data["energy"][t]] for t in TERMS}
        totals = {t: float(data["today"][t]) for t in TERMS}
    except (KeyError, TypeError, ValueError) as exc:
        raise CurveError(f"malformed payload: {exc}") from exc
    if not labels or any(len(power[t]) != len(labels) or len(energy[t]) != len(labels)
                         for t in TERMS):
        raise CurveError("time, power and energy lists differ in length")

    residual = (totals["produced"] + totals["imported"] + totals["discharge"]
                - totals["consumed"] - totals["exported"] - totals["charge"])
    if abs(residual) > BALANCE_TOLERANCE_KWH:
        raise CurveError(f"day counters do not balance (residual {residual:+.3f} kWh)")

    mins = [_minutes(x) for x in labels]
    if any(b <= a for a, b in zip(mins, mins[1:])):
        raise CurveError("time list is not strictly increasing")

    hours = {t: [0.0] * 24 for t in TERMS}
    for i, m in enumerate(mins):
        h = _hour_of(m)
        for t in TERMS:
            hours[t][h] += energy[t][i]
        # Fill a gap before this point with the mean of its neighbours' power.
        if i and m - mins[i - 1] > SLOT_MIN:
            for missing_end in range(mins[i - 1] + SLOT_MIN, m, SLOT_MIN):
                hm = _hour_of(missing_end)
                for t in TERMS:
                    hours[t][hm] += (power[t][i - 1] + power[t][i]) / 2 * SLOT_MIN / 60 / 1000

    factors = {}
    for t in TERMS:
        curve = sum(hours[t])
        total = totals[t]
        if curve < 0.001:
            if total >= SCALE_CHECK_KWH:
                raise CurveError(f"{t}: curve is empty but the day total is {total:.3f} kWh")
            # A near-zero day: nothing to shape, keep the (tiny) total in hour 0.
            hours[t] = [0.0] * 24
            hours[t][0] = total
            factors[t] = 1.0
            continue
        factor = total / curve
        if total >= SCALE_CHECK_KWH and not SCALE_MIN <= factor <= SCALE_MAX:
            raise CurveError(f"{t}: curve {curve:.3f} kWh vs counter {total:.3f} kWh "
                             f"(factor {factor:.3f})")
        hours[t] = [v * factor for v in hours[t]]
        factors[t] = factor
    return hours, factors


def statistic_id(domain: str, eid: str, term: str) -> str:
    return f"{domain}:storage_{eid.lower()}_{term}"


def hour_starts_utc(day: date, tz) -> list[datetime]:
    """UTC start of each local wall-clock hour 0..23 of `day`.

    On DST days two wall-clock hours can map to one UTC hour (spring forward);
    callers merge values that share a start.
    """
    from homeassistant.util import dt as dt_util
    return [dt_util.as_utc(datetime.combine(day, time(h), tzinfo=tz)) for h in range(24)]


async def async_import_days(hass, domain: str, eid: str,
                            days: list[tuple[str, dict]]) -> list[str]:
    """Import archived days (oldest first) as hourly external statistics.

    Every imported row's cumulative sum continues from the last row before the
    first day, so importing day N again, or days N..M, keeps the series
    consistent — provided nothing after M was imported earlier. Callers always
    pass a run that ends at the newest archived day. Returns the dates imported.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import (
        StatisticData, StatisticMeanType, StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics, statistics_during_period,
    )
    from homeassistant.const import UnitOfEnergy
    from homeassistant.util import dt as dt_util
    from homeassistant.util.unit_conversion import EnergyConverter

    if not days:
        return []
    tz = dt_util.get_default_time_zone()
    ids = {t: statistic_id(domain, eid, t) for t in TERMS}

    first_start = hour_starts_utc(date.fromisoformat(days[0][0]), tz)[0]
    before = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, first_start - timedelta(days=60), first_start,
        set(ids.values()), "hour", None, {"sum"},
    )
    running = {t: float((before.get(ids[t]) or [{}])[-1].get("sum") or 0.0) for t in TERMS}

    rows: dict[str, list] = {t: [] for t in TERMS}
    imported = []
    for day_str, data in days:
        try:
            hours, factors = hourly_from_minutely(data)
        except CurveError as exc:
            _LOGGER.warning("Battery statistics: %s not imported — %s", day_str, exc)
            continue
        merged: dict[datetime, dict[str, float]] = {}
        for h, start in enumerate(hour_starts_utc(date.fromisoformat(day_str), tz)):
            slot = merged.setdefault(start, {t: 0.0 for t in TERMS})
            for t in TERMS:
                slot[t] += hours[t][h]
        for start in sorted(merged):
            for t in TERMS:
                running[t] += merged[start][t]
                rows[t].append(StatisticData(start=start, state=merged[start][t], sum=running[t]))
        imported.append(day_str)
        _LOGGER.debug("Battery statistics: %s scale factors %s", day_str,
                      {t: round(f, 3) for t, f in factors.items()})

    for t in TERMS:
        if not rows[t]:
            continue
        async_add_external_statistics(hass, StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"APsystems {TERM_NAMES[t]} (cloud, hourly)",
            source=domain,
            statistic_id=ids[t],
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        ), rows[t])
    if imported:
        _LOGGER.info("Battery statistics imported for %s", ", ".join(imported)
                     if len(imported) <= 3 else f"{imported[0]} … {imported[-1]} ({len(imported)} days)")
    return imported
