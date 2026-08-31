"""Entity Explorer — resolve messy registrations/clients interactively and inspect
the actual conflicts the entity-resolution layer found and how each was decided."""
from __future__ import annotations

import streamlit as st

from app.knowledge.entities import canonicalize_client, client_aliases
from dashboard import base
from dashboard.widgets import data_table, json_viewer, page_header


def render() -> None:
    page_header(
        "Entity Explorer",
        "Type a messy registration ('ch81aq4130') or client name ('shakti cement hold') and watch "
        "the resolver land on the canonical identity — with aliases, provenance and any conflicts.",
        "Entity resolution is the backbone of dedup + rule correctness. Messy plates and client "
        "names must resolve identically across retries and teams, and every conflict must be "
        "explainable.",
    )
    tab_v, tab_c, tab_conf, tab_dr = st.tabs(
        ["Vehicle resolver", "Client resolver", "Conflict inspector", "Registry"])

    veh = base.jload(base.ARTIFACTS_DIR / "entities"/ "vehicles.json") or {}
    vehicles = veh.get("vehicles", [])
    conflicts = veh.get("conflicts_resolved", [])
    clients = base.jload(base.ARTIFACTS_DIR / "entities"/ "clients.json") or []
    drivers = base.jload(base.ARTIFACTS_DIR / "entities"/ "drivers.json") or []

    with tab_v:
        _vehicle_resolver(vehicles)
    with tab_c:
        _client_resolver(clients)
    with tab_conf:
        st.markdown(f"{len(conflicts)} conflicts resolved during registry build — "
                    "every one has a cited winner and preserved loser.")
        if conflicts:
            data_table(conflicts, "conflicts", height=420)
        else:
            st.info("No attribute-level conflicts in the canonical fleet.")
    with tab_dr:
        st.markdown(f"{len(vehicles)} canonical vehicles · {len(drivers)} drivers · "
                    f"{len(clients)} canonical clients")
        json_viewer(veh, "vehicles_artifact", expanded=False)
        json_viewer(clients, "clients_artifact", expanded=False)


def _vehicle_resolver(vehicles: list[dict]) -> None:
    st.write(f"**{len(vehicles)} canonical vehicles** in fleet_master + email/hub realizations.")
    q = st.text_input("Registration (messy ok)", "CH81AQ4130", key="ent_reg",
                      help="Dashes, spaces, case and variant letters are normalized.")
    if not q.strip():
        return
    from app.utils import canonicalize_reg
    canon = canonicalize_reg(q)
    hit = next((v for v in vehicles if v.get("canonical_reg") == canon), None)
    if not hit:
        st.warning(f"No vehicle resolves to `{canon}` — is it in the roster? Resolver never invents vehicles.")
        return
    st.success(f"Resolved **{q.strip()}** → `{hit.get('canonical_reg')}`")
    c1, c2 = st.columns(2)
    with c1:
        st.metric("vehicle_id", hit.get("vehicle_id"))
        st.markdown(f"- model: {hit.get('model')}\n- year: {hit.get('year')}\n"
                    f"- BS stage: {hit.get('bs_stage')}\n- engine heater: "
                    f"{'yes' if hit.get('engine_heater') else 'no'}\n- home hub: "
                    f"{hit.get('home_hub')}\n- capacity: {hit.get('capacity_tonnes')}t\n"
                    f"- status: {hit.get('status')}")
    with c2:
        st.markdown("**Aliases (all spellings that resolve here)**")
        for a in hit.get("aliases") or []:
            st.markdown(f"- `{a}`")
    st.subheader("Provenance — the rows behind the decision")
    json_viewer(hit.get("provenance"), "veh_prov", expanded=True)
    if hit.get("provenance", {}).get("conflicts"):
        st.warning(f"{len(hit['provenance']['conflicts'])} attribute conflicts — see Conflict inspector.")


def _client_resolver(clients: list[dict]) -> None:
    st.write("**Canonical clients** built from tickets + emails; one canonical id per real company.")
    q = st.text_input("Messy client (try 'shakti cement hold' or 'orion')", "shakti cement hold",
                      key="ent_cli")
    if not q.strip():
        return
    canonical = canonicalize_client(q)
    st.success(f"Resolved **{q.strip()}** → **{canonical}**")
    aliases = client_aliases()
    matched = []
    ql = q.strip().lower()
    for canon, alist in aliases.items():
        for a in alist:
            if a and (a in ql or ql in a):
                score = 0.95 if a == ql else 0.82
                matched.append({"canonical": canon, "matched_alias": a, "confidence": score})
    if matched:
        data_table(matched, "client_match", columns=["canonical", "matched_alias", "confidence"])
    else:
        st.caption("Could not map to a known canonical alias — resolution returns the raw label "
                   "(unknown clients still route safely).")