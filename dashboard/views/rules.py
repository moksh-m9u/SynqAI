"""Rule Explorer + Rule Simulator — browse the extracted dispatch rules and test
how real inputs flip decisions using the SAME deterministic engine (not a copy)."""
from __future__ import annotations

from datetime import datetime

import streamlit as st

from app.ingest.models import CanonicalVehicle, Ticket
from app.knowledge.entities import (canonicalize_client, hub_distance_km, region_info)
from app.rules.engine import candidate_rejections, evaluate_ticket, selection_score, RuleContext

from dashboard import base
from dashboard.widgets import data_table, json_viewer, page_header

HUBS = ["Ambala", "Chandigarh", "Delhi", "Faridabad", "Gurgaon", "Jaipur",
        "Kanpur", "Lucknow", "Ludhiana", "Nainital", "Noida", "Rudrapur"]


def render() -> None:
    page_header(
        "Rule Explorer + Simulator",
        "Every dispatch rule the system extracted from the retiring dispatcher's knowledge — "
        "then test how clients, routes, seasons and vehicles change decisions live.",
        "The deterministic rule engine is the star exhibit: Python runs cited YAML rules, "
        "never semantic search. The simulator calls the exact same `evaluate_ticket` and "
        "`candidate_rejections` functions the pipeline uses.",
    )
    tab_rules, tab_sim = st.tabs(["Rule Explorer", "Rule Simulator"])
    with tab_rules:
        _explorer()
    with tab_sim:
        _simulator()


def _explorer() -> None:
    rules = base.rules()
    gcd = lambda r, k: r.get(k) or "any"
    c1, c2, c3, c4 = st.columns(4)
    sev = c1.multiselect("Severity", sorted({r.get("severity") for r in rules}))
    cli = c2.multiselect("Client", sorted({gcd(r, "client") for r in rules}))
    sea = c3.multiselect("Season", sorted({gcd(r, "season") for r in rules}))
    roa = c4.multiselect("Route", sorted({gcd(r, "route") for r in rules}))
    for r in rules:
        if sev and r.get("severity") not in sev:
            continue
        if cli and gcd(r, "client") not in cli:
            continue
        if sea and gcd(r, "season") not in sea:
            continue
        if roa and gcd(r, "route") not in roa:
            continue
        with st.expander(f"{r.get('id')} — {r.get('severity')} "
                         f"[client={gcd(r,'client')} season={gcd(r,'season')} route={gcd(r,'route')}]"):
            st.markdown(f"**Condition:** {r.get('condition')}")
            st.markdown(f"**Action:** {r.get('action')}")
            st.markdown(f"**Citation:** `{r.get('citation')}`")
            if r.get("note"):
                st.caption("Note: "+ r.get("note"))
    st.caption(f"{len(rules)} rules total — artifact: `artifacts/rules/rules_export.jsonl`")


