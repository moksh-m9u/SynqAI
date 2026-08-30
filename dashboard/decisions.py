"""Decision Assistance Agent — a live, inspectable advisor.

Pure logic (no Streamlit). Given a ticket scenario or a free-form operational
question it walks the exact production decision path and returns:

  steps         — every step it took, with input / output / timing
  todos         — concrete next actions for an operator
  retrieved     — the RAG context it actually used (with scores)
  recommendation— a grounded verdict + rationale

Every step reuses the production code path (preprocess.prepare, the real rule
engine, select_vehicle, the real KnowledgeStore) so what the agent decides is
byte-for-byte what the pipeline would do.
"""
from __future__ import annotations

import time
from typing import Any

from app.ingest.loaders import load_tickets
from app.ops import sample_records
from app.utils import canonicalize_reg


class TraceLog:
    def __init__(self, sink=None) -> None:
        self.lines: list[str] = []
        self._sink = sink

    def __call__(self, s: str) -> None:
        self.lines.append(str(s))
        if self._sink:
            self._sink(s)


def _coerce_log(log) -> TraceLog:
    if isinstance(log, TraceLog):
        return log
    return TraceLog(log)  # plain callable sink -> also captured locally


def _step(steps: list[dict[str, Any]], sid: str, title: str, detail: str,
          inp: Any, out: Any, t0: float) -> None:
    steps.append({"id": sid, "title": title, "detail": detail,
                  "timing_ms": round((time.perf_counter() - t0) * 1000, 1),
                  "input": inp, "output": out})


def _client_resolution(raw_name: str | None) -> dict[str, Any]:
    from app.knowledge.entities import canonicalize_client, client_aliases

    raw = (raw_name or "").strip()
    canon = canonicalize_client(raw_name)
    matched: str | None = None
    for _, aliases in client_aliases().items():
        for a in aliases:
            if a and a.lower() == raw.lower():
                matched = a
    source = ("exact_alias" if matched
              else ("contains_match" if canon and canon.lower() != raw.lower() else "direct"))
    confidence = 0.99 if matched else (0.86 if source == "contains_match" else 1.0)
    return {"raw_value": raw or None, "canonical": canon, "matched_alias": matched,
            "source": source, "confidence": confidence}


def _vehicle_resolution(ticket, svc: Any) -> dict[str, Any]:
    canon_reg = canonicalize_reg(ticket.vehicle)
    vehicle = svc.fleet.get(canon_reg)
    fleet = {"canonical": vehicle.canonical_reg, "vehicle_id": vehicle.vehicle_id,
             "model": vehicle.model, "year": vehicle.year, "bs_stage": vehicle.bs_stage,
             "engine_heater": vehicle.engine_heater, "home_hub": vehicle.home_hub,
             "capacity_tonnes": vehicle.capacity_tonnes, "status": vehicle.status} if vehicle else None
    source = "fleet_master" if vehicle else "unregistered"
    return {"raw_value": ticket.vehicle, "canonical_reg": canon_reg,
            "in_fleet_master": vehicle is not None, "fleet_entry": fleet, "source": source}


def _driver_resolution(driver_id: str | None, svc: Any) -> dict[str, Any]:
    if not driver_id:
        return {"driver_id": None, "resolved": False, "reason": "no driver_id on ticket"}
    d = svc.drivers.get(driver_id)
    if d is None:
        return {"driver_id": driver_id, "resolved": False, "reason": "unknown driver_id"}
    return {"driver_id": driver_id, "resolved": True,
            "name": getattr(d, "name", None), "joining_date": str(getattr(d, "joining_date", None))}


def _region_resolution(ticket, svc: Any) -> dict[str, Any]:
    from app.knowledge.entities import hub_distance_km, region_info

    reg = region_info(ticket.origin_hub, ticket.destination)
    return {"origin_hub": ticket.origin_hub, "destination": ticket.destination,
            "touches_ncr": reg.get("touches_ncr"), "hill": reg.get("hill"),
            "hub_available": hub_lookup_ok(ticket.origin_hub)}


def hub_lookup_ok(name: str | None) -> bool:
    from app.knowledge.entities import hub_lookup
    return hub_lookup(name) is not None


