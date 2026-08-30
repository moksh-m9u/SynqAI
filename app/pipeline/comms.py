"""Client communication drafting.

Qwen (Groq) drafts the tone; a deterministic per-client template is the offline
fallback. EVERY body is post-sanitized with the PII masker as defense-in-depth:
no personal datum ever appears in an outbound action."""
from __future__ import annotations

from typing import Any

from app.config import LLM_MODEL_ID
from app.ingest.masking import mask
from app.utils import build_llm


def _llm() -> Any | None:
    return build_llm()


def _eta_txt(eta: dict[str, Any], client: str) -> str:
    raw = eta.get("estimated_hours")
    if raw is None:
        return "We are estimating the delivery window from route distance; a confirmed ETA will follow."
    base = eta.get("base_hours")
    pad = eta.get("padding_frac", 0.0)
    txt = f"Estimated arrival in about {raw} hours"
    if pad:
        txt += " (includes the seasonal on-route allowance we quote for this corridor)"
    if client == "Shakti Cement":
        txt = (f"Estimated arrival in about {raw} hours — planned to the 36-hour door-to-door "
               "window we operate for Shakti dispatches.")
    if client == "Vertex Retail" and eta.get("vertex_hold_until_morning"):
        txt = (f"Estimated arrival after today's 18:00 gate close, so per our standing arrangement "
               f"the vehicle will be held at the last halt and delivered with the morning gate at 08:00 — "
               "recorded as a scheduled morning delivery, not a failed attempt.")
    return txt


_TEMPLATES = {
    "Shakti Cement": (
        "Subject: Replacement vehicle dispatched — {vehicle}\n"
        "Dear Shakti Cement Dispatch,\n"
        "Vehicle {vehicle} experienced a breakdown on {route} and is being attended to. "
        "A replacement vehicle is being dispatched from {hub}. {eta}\n"
        "Regards,\nMeridian Freight Dispatch"),
    "Vertex Retail": (
        "Subject: Replacement vehicle for your current dispatch\n"
        "Dear Vertex Retail Logistics,\n"
        "Vehicle {vehicle} broke down on {route}. Replacement {replacement} is being arranged from {hub}. {eta}\n"
        "Regards,\nMeridian Freight Dispatch"),
    "Apex Chemicals": (
        "Subject: Replacement vehicle for your dispatch\n"
        "Dear Apex Chemicals Stores,\n"
        "Vehicle {vehicle} had a breakdown on {route}. As previously advised, this incident does not affect your "
        "next placement: replacement {replacement} is a different vehicle, dispatched from {hub}. {eta}\n"
        "Regards,\nMeridian Freight Dispatch"),
    "Orion Pharma": (
        "Subject: Replacement vehicle (audit-compliant) for your dispatch\n"
        "Dear Orion Pharma Supply Chain,\n"
        "Vehicle {vehicle} suffered a breakdown on {route}. Replacement {replacement} (manufactured {year}) is "
        "being dispatched from {hub}, meeting the 2020-or-newer requirement for Orion consignments. {eta}\n"
        "Regards,\nMeridian Freight Dispatch"),
    "_default": (
        "Subject: Replacement vehicle for breakdown {vehicle}\n"
        "Dear {client},\n"
        "The vehicle on your current dispatch experienced a breakdown on {route}. "
        "Replacement {replacement} is being dispatched from {hub}. {eta}\n"
        "Regards,\nMeridian Freight Dispatch"),
}

NO_COMMS_COMMENT = "Internal/client-less ticket — no external client communication drafted."


def draft_communication(ctx: dict[str, Any], selection: dict[str, Any], run_id: str = "",
                        thread_id: str = "") -> dict[str, Any] | None:
    client = ctx.get("client", "")
    if client in ("Internal", "") and (ctx.get("client_source") or "").strip().lower() in ("internal", ""):
        return None

    reg = ctx.get("vehicle", {}).get("canonical_reg") if ctx.get("vehicle") else ""
    replacement = selection.get("chosen") or reg or "(to be advised)"
    year = ""
    if selection and "chosen_year" in selection:
        year = str(selection.get("chosen_year"))
    hub = selection.get("candidate_hub") or ctx.get("vehicle", {}).get("home_hub", "")
    route = f"{ctx.get('ticket', {}).get('origin_hub','?')} → {ctx.get('ticket', {}).get('destination','?')}"
    eta = ctx.get("eta", {})

    fallback = (_TEMPLATES.get(client) or _TEMPLATES["_default"]).format(
        client=client, vehicle=reg or "(vehicle)", replacement=replacement,
        hub=hub or "(hub)", route=route, eta=_eta_txt(eta, client), year=year)

    body = fallback
    llm = _llm()
    if llm is not None:
        try:
            prompt = (
                "You are Meridian Freight's dispatcher. Draft a short client email (subject + body, ~5 lines) "
                "about a replacement vehicle after a breakdown. It must be professional and factual. "
                "NEVER include any personal data (names of drivers, phone numbers, ID numbers, licence numbers). "
                "Return ONLY the email text.\n"
                f"Client: {client}\nBroken vehicle reg: {reg}\nReplacement vehicle reg: {replacement}\n"
                f"Dispatch hub: {hub}\nRoute: {route}\nETA detail: {_eta_txt(eta, client)}\n"
            )
            resp = llm.invoke(prompt)
            generated = resp.content if hasattr(resp, "content") else str(resp)
            if generated and len(generated) > 60:
                body = generated
        except Exception:
            pass  # deterministic fallback stands

    body = mask(body)  # defense-in-depth: no personal data, ever

    citations = [
        {"source": c.get("source", ""), "ref": c.get("ref", ""), "document_type": "dispatcher_rule"}
        for c in ctx.get("citations", []) if c.get("document_type") == "dispatcher_rule"
    ]
    if not citations:
        citations = [{"source": "dispatcher_interview.txt", "ref": rule, "document_type": "dispatcher_rule"}
                     for rule in (ctx.get("applied_rule_ids") or [])[:6]]

    return {
        "message_id": f"MSG-{ctx.get('ticket', {}).get('ticket_id','?')}",
        "ticket_id": ctx.get("ticket_id", ""),
        "recipient": _recipient_label(client, ctx.get("client_source", "")),
        "subject": body.splitlines()[0] if body.splitlines() else "Replacement vehicle dispatched",
        "body": body,
        "citations": citations,
        "context_summary": {
            "broken_vehicle": reg,
            "replacement": replacement,
            "hub": hub,
            "eta_hours": eta.get("estimated_hours"),
            "applied_rules": ctx.get("applied_rule_ids", []),
            "quarantine_note": "",
        },
        "status": "PENDING_APPROVAL",
        "run_id": run_id,
        "thread_id": thread_id,
    }


def _recipient_label(client: str, source: str) -> str:
    if client == "Internal":
        return "Internal Ops (no external client)"
    return client or source or "Client"