def _simulator() -> None:
    st.markdown("### Simulate a dispatch decision")
    st.caption("Runs the real engine deterministically on a synthetic ticket + fleet.")
    c1, c2, c3 = st.columns(3)
    client_in = c1.text_input("Client (try: 'shakti cement hold', 'orion pharma')", "shakti cement hold")
    month = c2.selectbox("Month (season driver)", list(range(1, 13)), index=0,
                         help="Dec/Jan = winter NCR/hill; Jul-Sep = monsoon east of Lucknow")
    origin = c3.selectbox("Origin hub", HUBS, index=HUBS.index("Gurgaon"))
    d1, d2, d3 = st.columns(3)
    dest = d1.selectbox("Destination", HUBS, index=HUBS.index("Delhi"),
                        help="Hill = Nainital; Monsoon-E = Lucknow-adjacent")
    year = d2.number_input("Replacement vehicle year", 2014, 2025, 2023)
    bs = d3.selectbox("BS stage", ["BS6", "BS5", "BS4", "BS3", "BS2"], index=0)
    e1, e2 = st.columns(2)
    heater = e1.toggle("Engine heater", value=True)
    km = e2.number_input("km from origin hub", 0.0, 1500.0, 45.0, help="<=50 → local-hub dispatch rule")

    if st.button("Evaluate", type="primary", key="sim_go"):
        client = canonicalize_client(client_in)
        created = datetime(2025, month, 14, 22, 0)
        ticket = Ticket(
            ticket_id="SIM-0001", created_at=created, vehicle="SML-BROKEN",
            origin_hub=origin, km_from_origin_hub=km, destination=dest,
            issue="simulation", client=client, severity="HIGH")
        fleet = {
            "SML-MAIN": CanonicalVehicle(canonical_reg="SML-MAIN", vehicle_id="SML-MAIN", model="Truck",
                                         year=int(year), bs_stage=bs, engine_heater=bool(heater),
                                         home_hub=origin, capacity_tonnes=21.0, status="Active"),
            "SML-OLD": CanonicalVehicle(canonical_reg="SML-OLD", vehicle_id="SML-OLD", model="Truck",
                                        year=max(int(year) - 2, 2014), bs_stage="BS4",
                                        engine_heater=False, home_hub=origin, capacity_tonnes=16.0,
                                        status="Active"),
            "SML-NEW": CanonicalVehicle(canonical_reg="SML-NEW", vehicle_id="SML-NEW", model="Truck",
                                        year=int(year) + 1, bs_stage="BS6", engine_heater=True,
                                        home_hub=origin, capacity_tonnes=25.0, status="Active"),
        }
        broken = CanonicalVehicle(canonical_reg="SML-BRN", vehicle_id="SML-BRN", model="Truck",
                                  year=int(year) - 1, bs_stage="BS4", engine_heater=False,
                                  home_hub=origin, capacity_tonnes=20.0, status="Active")
        max_km = (ticket.km_from_origin_hub or 45) + 5
        minimal_fleet = {"SML-BRN": broken}
        ctx = RuleContext(ticket=ticket, client=client, vehicle=broken,
                          driver=None, fleet=minimal_fleet, maint_by_veh={}, run_id="simulator")
        verdicts = evaluate_ticket(ctx)

        st.markdown(f"Canonical client: **{client}** · route `{origin} → {dest}` (dist "
                    f"{hub_distance_km(origin, dest)} km) · month {month} · region "
                    f"{region_info(origin, dest)}")
        _show_tree(verdicts, fleet, ctx)


def _show_tree(verdicts: list, fleet, ctx: RuleContext) -> None:
    st.markdown("#### Decision tree")
    applied = [v for v in verdicts if v.triggered]
    rejected_verdicts = [v for v in verdicts if not v.triggered]
    dist = hub_distance_km(ctx.ticket.origin_hub, ctx.ticket.destination)
    st.markdown("**1. Ticket-level context rules (evaluated on intake):**")
    for v in applied:
        st.markdown(f"-  **{v.rule_id}** — {v.detail}")
    if not applied:
        st.markdown("- none triggered (this record keeps the default route)")
    with st.expander(f"Rules NOT triggered ({len(rejected_verdicts)}) — why not"):
        for v in rejected_verdicts:
            st.markdown(f"-  {v.rule_id}: {v.detail}")

    st.markdown("**2. Candidate-level eligibility (replacement vehicles):**")
    for reg, cand in fleet.items():
        if not cand:
            continue
        rejs = candidate_rejections(ctx, cand)
        ok = not rejs
        score, notes = selection_score(ctx, cand, dist)
        with st.expander(
            f"{reg} · year {cand.year} · {cand.bs_stage} · "
            f"heater={'yes' if cand.engine_heater else 'no'} — "
            f"**{'ELIGIBLE' if ok else 'REJECTED'}**",
            expanded=reg == "SML-MAIN"):
            if rejs:
                for r in rejs:
                    st.markdown(f"-  **{r['rule_id']}** — {r['reason']}")
            else:
                st.success("Passes every hard constraint and heuristic.")
            st.write(f"tie-break score = {score}")
            st.caption("Eligibility = R_DELIVERY_ELIGIBLE (active, not the broken vehicle, not "
                       "assigned) AND the season/route gates (BS6-on-NCR-winter, hill heater, "
                       "brake-30d, jugaad, service, Orion >=2020).")