def _retrieval_query(ticket) -> str:
    parts = [ticket.issue, ticket.destination, ticket.origin_hub, ticket.client]
    return " ".join(p for p in parts if p)


def trace_ticket(record: dict[str, Any], *, collection: str = "merged",
                 top_k: int = 6, log: TraceLog | None = None) -> dict[str, Any]:
    """Full decision trace for one ticket record (production code path)."""
    log = _coerce_log(log)
    steps: list[dict[str, Any]] = []
    started = time.perf_counter()

    from app.run import _build_services
    from app.pipeline.preprocess import prepare

    # ---------------- STEP 1: understand / standardize ----------------
    t0 = time.perf_counter()
    log("step 1/6 understand — validate + schema recovery + quarantine check")
    try:
        pt = prepare(record, seen=set(), run_id="dce")
    except Exception as exc:  # e.g. schema recovery hit an unavailable LLM
        log(f"prepare raised ({exc}) -> hard quarantine")
        _step(steps, "understand", "Understand the raw record", "Validation + schema recovery",
              record, {"status": "quarantined", "reason": f"validate error: {exc}"}, t0)
        return _quarantine_result(record, steps, started, log)

    if pt.status == "quarantined":
        log(f"quarantined: {pt.reason}")
        _step(steps, "understand", "Understand the raw record", "Validation + schema recovery",
              record, {"status": "quarantined", "reason": pt.reason,
                       "duplicate_of": pt.duplicate_of, "recovered": pt.recovered,
                       "report": pt.report}, t0)
        return _quarantine_result(record, steps, started, log)

    log(f"valid ticket {pt.ticket_id} (recovered={pt.recovered})")
    ticket = pt.ticked
    _step(steps, "understand", "Understand the raw record", "Validate, dedupe, schema-recover",
          record, {"status": "valid", "ticket_id": pt.ticket_id, "recovered": pt.recovered,
                   "fields": _ticket_fields(ticket)}, t0)

    svc = _build_services()

    # ---------------- STEP 2: resolve entities ----------------
    t0 = time.perf_counter()
    log("step 2/6 resolve — canonical client, vehicle, driver, hub")
    ent = {"client": _client_resolution(ticket.client),
           "vehicle": _vehicle_resolution(ticket, svc),
           "driver": _driver_resolution(ticket.driver_id, svc),
           "region": _region_resolution(ticket, svc)}
    _step(steps, "resolve", "Resolve messy entities",
          "Map raw names to canonical records (aliases, registration normalization)",
          {"client": ticket.client, "vehicle": ticket.vehicle, "driver_id": ticket.driver_id,
           "origin_hub": ticket.origin_hub, "destination": ticket.destination}, ent, t0)

    # ---------------- STEP 3: retrieve context ----------------
    t0 = time.perf_counter()
    log(f"step 3/6 retrieve — search knowledge base (collection={collection}, top_k={top_k})")
    query = _retrieval_query(ticket)
    try:
        rich = svc.store.search_rich(query, collection=collection, top_k=top_k)
        hits = rich.get("hits", [])
    except Exception as exc:
        hits = []
        log(f"retrieval unavailable ({exc})")
    hits = [{k: h.get(k) for k in ("chunk_id", "score", "source", "document_type",
                                    "language", "text")} for h in hits]
    _step(steps, "retrieve", "Retrieve relevant knowledge",
          f"Embed the ticket and pull the closest knowledge chunks",
          {"query": query, "collection": collection, "top_k": top_k}, hits, t0)

    # ---------------- STEP 4: apply dispatch rules ----------------
    t0 = time.perf_counter()
    log("step 4/6 rules — evaluate the ticket against the rule catalogue")
    from app.rules.engine import RuleContext, evaluate_ticket

    broken_veh = svc.fleet.get(canonicalize_reg(ticket.vehicle))
    driver = svc.drivers.get(ticket.driver_id) if ticket.driver_id else None
    rule_ctx = RuleContext(ticket=ticket, client=ent["client"]["canonical"],
                           vehicle=broken_veh, driver=driver, fleet=svc.fleet,
                           maint_by_veh=svc.maint_by_veh, assigned=set(), run_id="dce")
    verdicts = [v.to_dict() for v in evaluate_ticket(rule_ctx)]
    log(f"{sum(1 for v in verdicts if v['triggered'])} rules triggered")
    _step(steps, "rules", "Apply the retired dispatcher's rules",
          "Deterministic hard constraints, soft constraints and heuristics (never Qwen)",
          {"client": ent["client"]["canonical"], "season": _season(ticket),
           "region": ent["region"]}, verdicts, t0)

    # ---------------- STEP 5: select vehicle ----------------
    t0 = time.perf_counter()
    log("step 5/6 select — scope hubs, filter candidates, rank survivors")
    from app.pipeline.select_vehicle import select_vehicle

    sel = select_vehicle(ticket, svc, {"client": ent["client"]["canonical"],
                                       "vehicle": broken_veh, "driver": driver,
                                       "verdicts": verdicts})
    log(f"selected={sel.chosen or 'none -> roadside assistance'} "
        f"(eliminated={len(sel.eliminations)})")
    out = {"chosen": sel.chosen, "pool_size": sel.pool_size,
           "applied_rules": sel.applied_rules, "eliminations": sel.eliminations,
           "citations": sel.citations, "notes": sel.notes}
    _step(steps, "select", "Select the replacement vehicle",
          "50km/nearest-hub scoping, hard-constraint filter, deterministic ranking",
          {"origin_hub": ticket.origin_hub, "km_from_origin_hub": ticket.km_from_origin_hub,
           "location": [f"{c.get('candidate', c.get('vehicle'))} | {c.get('hub', '')}"
                        for c in sel.eliminations[:8]]}, out, t0)

    # ---------------- STEP 6: decide ----------------
    t0 = time.perf_counter()
    triggered = [v for v in verdicts if v["triggered"]]
    todos = _todos_for(sel, ent, ticket, triggered)
    recommendation = _recommendation(sel, ent, ticket, triggered, steps, started)
    _step(steps, "decide", "Decide and plan next actions",
          "Synthesize outcome, rationale and an operator todo-list",
          {"ticket_id": ticket.ticket_id}, {"outcome": recommendation["outcome"],
                                            "todos": todos}, t0)

    return {"mode": "ticket", "ticket_id": ticket.ticket_id,
            "identifier": f"{ticket.ticket_id or '(auto)'}",
            "steps": steps, "todos": todos, "retrieved": hits,
            "recommendation": recommendation,
            "verdicts": verdicts, "selection": out,
            "entities": ent, "wall_ms": round((time.perf_counter() - started) * 1000),
            "log": log.lines}


