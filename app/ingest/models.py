"""Pydantic domain models for all ingested sources. These are the validation
contracts: anything that fails here is a candidate for schema recovery or quarantine."""
from __future__ import annotations

from datetime import datetime, date
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Ticket(BaseModel):
    model_config = ConfigDict(extra="allow")

    ticket_id: str
    created_at: datetime
    vehicle: str
    driver_id: Optional[str] = None
    origin_hub: str
    km_from_origin_hub: Optional[float] = None
    destination: str
    issue: str
    severity: str = ""
    client: str
    status: str = "OPEN"
    resolution_note: str = ""
    source_file: str = "tickets.json"
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _coerce_pk(cls, v: dict) -> dict:
        v.setdefault("raw", {})
        return v


class Vehicle(BaseModel):
    model_config = ConfigDict(extra="allow")

    vehicle_id: Optional[str] = None
    registration_number: str
    model: str
    year: Optional[int] = None
    bs_stage: str
    engine_heater: Optional[str] = None  # 'Yes'/'No'/empty
    home_hub: str
    capacity_tonnes: Optional[float] = None
    status: str = "Active"
    source_file: str = "fleet_master.csv"
    raw: dict[str, Any] = Field(default_factory=dict)


class Driver(BaseModel):
    model_config = ConfigDict(extra="allow")

    driver_id: str
    name: str
    phone: str
    dl_number: str
    aadhaar: str
    joining_date: date
    home_hub: str
    source_file: str = "drivers_roster.csv"
    raw: dict[str, Any] = Field(default_factory=dict)


class Trip(BaseModel):
    model_config = ConfigDict(extra="allow")

    trip_id: str
    created_at: datetime
    route_type: str
    origin_name: str
    dest_name: str
    dispatch_time: datetime
    delivery_time: Optional[datetime] = None
    osrm_distance_km: Optional[float] = None
    osrm_time_min: Optional[float] = None
    actual_time_min: Optional[float] = None
    vehicle_reg: str
    driver_id: Optional[str] = None
    client: str
    status: str
    billed_amount: Optional[float] = None
    source_file: str = "meridian_trips.csv"


class MaintenanceRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    entry_id: str = Field(default_factory=lambda: f"MNT-{uuid4().hex[:8]}")
    date: date
    vehicle: str
    odometer_km: Optional[float] = None
    mechanic: str
    notes: str = ""
    notes_masked: str = ""
    source_file: str = "maintenance_log.xlsx"


class EmailRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    thread_id: str
    subject: str
    date: datetime
    from_addr: str = ""
    to_addr: str = ""
    body: str = ""
    body_masked: str = ""
    source_file: str = ""


# ---------------------------------------------------------------- canonical

class CanonicalVehicle(BaseModel):
    """Resolved entity for one physical truck. Registration number is the key."""
    canonical_reg: str
    vehicle_id: Optional[str]
    model: str
    year: Optional[int]
    bs_stage: str
    engine_heater: bool
    home_hub: str
    capacity_tonnes: Optional[float]
    status: str
    aliases: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)  # source -> row/conflict notes


class CanonicalClient(BaseModel):
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


class EligibleCandidate(BaseModel):
    canonical_reg: str
    home_hub: str
    year: Optional[int]
    bs_stage: str
    engine_heater: bool
    capacity_tonnes: Optional[float]
    hub_distance_km: Optional[float] = None
    reasons: list[str] = Field(default_factory=list)      # why it survived
    eliminations: list[str] = Field(default_factory=list) # rules it was checked against


class SelectionResult(BaseModel):
    ticket_id: str
    chosen: Optional[str] = None
    pool_size: int = 0
    eliminations: list[dict[str, Any]] = Field(default_factory=list)
    applied_rules: list[str] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)  # {source, ref, rule_ids}
    notes: list[str] = Field(default_factory=list)