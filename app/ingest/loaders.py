"""Deterministic loaders for all seven sources. No LLM here — Python decides,
Qwen resolves ambiguity only in schema recovery (see schema_recovery.py).

Personal data (phone / Aadhaar / DL) is masked at ingestion, before storage."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import openpyxl

from app.config import DATA_DIR
from app.ingest.masking import mask
from app.ingest.models import Driver, EmailRecord, MaintenanceRecord, Ticket, Trip, Vehicle
from app.utils import canonicalize_reg, parse_date, parse_datetime


# ---------------------------------------------------------------- tickets
def load_tickets(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or DATA_DIR / "tickets.json"
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- fleet
def load_fleet(path: Path | None = None) -> list[Vehicle]:
    p = path or DATA_DIR / "fleet_master.csv"
    rows: list[Vehicle] = []
    with open(p, encoding="utf-8", newline="") as fh:
        for raw in csv.DictReader(fh):
            reg = (raw.get("registration_number") or "").strip()
            if not reg:
                continue
            cap = raw.get("capacity_tonnes")
            year = raw.get("year")
            rows.append(Vehicle(
                vehicle_id=(raw.get("vehicle_id") or "").strip() or None,
                registration_number=reg,
                model=(raw.get("model") or "").strip(),
                year=int(float(year)) if year else None,
                bs_stage=(raw.get("bs_stage") or "").strip().upper(),
                engine_heater=((raw.get("engine_heater") or "").strip() or None),
                home_hub=(raw.get("home_hub") or "").strip(),
                capacity_tonnes=float(cap) if cap else None,
                status=(raw.get("status") or "Active").strip(),
                raw=dict(raw),
            ))
    return rows


# ---------------------------------------------------------------- drivers
def load_drivers(path: Path | None = None) -> list[Driver]:
    p = path or DATA_DIR / "drivers_roster.csv"
    out: list[Driver] = []
    with open(p, encoding="utf-8", newline="") as fh:
        for raw in csv.DictReader(fh):
            out.append(Driver(
                driver_id=(raw.get("driver_id") or "").strip(),
                name=(raw.get("name") or "").strip(),
                phone=mask((raw.get("phone") or "").strip()),
                dl_number=mask((raw.get("dl_number") or "").strip()),
                aadhaar=mask((raw.get("aadhaar") or "").strip()),
                joining_date=parse_date(raw.get("joining_date")) or parse_date("1900-01-01"),
                home_hub=(raw.get("home_hub") or "").strip(),
                raw=dict(raw),
            ))
    return out


# ---------------------------------------------------------------- trips
def load_trips(path: Path | None = None) -> list[Trip]:
    p = path or DATA_DIR / "meridian_trips.csv"
    out: list[Trip] = []
    with open(p, encoding="utf-8", newline="") as fh:
        for raw in csv.DictReader(fh):
            try:
                out.append(Trip(
                    trip_id=raw.get("trip_id", ""),
                    created_at=parse_datetime(raw.get("created_at")) or parse_datetime("1900-01-01"),
                    route_type=raw.get("route_type", ""),
                    origin_name=raw.get("origin_name", ""),
                    dest_name=raw.get("dest_name", ""),
                    dispatch_time=parse_datetime(raw.get("dispatch_time")) or parse_datetime("1900-01-01"),
                    delivery_time=parse_datetime(raw.get("delivery_time")),
                    osrm_distance_km=_f(raw.get("osrm_distance_km")),
                    osrm_time_min=_f(raw.get("osrm_time_min")),
                    actual_time_min=_f(raw.get("actual_time_min")),
                    vehicle_reg=raw.get("vehicle_reg", ""),
                    driver_id=raw.get("driver_id"),
                    client=(raw.get("client") or "").strip(),
                    status=raw.get("status", ""),
                    billed_amount=_f(raw.get("billed_amount")),
                    raw=dict(raw),
                ))
            except Exception:
                continue
    return out


# ---------------------------------------------------------------- maintenance
def load_maintenance(path: Path | None = None) -> list[MaintenanceRecord]:
    p = path or DATA_DIR / "maintenance_log.xlsx"
    wb = openpyxl.load_workbook(p, data_only=True)
    ws = wb.worksheets[0]
    out: list[MaintenanceRecord] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not row[1]:
            continue
        date, vehicle, odo, mechanic, notes = (list(row) + [None] * 5)[:5]
        notes_txt = str(notes or "").strip()
        out.append(MaintenanceRecord(
            date=parse_date(date) or parse_date("1900-01-01"),
            vehicle=str(vehicle).strip(),
            odometer_km=_f(odo),
            mechanic=str(mechanic or "").strip(),
            notes=notes_txt,
            notes_masked=mask(notes_txt),
        ))
    return out


# ---------------------------------------------------------------- emails
_HEADER_RE = re.compile(r"^(From|To|Date|Subject):\s*(.*)$", re.IGNORECASE)


def load_emails(path: Path | None = None) -> list[EmailRecord]:
    p = path or DATA_DIR / "emails"
    out: list[EmailRecord] = []
    for fh in sorted(Path(p).glob("*.txt")):
        text = fh.read_text(encoding="utf-8")
        headers: dict[str, str] = {}
        lines = text.splitlines()
        body_start = 0
        for i, line in enumerate(lines):
            m = _HEADER_RE.match(line)
            if m:
                headers[m.group(1).lower()] = m.group(2).strip()
            elif line.strip() and ":" not in line:
                body_start = i
                break
            elif line.strip() == "" and i > 0:
                body_start = i + 1
            elif _HEADER_RE.match(line) is None and "Subject" in headers and line.strip():
                # body reached (headers done)
                body_start = i
                break
        body = "\n".join(lines[body_start:]).strip()
        date = headers.get("date", "")
        out.append(EmailRecord(
            thread_id=fh.stem,
            subject=headers.get("subject", ""),
            date=parse_datetime(date) or parse_datetime("1900-01-01"),
            from_addr=headers.get("from", ""),
            to_addr=headers.get("to", ""),
            body_masked=mask(body),
            body=body,
            source_file=fh.name,
        ))
    return out


# ---------------------------------------------------------------- interview
def load_interview(path: Path | None = None) -> str:
    p = path or DATA_DIR / "dispatcher_interview.txt"
    return mask(p.read_text(encoding="utf-8"))


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None