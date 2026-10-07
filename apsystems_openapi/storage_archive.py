"""On-disk archive of the storage (battery) payloads.

Every storage payload the integration fetches is written here as it arrives,
so that nothing the API returned is thrown away and a Home Assistant restart
can repopulate the battery sensors without spending a single API call.

Layout, under ``<config>/apsystems_openapi/storage/<eid>/``:

    minutely/YYYY-MM-DD.json   /storage/period at "minutely" for that day
    hourly/YYYY-MM-DD.json     /storage/period at "hourly" for that day
    latest/YYYY-MM.jsonl       one /storage/latest reading per line

All functions here do blocking file I/O: call them through
``hass.async_add_executor_job``.
"""
from __future__ import annotations

import json
import os
import re

_DATE_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
_MONTH_FILE = re.compile(r"^(\d{4}-\d{2})\.jsonl$")


def _storage_dir(base: str, eid: str, kind: str) -> str:
    return os.path.join(base, "storage", eid, kind)


def write_period(base: str, eid: str, level: str, data_date: str,
                 fetched_at: str, data) -> str:
    """Write one day's /storage/period payload. Overwrites that day's file."""
    folder = _storage_dir(base, eid, level)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{data_date}.json")
    record = {
        "eid": eid,
        "level": level,
        "data_date": data_date,
        "fetched_at": fetched_at,
        "data": data,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, separators=(",", ":"))
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written day
    return path


def read_latest_period(base: str, eid: str, level: str, before: str):
    """Return (data_date, data) for the newest archived day strictly before
    ``before`` (YYYY-MM-DD), or None when nothing is archived."""
    folder = _storage_dir(base, eid, level)
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return None
    dates = sorted(
        m.group(1) for n in names if (m := _DATE_FILE.match(n)) and m.group(1) < before
    )
    for data_date in reversed(dates):
        try:
            with open(os.path.join(folder, f"{data_date}.json"), encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, ValueError):
            continue  # unreadable file: fall back to the previous day
        if isinstance(record, dict) and isinstance(record.get("data"), dict):
            return data_date, record["data"]
    return None


def append_latest(base: str, eid: str, record: dict) -> str:
    """Append one /storage/latest reading to this month's JSON-lines file."""
    folder = _storage_dir(base, eid, "latest")
    os.makedirs(folder, exist_ok=True)
    month = str(record.get("fetched_at", ""))[:7] or "unknown"
    path = os.path.join(folder, f"{month}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
    return path


def read_last_latest(base: str, eid: str):
    """Return the most recent archived /storage/latest record, or None."""
    folder = _storage_dir(base, eid, "latest")
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return None
    months = sorted(m.group(1) for n in names if (m := _MONTH_FILE.match(n)))
    for month in reversed(months):
        try:
            with open(os.path.join(folder, f"{month}.jsonl"), encoding="utf-8") as f:
                lines = [line for line in f.read().splitlines() if line.strip()]
        except OSError:
            continue
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a torn last line after a crash: use the one before
            if isinstance(record, dict) and isinstance(record.get("data"), dict):
                return record
    return None
