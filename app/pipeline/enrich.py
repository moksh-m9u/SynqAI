"""Context enrichment: joins live operational truth (fleet, roster, trips,
maintenance) with rule engine verdicts and deterministic ETA estimation.

Citations are carried on every fact; 'insufficient information' is returned
instead of a hallucinated answer when a source is silent."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ingest.models import Driver, Ticket
from app.knowledge.entities import canonicalize_client, hub_distance_km, region_info
from app.rules.engine import (RuleContext, brake_work_dates, compute_eta, evaluate_ticket,
                              jugaad_status, service_due_evidence)
from app.utils import canonicalize_reg


@dataclass
class Services:
    fleet: dict[str, Any]                  # canonical_reg -> CanonicalVehicle
    drivers: dict[str, Driver]
    maint_by_veh: dict[str, list[Any]]
    trips_by_veh: dict[str, list[Any]]
    assigned: set[str]
    run_id: str = ""
    eta_stats: dict[str, float] | None = None


def build_context(ticket: Ticket, svc: Services) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Returns (context, citations)."""
    ctx: dict[str, Any] = {}
    citations: list[dict[str, Any]] = []
    canon_reg = canonicalize_reg(ticket.vehicle)

    # --- broken vehicle (fleet master wins for attributes) ---
    vehicle = svc.fleet.get(canon_reg)
    if vehicle:
        ctx["vehicle"] = {
            "canonical_reg": vehicle.canonical_reg,
            "vehicle_id": vehicle.vehicle_id,
            "model": vehicle.model, "year": vehicle.year, "bs_stage": vehicle.bs_stage,
            "engine_heater": vehicle.engine_heater, "home_hub": vehicle.home_hub,
            "capacity_tonnes": vehicle.capacity_tonnes, "status": vehicle.status,
            "aliases": vehicle.aliases,
        }
        citations.append({"source": "fleet_master.csv", "ref": f"plate={canon_reg}",
                          "entity": f"VEHICLE_{canon_reg}", "document_type": "fleet_record"})
    else:
        ctx["vehicle"] = None
        ctx.setdefault("notes", []).append("vehicle not found in fleet_master — insufficient information")
        citations.append({"source": "fleet_master.csv", "ref": f"plate={canon_reg}",
                          "finding": "no matching record"})

    # --- driver (masked PII, IDs only in artifacts) ---
    driver = svc.drivers.get(ticket.driver_id or "")
    if driver:
        ctx["driver"] = {
            "driver_id": driver.driver_id, "home_hub": driver.home_hub,
            "joining_date": driver.joining_date.isoformat(),
            "tenure_days": (ticket.created_at.date() - driver.joining_date).days,
            "pii_masked": True,
        }
        citations.append({"source": "drivers_roster.csv", "ref": driver.driver_id,
                          "entity": f"DRIVER_{driver.driver_id}", "document_type": "roster_record"})
    else:
        ctx["driver"] = None
        ctx.setdefault("notes", []).append(f"driver {ticket.driver_id or '(none)'} not in roster — insufficient information")

    # --- client + SLA ---
    client = canonicalize_client(ticket.client)
    ctx["client"] = client
    ctx["client_source"] = ticket.client
    citations.append({"source": "tickets.json", "ref": ticket.ticket_id,
                      "entity": f"CLIENT_{client.replace(' ', '_')}", "document_type": "ticket"})

    # --- maintenance history for the broken vehicle ---
    maint = svc.maint_by_veh.get(canon_reg, [])
    ctx["maintenance"] = {
        "records": [{"date": r.date.isoformat(), "odometer_km": r.odometer_km,
                     "mechanic": r.mechanic, "notes": r.notes_masked} for r in reversed(maint[-12:])],
        "brake_work_dates": [d.isoformat() for d in brake_work_dates(canon_reg, svc.maint_by_veh, ticket.created_at.date())],
        "jugaad": jugaad_status(canon_reg, svc.maint_by_veh, ticket.created_at.date()),
        "service_due": service_due_evidence(canon_reg, svc.maint_by_veh),
    }
    if maint:
        citations.append({"source": "maintenance_log.xlsx", "ref": f"plate={canon_reg}",
                          "entity": f"VEHICLE_{canon_reg}", "document_type": "maintenance_history",
                          "records": len(maint)})

    # --- trip context (historical 2018 corpus; no active 2026 trip available) ---
    trips = svc.trips_by_veh.get(canon_reg, [])
    ctx["recent_trips"] = [{
        "trip_id": t.trip_id, "date": t.created_at.isoformat(), "route": f"{t.origin_name}->{t.dest_name}",
        "client": t.client, "status": t.status, "billed_amount": t.billed_amount,
    } for t in trips[-3:]]
    trip_summary = (f"{len(trips)} historical trips on file (corpus 2018 sample); "
                    "no active trip in the 2026 operating window — insufficient information for live position")
    ctx["trip_summary"] = trip_summary if not trips else trip_summary

    # --- geography + rules + ETA ---
    ctx["region"] = region_info(ticket.origin_hub, ticket.destination)
    rule_ctx = RuleContext(ticket=ticket, client=client, vehicle=vehicle,
                           driver=driver, fleet=svc.fleet, maint_by_veh=svc.maint_by_veh,
                           assigned=set(svc.assigned), run_id=svc.run_id)
    verdicts = evaluate_ticket(rule_ctx)
    ctx["verdicts"] = [v.to_dict() for v in verdicts]
    ctx["applied_rule_ids"] = [v.rule_id for v in verdicts if v.triggered]
    for v in verdicts:
        if v.triggered:
            citations.append({"source": ", ".join(v.citation), "ref": v.rule_id,
                              "entity": f"RULE_{v.rule_id}", "document_type": "dispatcher_rule"})

    stats = svc.eta_stats or {}
    ctx["eta"] = compute_eta(rule_ctx, stats)
    hub_dist = hub_distance_km(ticket.origin_hub, ticket.destination)
    ctx["route_distance_est_km"] = hub_dist

    # --- SQLite checkpointer + langsmith thread context (attached later in node) ---
    return ctx, citations