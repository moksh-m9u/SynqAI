"""Vehicle selection: deterministic, rule-first.

1. Scope the candidate hubs (R_ORIGIN_50KM / R_NEAREST_HUB):
   - within 50km of origin -> origin hub ONLY
   - beyond 50km           -> nearest hub (haversine over hub_coords resource)
                                that holds at least one eligible vehicle
   Documented deviation (alert): a mandatory hub with zero eligible vehicles
   expands to the next nearest hub with an eligible vehicle.
2. Hard-constraint filtering via the rule engine (season/route BS stage, hill
   heater + 30-day brake rule, jugaad 7-day clock, overdue service, Orion
   year>=2020, Apex rotation, already-assigned, broken vehicle itself).
3. Deterministic tie-break score (year, heater, capacity, hub proximity, plate)
   — identical across reruns.
4. None eligible -> work order on the broken vehicle itself (roadside
   assistance), with citations explaining why.
"""
from __future__ import annotations

from typing import Any

from app.ingest.models import CanonicalVehicle, SelectionResult, Ticket
from app.knowledge.entities import hub_distance_km, nearest_hubs
from app.rules.engine import all_rule_specs, candidate_rejections, selection_score


def candidate_hubs_priority(origin_hub: str, km_from_origin: float | None) -> list[str]:
    within50 = km_from_origin is not None and km_from_origin <= 50
    if within50:
        return [origin_hub]
    nearest = [h for h, _ in nearest_hubs(origin_hub)]
    return nearest or [origin_hub]


def select_vehicle(ticket: Ticket, svc: Any, ctx: dict[str, Any]) -> SelectionResult:
    result = SelectionResult(ticket_id=ticket.ticket_id)
    fleet: dict[str, CanonicalVehicle] = svc.fleet
    km = ticket.km_from_origin_hub
    within50 = km is not None and km <= 50
    priority = candidate_hubs_priority(ticket.origin_hub, km)

    def hub_survivors(hub: str):
        out = []
        for cand in fleet.values():
            if _hub_eq(cand.home_hub, hub):
                rejs = candidate_rejections(rule_ctx, cand)
                if not rejs:
                    hd = hub_distance_km(hub, cand.home_hub) or 0.0
                    out.append(cand)
        return out

    def rank(pool):
        ranked = []
        for cand in pool:
            hd = hub_distance_km(ticket.origin_hub, cand.home_hub) or 0.0
            score, _ = selection_score(rule_ctx, cand, hd)
            ranked.append((score, cand))
        return sorted(ranked, key=lambda x: (-x[0], x[1].canonical_reg))

    rule_ctx = None  # placeholder; built below (needs client/driver from ctx)
    from app.rules.engine import RuleContext
    rule_ctx = RuleContext(ticket=ticket, client=ctx.get("client", ""),
                           vehicle=ctx.get("vehicle"), driver=ctx.get("driver"),
                           fleet=fleet, maint_by_veh=svc.maint_by_veh,
                           assigned=set(svc.assigned), run_id=svc.run_id)

    # collect eliminations for every considered candidate for full transparency
    for hub in [h for h in priority] + [h for h, _ in nearest_hubs(ticket.origin_hub)]:
        for cand in fleet.values():
            if not _hub_eq(cand.home_hub, hub):
                continue
            for r in candidate_rejections(rule_ctx, cand):
                result.eliminations.append({"candidate": cand.canonical_reg, "hub": cand.home_hub,
                                            "rule_id": r["rule_id"], "reason": r["reason"]})

    chosen = None
    hub_used = None
    if within50:
        pool = hub_survivors(priority[0])
        if pool:
            chosen = rank(pool)[0][1]
            hub_used = priority[0]
        else:
            result.notes.append("ALERT: origin hub has no eligible vehicle; expanding to nearest hub (documented deviation R_ORIGIN_50KM)")
            for hub in [h for h, _ in nearest_hubs(priority[0])]:
                alt_pool = hub_survivors(hub)
                if alt_pool:
                    chosen = rank(alt_pool)[0][1]
                    hub_used = hub
                    break
    else:
        for hub in priority:
            pool = hub_survivors(hub)
            if pool:
                hub_used = hub
                best = rank(pool)
                chosen = best[0][1] if best else None
                break

    result.pool_size = sum(1 for v in fleet.values() if _hub_eq(v.home_hub, hub_used or v.home_hub))
    result.citations = [{"source": ", ".join(v["citation"]), "ref": v["rule_id"],
                         "document_type": "dispatcher_rule"}
                        for v in ctx.get("verdicts", []) if v["triggered"]]
    result.applied_rules = [v["rule_id"] for v in ctx.get("verdicts", []) if v["triggered"]]

    if chosen:
        result.chosen = chosen.canonical_reg
        result.notes.append(f"candidate hub: {hub_used or chosen.home_hub}")
        result.notes.append(f"selected {chosen.canonical_reg} (year={chosen.year}, bs={chosen.bs_stage}, heater={chosen.engine_heater}, cap={chosen.capacity_tonnes})")
        result.notes.append(f"priority={'origin-within-50km' if within50 else 'nearest-eligible-hub'}")
    else:
        result.notes.append("NO eligible replacement vehicle — work order dispatches roadside assistance on the broken vehicle")
    return result


def _hub_eq(a: str | None, b: str | None) -> bool:
    return (a or "").strip().title() == (b or "").strip().title()