def _ticket_fields(ticket) -> dict[str, Any]:
    return {"ticket_id": ticket.ticket_id, "created_at": str(ticket.created_at),
            "client": ticket.client, "vehicle": ticket.vehicle,
            "origin_hub": ticket.origin_hub, "km_from_origin_hub": ticket.km_from_origin_hub,
            "destination": ticket.destination, "issue": ticket.issue,
            "severity": ticket.severity, "status": ticket.status}


def _season(ticket) -> str:
    m = ticket.created_at.month
    if m in {10, 11, 12, 1, 2}:
        return "winter (Oct-Feb)"
    if m in {3, 4, 5}:
        return "pre-monsoon (Mar-May)"
    return "monsoon (Jun-Sep)"


def _quarantine_result(record: dict[str, Any], steps: list[dict[str, Any]],
                       started: float, log: TraceLog) -> dict[str, Any]:
    reason = steps[0]["output"].get("reason", "validation failed")
    return {"mode": "ticket", "ticket_id": steps[0]["output"].get("ticket_id") or "(missing_id)",
            "identifier": steps[0]["output"].get("ticket_id") or "(missing_id)",
            "steps": steps,
            "todos": [{"do": "Inspect the flags below and repair the record", "why": reason},
                      {"do": "Fix or supply ticket_id, created_at, vehicle, origin_hub, destination, issue",
                       "why": "critical-field completeness policy"},
                      {"do": "Re-submit the corrected ticket to the sandbox", "why": "queue gate"},
                      {"do": "Drop the record if it is unresolvable (logged to quarantine)",
                       "why": "exactly-once guarantee"}],
            "retrieved": [],
            "recommendation": {"outcome": "QUARANTINE", "rationale": reason,
                               "applied_rules": [], "confidence": 1.0},
            "verdicts": [], "selection": {"chosen": None, "eliminations": [],
                                          "applied_rules": [], "notes": []},
            "entities": {}, "wall_ms": round((time.perf_counter() - started) * 1000),
            "log": log.lines}


