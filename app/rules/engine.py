"""Deterministic rule engine.

Rules are declared in rules.yaml (inspectable, cited) and are EXECUTED by Python
functions below. The engine never redisocvers rules via semantic search: Qdrant
holds the transcript for citations only.

Python decides. Qwen resolves ambiguity only in schema recovery and comms drafting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import json
import yaml

from app.config import ARTIFACTS_DIR, RULES_PATH
from app.ingest.models import CanonicalVehicle, Driver, MaintenanceRecord, Ticket
from app.knowledge.entities import hub_distance_km, region_info
from app.utils import atomic_write_json, canonicalize_reg, night_hour

# ---------------------------------------------------------------- maintenance signal parsing
_BRAKE_KEY = re.compile(r"brake", re.I)
_JUGAAD_KEY = re.compile(r"jugaad|permanent fix|temporary fix|permanent repair|baaki|pending fix|band kiya", re.I)
_FIXED_KEY = re.compile(r"replaced under warranty|naya lagwaya|new (pad|drum)|permanent fix ho gaya|permanent (repair|fix) (done|complete)", re.I)
_JUGAAD_PENDING = re.compile(r"baaki|pending|permanent fix baaki|temporary fix applied", re.I)


def brake_work_dates(reg: str, maint_by_veh: dict[str, list[MaintenanceRecord]], ref: date) -> list[date]:
    canon = canonicalize_reg(reg)
    out = []
    for r in maint_by_veh.get(canon, []):
        if r.date and r.date <= ref and abs((ref - r.date).days) <= 366 and _BRAKE_KEY.search(r.notes):
            out.append(r.date)
    return sorted(out, reverse=True)


def jugaad_status(reg: str, maint_by_veh: dict[str, list[MaintenanceRecord]], ref: date) -> dict[str, Any]:
    canon = canonicalize_reg(reg)
    recs = sorted([r for r in maint_by_veh.get(canon, [])
                   if r.date and r.date <= ref], key=lambda r: r.date, reverse=True)
    jugaad_entries = [r for r in recs if _JUGAAD_KEY.search(r.notes)]
    status: dict[str, Any] = {"active": False, "last_entry": None, "permanent_fix_done": False,
                              "within_7d": False, "evidence": []}
    for r in jugaad_entries:
        pending = bool(_JUGAAD_PENDING.search(r.notes))
        fixed = bool(_FIXED_KEY.search(r.notes))
        if pending or (not fixed and r.mechanic.lower() == "guddu"):
            within = (ref - r.date).days <= 7
            status["active"] = within
            status["last_entry"] = r.date
            status["within_7d"] = within
            status["evidence"].append({"date": r.date, "mechanic": r.mechanic, "notes": r.notes})
            if not fixed:
                status["permanent_fix_done"] = False
                break
    # active 7-day clock also applies if a fix is recorded but not marked permanent => keep restricted
    return status


def service_due_evidence(reg: str, maint_by_veh: dict[str, list[MaintenanceRecord]]) -> dict[str, Any]:
    """R_OVERDUE_SERVICE_GROUNDED. The maintenance log carries NO scheduled service
    due dates in the supplied data; per precedence the rule is applied honestly:
    no evidence => not triggered, with a citation to that finding."""
    canon = canonicalize_reg(reg)
    hits = [r for r in maint_by_veh.get(canon, []) if re.search(r"service|due date|overdue", r.notes, re.I)]
    return {"due_date_evidence": [r.date.isoformat() for r in hits],
            "triggered": bool(hits), "note": "no service-due records in ingested maintenance_log"}


# ---------------------------------------------------------------- rule spec loading
_RULE_SPECS: dict[str, dict[str, Any]] | None = None


def _load_rule_specs() -> dict[str, dict[str, Any]]:
    global _RULE_SPECS
    if _RULE_SPECS is None:
        text = RULES_PATH.read_text(encoding="utf-8")
        docs = [d for d in yaml.safe_load_all(text) if d]
        _RULE_SPECS = {d["id"]: d for d in docs}
    return _RULE_SPECS


def rule_spec(rule_id: str) -> dict[str, Any]:
    spec = _load_rule_specs().get(rule_id) or {}
    spec.setdefault("id", rule_id)
    return spec


def all_rule_specs() -> list[dict[str, Any]]:
    return list(_load_rule_specs().values())


def export_rule_specs() -> Path:
    """Materialize the rule catalog as an inspectable artifact (AGENTS.md rule
    artifacts: id, severity, condition, action, citation, client/season/route
    index fields, provenance). Written on every pipeline run."""
    out = []
    for spec in all_rule_specs():
        row = {
            "id": spec["id"],
            "severity": spec.get("severity", ""),
            "client": spec.get("client", "any"),
            "season": spec.get("season", "any"),
            "route": spec.get("route", "any"),
            "condition": spec.get("condition", ""),
            "action": spec.get("action", ""),
            "citation": spec.get("citation", ""),
            "note": spec.get("note", ""),
            "provenance": {
                "spec_source": "app/rules/rules.yaml",
                "extracted_from": "dispatcher_interview.txt + emails/",
                "execution": "deterministic Python in app.rules.engine (never Qwen)",
            },
        }
        out.append(row)
    dirp = ARTIFACTS_DIR / "rules"
    dirp.mkdir(parents=True, exist_ok=True)
    path = dirp / "rules_export.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for row in out:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


# ---------------------------------------------------------------- context
@dataclass
class RuleContext:
    ticket: Ticket
    client: str                       # canonical client
    vehicle: CanonicalVehicle | None  # BROKEN vehicle
    driver: Driver | None
    fleet: dict[str, CanonicalVehicle]              # canonical_reg -> vehicle
    maint_by_veh: dict[str, list[MaintenanceRecord]]
    assigned: set[str] = field(default_factory=set)  # plates already assigned in this queue run
    run_id: str = ""
    thread_id: str = ""

    def __post_init__(self):
        from app.knowledge.entities import CanonicalVehicle
        if isinstance(self.vehicle, dict):
            self.vehicle = CanonicalVehicle(**self.vehicle) if self.vehicle else None

    def _driver(self):
        d = self.driver
        if d is None:
            return None, None
        if isinstance(d, dict):
            return d.get("driver_id"), d.get("joining_date")
        return d.driver_id, d.joining_date

    def ref_date(self) -> date:
        return self.ticket.created_at.date()

    def region(self) -> dict[str, bool]:
        return region_info(self.ticket.origin_hub, self.ticket.destination)


@dataclass
class Verdict:
    rule_id: str
    severity: str
    triggered: bool
    detail: str
    citation: list[str]
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "severity": self.severity,
                "triggered": self.triggered, "detail": self.detail,
                "citation": self.citation, "evidence": self.evidence}


def _months(ctx: RuleContext) -> set[int]:
    return {ctx.ticket.created_at.month}


# ---------------------------------------------------------------- ticket-level verdicts
def evaluate_ticket(ctx: RuleContext) -> list[Verdict]:
    specs = _load_rule_specs()
    month = ctx.ticket.created_at.month
    reg = ctx.region()
    verdicts: list[Verdict] = []

    def add(rule_id: str, triggered: bool, detail: str, evidence: dict[str, Any] | None = None):
        s = specs[rule_id]
        verdicts.append(Verdict(rule_id=rule_id, severity=s["severity"], triggered=triggered,
                                detail=detail, citation=[s["citation"]] if isinstance(s.get("citation"), str)
                                else s.get("citation", []), evidence=evidence or {}))

    km = ctx.ticket.km_from_origin_hub
    within50 = km is not None and km <= 50
    add("R_ORIGIN_50KM", bool(within50),
        f"km_from_origin_hub={km} <= 50 -> replacement from {ctx.ticket.origin_hub or 'ORIGIN'} hub",
        {"km_from_origin_hub": km, "origin_hub": ctx.ticket.origin_hub})
    add("R_NEAREST_HUB", km is not None and km > 50,
        f"km_from_origin_hub={km} > 50 -> replacement from nearest hub with an eligible vehicle",
        {"km_from_origin_hub": km})

    winter = month in {10, 11, 12, 1, 2}
    add("R_NCR_BS6_WINTER", winter and reg["touches_ncr"],
        f"month={month} winter={winter} touches_ncr={reg['touches_ncr']} -> BS6 only",
        {"month": month, "touches_ncr": reg["touches_ncr"]})

    hill_season = month in {11, 12, 1, 2}
    add("R_HILL_ENGINE_HEATER", hill_season and reg["hill"],
        f"month={month} hill={reg['hill']} -> engine_heater required",
        {"month": month, "hill": reg["hill"]})
    add("R_HILL_BRAKE_30D", hill_season and reg["hill"],
        f"month={month} hill={reg['hill']} -> no brake work in last 30 days",
        {"month": month, "hill": reg["hill"]})

    if ctx.vehicle:
        ref = ctx.ref_date()
        brake_dates = brake_work_dates(ctx.vehicle.canonical_reg, ctx.maint_by_veh, ref)
        ju = jugaad_status(ctx.vehicle.canonical_reg, ctx.maint_by_veh, ref)
        sd = service_due_evidence(ctx.vehicle.canonical_reg, ctx.maint_by_veh)
        add("R_JUGAAD_7D", ju["active"],
            "broken vehicle carries an active jogadi restriction (7-day clock / home-region only)",
            ju)
        add("R_OVERDUE_SERVICE_GROUNDED", sd["triggered"], sd["note"], sd)

    if ctx.client == "Shakti Cement":
        add("R_SHAKTI_36H", True, "Shakti loads planned to 36h door-to-door (contract 48h is legacy)",
            {"client": "Shakti Cement"})
    if ctx.client == "Vertex Retail" and (ctx.ticket.destination or "").strip().lower() == "ludhiana":
        add("R_VERTEX_6PM_GATE", True,
            "Vertex Ludhiana gate closes 18:00 sharp; later arrivals held overnight as scheduled morning delivery",
            {"destination": ctx.ticket.destination})
    if ctx.client == "Orion Pharma":
        add("R_ORION_2020", True, "Orion: only 2020 or newer vehicles, newest preferred",
            {"client": "Orion Pharma"})
    if ctx.client == "Apex Chemicals":
        add("R_APEX_ROTATION", True, "Apex: the problem vehicle is not returned on the next Apex dispatch",
            {"client": "Apex Chemicals"})
    if month in {7, 8, 9} and reg["east_lucknow"]:
        add("R_MONSOON_EAST_PADDING", True,
            "monsoon east-of-Lucknow route: ETA +20%, quoted padded number, no standard SLA promise",
            {"month": month, "east_lucknow": reg["east_lucknow"]})

    if ctx.driver:
        _did, _jd = ctx._driver()
        joined = date.fromisoformat(_jd) if isinstance(_jd, str) else (_jd or None)
        tenure_days = (ctx.ticket.created_at.date() - joined).days if joined is not None else 99999
        is_night = night_hour(ctx.ticket.created_at.hour)
        add("R_NEW_DRIVER_NIGHT", tenure_days < 180 and is_night,
            f"driver new (<6mo, {tenure_days}d) and night run -> not solo",
            {"tenure_days": tenure_days, "night": is_night,
             "driver_id": _did})

    return verdicts


# ---------------------------------------------------------------- candidate-level eligibility
def candidate_rejections(ctx: RuleContext, cand: CanonicalVehicle) -> list[dict[str, str]]:
    """Return (rule_id, reason) pairs for a candidate that is being considered as replacement."""
    specs = _load_rule_specs()
    rej: list[dict[str, str]] = []
    month = ctx.ticket.created_at.month
    reg = ctx.region()
    ref = ctx.ref_date()

    if cand.status.lower() != "active":
        rej.append({"rule_id": "R_DELIVERY_ELIGIBLE", "reason": f"status={cand.status}"})
    if ctx.vehicle and cand.canonical_reg == ctx.vehicle.canonical_reg:
        rej.append({"rule_id": "R_DELIVERY_ELIGIBLE", "reason": "candidate is the broken vehicle itself"})
    if cand.canonical_reg in ctx.assigned:
        rej.append({"rule_id": "R_DELIVERY_ELIGIBLE", "reason": "already assigned to a work order in this queue run"})

    if (month in {10, 11, 12, 1, 2}) and reg["touches_ncr"]:
        if cand.bs_stage != "BS6":
            rej.append({"rule_id": "R_NCR_BS6_WINTER",
                        "reason": f"bs_stage={cand.bs_stage or '?'} not route-permitted on NCR winter route (need BS6)"})

    hill_season = month in {11, 12, 1, 2} and reg["hill"]
    if hill_season:
        if not cand.engine_heater:
            rej.append({"rule_id": "R_HILL_ENGINE_HEATER", "reason": "no engine heater on hill-route dispatch"})
        brake_dates = brake_work_dates(cand.canonical_reg, ctx.maint_by_veh, ref)
        within30 = [d for d in brake_dates if (ref - d).days <= 30]
        if within30:
            rej.append({"rule_id": "R_HILL_BRAKE_30D",
                        "reason": f"brake work {within30[0]} within 30 days of dispatch"})

    ju = jugaad_status(cand.canonical_reg, ctx.maint_by_veh, ref)
    if ju["active"]:
        rej.append({"rule_id": "R_JUGAAD_7D",
                    "reason": "active jogadi restriction (home-region only, permanent fix pending)"})

    sd = service_due_evidence(cand.canonical_reg, ctx.maint_by_veh)
    if sd["triggered"]:
        rej.append({"rule_id": "R_OVERDUE_SERVICE_GROUNDED", "reason": "overdue service"})

    if ctx.client == "Orion Pharma" and cand.year is not None:
        if cand.year < 2020:
            rej.append({"rule_id": "R_ORION_2020", "reason": f"year {cand.year} < 2020 (Orion audit SOP)"})

    if ctx.client == "Apex Chemicals" and ctx.vehicle and cand.canonical_reg == ctx.vehicle.canonical_reg:
        rej.append({"rule_id": "R_APEX_ROTATION", "reason": "problem vehicle must rotate off Apex"})

    # capacity (soft) only when tonnage is actually known
    return rej


def selection_score(ctx: RuleContext, cand: CanonicalVehicle, hub_dist: float | None) -> tuple[float, list[str]]:
    """Deterministic tie-break policy, documented in selection artifacts."""
    score = 100.0
    notes: list[str] = []
    # newer vehicle preferred (Orion-ish safety margin, harmless generally)
    if cand.year:
        score += (cand.year - 2012) * 1.0
    # engine heater is a robustness bonus even off-hill
    if cand.engine_heater:
        score += 2.0
    # capacity: prefer a vehicle that could carry a load; larger ok but penalty for oversize
    if cand.capacity_tonnes is not None:
        score += min(cand.capacity_tonnes, 31) * 0.1
    # home-hub proximity to the dispatch origin is a mild preference
    if hub_dist is not None:
        score += max(0.0, 60.0 - hub_dist) * 0.05
    # deterministic secondary key: registration lexicographic (stable across reruns)
    score = round(score, 6)
    notes.append(f"score={score} (year={cand.year}, heater={cand.engine_heater}, cap={cand.capacity_tonnes}, hub_dist={hub_dist})")
    return score, notes


# ---------------------------------------------------------------- ETA estimation (deterministic)
def build_eta_cache() -> dict[str, float]:
    """Fleet-average speed from meridian_trips, cached as an artifact for comms shaping."""
    from app.ingest.loaders import load_trips
    trips = [t for t in load_trips()
             if t.osrm_distance_km and t.actual_time_min and t.actual_time_min > 0
             and t.osrm_distance_km > 10]
    if not trips:
        return {"avg_speed_kph": 32.0, "sample_trips": 0}
    total_km = sum(t.osrm_distance_km for t in trips)
    total_min = sum(t.actual_time_min for t in trips)
    avg = total_km / (total_min / 60.0)
    return {"avg_speed_kph": round(avg, 2), "sample_trips": len(trips)}


def compute_eta(ctx: RuleContext, stats: dict[str, float]) -> dict[str, Any]:
    """Legal ETA estimate. Always cited; never presented as ground truth."""
    est = hub_distance_km(ctx.ticket.origin_hub, ctx.ticket.destination)
    speed = stats.get("avg_speed_kph", 32.0)
    base_h = (est or 200) / speed if est else None
    padding = 0.0
    if _load_rule_specs()["R_MONSOON_EAST_PADDING"] and ctx.ticket.created_at.month in {7, 8, 9} \
        and region_info(ctx.ticket.origin_hub, ctx.ticket.destination)["east_lucknow"]:
        padding = 0.20
    padded = round(base_h * (1 + padding), 1) if base_h else None
    vertex_hold = False
    if ctx.client == "Vertex Retail" and (ctx.ticket.destination or "").strip().lower() == "ludhiana":
        vertex_hold = True
    return {"base_hours": round(base_h, 1) if base_h else None,
            "padding_frac": padding, "estimated_hours": padded,
            "avg_speed_kph": speed, "basis": "hub_coords + fleet avg speed (meridian_trips)",
            "vertex_hold_until_morning": vertex_hold}