def _todos_for(sel, ent: dict[str, Any], ticket, triggered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    t: list[dict[str, Any]] = []
    broken = ent["vehicle"].get("canonical_reg") or ticket.vehicle
    if sel.chosen:
        t.append({"do": f"Dispatch replacement {sel.chosen} to {ticket.destination or 'destination'}",
                  "why": "eligible candidate ranked by rule engine"})
        t.append({"do": f"Route recovery transport for broken vehicle {broken}", "why": "asset recovery plan"})
        t.append({"do": "Send the client communication once approved (draft is in the approval inbox)",
                  "why": "HITL gate"})
    else:
        t.append({"do": f"Dispatch roadside assistance on {broken}", "why": "no eligible replacement found"})
        t.append({"do": f"Ground {broken} for maintenance inspection before next assignment",
                  "why": "prevent recurring breakdown"})
        t.append({"do": "Notify client of the revised ETA", "why": "contract communication"})

    for v in triggered:
        rid = v["rule_id"]
        if rid == "R_SHAKTI_36H":
            t.append({"do": "Plan load to 36h door-to-door (skip the legacy 48h contract)",
                      "why": "Shakti Cement contract"})
        elif rid == "R_ORION_2020":
            t.append({"do": "Assign only a 2020+ unit (newest preferred) for this client",
                      "why": "Orion Pharma SLA"})
        elif rid == "R_VERTEX_6PM_GATE":
            t.append({"do": "Arrive before 18:00 or book the morning delivery slot",
                      "why": "Vertex Ludhiana gate policy"})
        elif rid == "R_OVERDUE_SERVICE_GROUNDED":
            t.append({"do": "Schedule service before the unit is reassigned",
                      "why": "overdue-service grounding rule"})
        elif rid == "R_JUGAAD_7D":
            t.append({"do": "Respect the 7-day / home-region jugaad restriction",
                      "why": "jogadi repair clause"})
    return t


def _recommendation(sel, ent, ticket, triggered, steps, started) -> dict[str, Any]:
    if sel.chosen:
        outcome = "REASSIGN"
        rationale = (f"Dispatch {sel.chosen} from the eligible pool "
                     f"({sel.pool_size} candidates considered, {len(sel.eliminations)} eliminated). "
                     f"Rules applied: {', '.join(sel.applied_rules) or 'none'}. "
                     + (" ".join(sel.notes[:2]) + ". " if sel.notes else ""))
    else:
        outcome = "ROADSIDE_ASSIST"
        rationale = ("No eligible replacement vehicle. Work order dispatches roadside "
                     f"assistance on the broken unit. Eliminations: "
                     f"{', '.join(e.get('reason', '') for e in sel.eliminations[:3])}" or "candidates exhausted")
    return {"outcome": outcome, "vehicle": sel.chosen,
            "rationale": rationale.strip(), "applied_rules": sel.applied_rules,
            "confidence": 0.97 if sel.chosen else 0.9,
            "wall_ms": round((time.perf_counter() - started) * 1000)}


# ------------------------------------------------------------ free question
def answer_question(question: str, *, collection: str = "merged",
                    top_k: int = 8, log: TraceLog | None = None) -> dict[str, Any]:
    """Ad-hoc advisor: retrieve real context, scan rules, return an actionable answer."""
    log = _coerce_log(log)
    steps: list[dict[str, Any]] = []
    started = time.perf_counter()

    t0 = time.perf_counter()
    log("step 1/4 understand — parse the question into a retrieval topic")
    _step(steps, "understand", "Understand the question",
          "Interpret the query as a knowledge-base + rules lookup",
          {"question": question}, {"topic": _topic(question)}, t0)

    t0 = time.perf_counter()
    log(f"step 2/4 retrieve — search knowledge base (collection={collection}, top_k={top_k})")
    from app.run import _build_services
    svc = _build_services()
    try:
        rich = svc.store.search_rich(question, collection=collection, top_k=top_k)
        hits = rich.get("hits", [])
    except Exception as exc:
        hits = []
        log(f"retrieval unavailable ({exc})")
    hits = [{k: h.get(k) for k in ("chunk_id", "score", "source", "document_type",
                                    "language", "text")} for h in hits]
    _step(steps, "retrieve", "Retrieve supporting context",
          "Embed the question and score chunks against the knowledge base",
          {"question": question, "collection": collection}, hits, t0)

    t0 = time.perf_counter()
    log("step 3/4 scan rules — find dispatcher rules relevant to this question")
    relevant = _relevant_rules(question)
    _step(steps, "scan_rules", "Scan the dispatch rule catalogue",
          "Keyword-match the question against rule conditions, actions and notes",
          {"question": question}, relevant, t0)

    t0 = time.perf_counter()
    answer, grounded = _ground_answer(question, hits)
    todos = [{"do": f"Follow rule {r['id']} before dispatching: {r['condition'][:120]}",
              "why": "extracted dispatcher knowledge"} for r in relevant[:3]]
    todos += [{"do": "Verify vehicle availability at the relevant hub before committing",
               "why": "fleet moves dispatch-time"},
              {"do": "Log the decision and its citations to the audit trail",
               "why": "exactly-once / traceability"}]
    rec = {"outcome": "ADVICE", "answer": answer,
           "grounded_in": grounded, "relevant_rules": [r["id"] for r in relevant],
           "wall_ms": round((time.perf_counter() - started) * 1000)}
    _step(steps, "recommend", "Ground the answer and plan actions",
          "Extractive answer from retrieved chunks + operator todos",
          {"question": question}, rec, t0)

    return {"mode": "question", "ticket_id": None, "identifier": _topic(question),
            "steps": steps, "todos": todos, "retrieved": hits,
            "recommendation": rec, "verdicts": [], "selection": {},
            "entities": {}, "wall_ms": round((time.perf_counter() - started) * 1000),
            "log": log.lines}


def _topic(question: str) -> str:
    q = question.strip()
    return q[:72] + ("…" if len(q) > 72 else "")


def _relevant_rules(question: str) -> list[dict[str, Any]]:
    from app.rules.engine import all_rule_specs

    toks = {w for w in question.lower().replace("?", " ").split() if len(w) > 2}
    out = []
    for spec in all_rule_specs():
        text = " ".join(str(spec.get(k) or "") for k in ("condition", "action", "client", "note"))
        words = set(text.lower().split())
        score = len(toks & words)
        if score:
            out.append({"id": spec["id"], "severity": spec.get("severity"),
                        "condition": spec.get("condition"), "action": spec.get("action"),
                        "citation": spec.get("citation"), "relevance": score})
    return sorted(out, key=lambda r: -r["relevance"])[:5]


def _ground_answer(question: str, hits: list[dict[str, Any]]) -> tuple[str, int]:
    if not hits:
        return ("No supporting knowledge found yet — index the relevant document "
                "in Knowledge Upload, then re-ask.", 0)
    top = hits[:3]
    best = top[0].get("score") or 0.0
    claims = []
    for h in top:
        text = (h.get("text") or "").strip()
        claims.append(text[:240])
    if best < 0.45:
        return ("Low-confidence answer — the closest chunk scores {:.2f}, which is below the "
                "0.45 retrieval floor. Treat this as unverified context.".format(best), 1)
    ans = ("Based on the retrieved knowledge:\n\n· " + "\n· ".join(claims) +
           "\n\nCross-check against the flagged rules before dispatching.")
    return ans, len(hits)


# ------------------------------------------------------------ scenario helper
def scenarios(use_baseline: bool = True) -> list[tuple[str, dict[str, Any]]]:
    """Named, pre-built scenarios for the one-click demo + a few baseline tickets."""
    out: list[tuple[str, dict[str, Any]]] = []
    for i, rec in enumerate(sample_records(4)):
        out.append((f"{rec.get('ticket_id')} · {rec.get('client')} · {rec.get('issue', '')[:34]}", rec))
    if use_baseline:
        try:
            for r in load_tickets()[:6]:
                out.append((f"BASELINE {r.ticket_id} · {r.client} · {r.issue[:34]}",
                            r.model_dump()))
        except Exception:
            pass
    return out