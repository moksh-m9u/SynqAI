"""Core unit tests for the Meridian pipeline (no network, deterministic)."""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import date, datetime

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- PII masking
def test_mask_phone():
    from app.ingest.masking import mask
    out = mask("call driver at +91 98765 43210 or 7012345678; aadhaar 1234-5678-9012")
    assert "98765" not in out and "7012345678" not in out
    assert "1234-5678-9012" not in out
    assert out.count("***MASKED***") == 3


def test_mask_dl():
    from app.ingest.masking import mask
    out = mask("dl UP32 2020 123456")
    assert "2020 123456" not in out and out.count("***MASKED***") == 1


def test_mask_keeps_vehicle_plates():
    from app.ingest.masking import mask
    out = mask("vehicle UP-40-IM-3144 broke down near Meerut")
    assert "UP-40-IM-3144" in out


# ---------------------------------------------------------------- canonical entities / utils
def test_canonicalize_reg_variants():
    from app.utils import canonicalize_reg
    assert canonicalize_reg("up 86 cm 7252") == canonicalize_reg("UP86CM7252")
    assert canonicalize_reg("UP-40-IM-3144") == "UP40IM3144"


# ---------------------------------------------------------------- preprocessing
def test_dedupe_first_wins_and_passes_missing_ids():
    from app.pipeline.preprocess import dedupe
    recs = [
        {"ticket_id": "TKT-1", "vehicle": "UP86CM7252"},
        {"ticket_id": "TKT-1", "vehicle": "UP86CM7252", "note": "(sync copy)"},
        {"tkt_no": "X-1", "vehicle": "UP86CM7252"},     # missing ticket_id -> rescue path
    ]
    canon, dupes = dedupe(recs)
    assert len(canon) == 2 and len(dupes) == 1


def test_prepare_quarantines_broken_but_never_crashes():
    from app.pipeline.preprocess import prepare
    broken = {"ticket_id": "TKT-999", "created_at": "not-a-date",
              "vehicle": "hr??unknown", "origin_hub": "", "destination": "",
              "issue": ""}
    pt = prepare(broken, set())
    assert pt.status == "quarantined"
    assert "created_at" in pt.reason


def test_schema_recovery_aliases():
    from app.ingest.schema_recovery import recover_record
    raw = {"tkt_no": "SUR-1", "breakdown_when": "2026-09-02T14:30:00", "truck": "UP-40-IM-8494",
           "driver": "DRV-014", "far_from_base_km": 65, "from_hub": "Meerut", "to_city": "Delhi",
           "symptom": "clutch slipping", "priority": "HIGH", "account": "Orion Pharma"}
    norm, report = recover_record(raw, run_id="test")
    assert norm is not None
    assert norm["ticket_id"] == "SUR-1" and norm["vehicle"] == "UP-40-IM-8494"
    assert norm["km_from_origin_hub"] == 65.0 and norm["client"] == "Orion Pharma"
    assert report["mapping"]["truck"] == "vehicle"


# ---------------------------------------------------------------- outbox exactly-once
def test_outbox_exactly_once(tmp_path):
    from app.pipeline.outbox import Outbox
    db = tmp_path / "state.sqlite"
    out = tmp_path / "work_orders.jsonl"
    o = Outbox(db, outputs_dir=tmp_path)
    wo = {"work_order_id": "WO-1", "ticket_id": "T1", "vehicle_reg": "UP86CM7252",
          "created_at": "2026-01-01 00:00:00", "citations": []}
    first = o.write_work_order(wo, "run-1")
    second = o.write_work_order(dict(wo, vehicle_reg="OTHER"), "run-2")
    assert first["vehicle_reg"] == "UP86CM7252"
    assert second["vehicle_reg"] == "UP86CM7252"  # idempotent: no double write
    assert len([l for l in out.read_text().splitlines() if l.strip()]) == 1


# ---------------------------------------------------------------- rule engine
def _ticket(**kw):
    from app.ingest.models import Ticket
    base = dict(ticket_id="T1", created_at="2026-11-15T18:00:00", vehicle="UP40IM3144",
                origin_hub="Delhi", destination="Agra", issue="engine failure",
                severity="HIGH", client="Shakti Cement", status="OPEN")
    base.update(kw)
    return Ticket.model_validate(base)


def test_rule_verdicts_origin_and_client():
    from app.rules.engine import RuleContext, evaluate_ticket, build_eta_cache
    ctx = RuleContext(ticket=_ticket(km_from_origin_hub=20.0), client="Shakti Cement",
                      vehicle=None, driver=None, fleet={}, maint_by_veh={}, run_id="t")
    vs = {v.rule_id: v.triggered for v in evaluate_ticket(ctx)}
    assert vs["R_ORIGIN_50KM"] is True
    assert vs["R_NEAREST_HUB"] is False
    assert vs["R_SHAKTI_36H"] is True
    assert vs.get("R_APEX_ROTATION", False) is False  # client-scoped rules are absent otherwise


def test_overdue_service_grounded_reports_insufficient_info():
    from app.ingest.models import CanonicalVehicle
    from app.rules.engine import RuleContext, evaluate_ticket
    veh = CanonicalVehicle(canonical_reg="UP40IM3144", vehicle_id="MF-X", model="ABC",
                           year=2020, bs_stage="BS6", engine_heater=False, home_hub="Delhi",
                           capacity_tonnes=16.0, status="Active")
    ctx = RuleContext(ticket=_ticket(), client="Shakti Cement", vehicle=veh, driver=None,
                      fleet={}, maint_by_veh={}, run_id="t")
    vs = {v.rule_id: v for v in evaluate_ticket(ctx)}
    v = vs["R_OVERDUE_SERVICE_GROUNDED"]
    # no service-due records exist -> grounded must NOT trigger without hard evidence
    assert v.triggered is False
    assert "insufficient" in (v.detail or "").lower() or v.evidence.get("due_date_evidence") == []


# ---------------------------------------------------------------- selection logic
def test_selection_within_50km_scopes_to_origin_hub():
    from app.ingest.models import CanonicalVehicle
    from app.rules.engine import RuleContext
    from app.pipeline.select_vehicle import candidate_hubs_priority
    prio = candidate_hubs_priority("Lucknow", 20.0)
    assert prio == ["Lucknow"]


def test_candidate_rejections_mark_broken_vehicle():
    from app.ingest.models import CanonicalVehicle
    from app.rules.engine import RuleContext, candidate_rejections
    cand = CanonicalVehicle(canonical_reg="UP40IM3144", vehicle_id="MF-X", model="ABC",
                            year=2020, bs_stage="BS6", engine_heater=False, home_hub="Lucknow",
                            capacity_tonnes=16.0, status="Active")
    ctx = RuleContext(ticket=_ticket(), client="Shakti Cement", vehicle=cand, driver=None,
                      fleet={}, maint_by_veh={}, assigned={""}, run_id="t")
    rejs = {r["rule_id"]: r for r in candidate_rejections(ctx, cand)}
    assert "R_DELIVERY_ELIGIBLE" in rejs  # candidate is the broken vehicle


# ---------------------------------------------------------------- full prepare -> ids
def test_main_queue_counts_match_charter():
    from app.ingest.loaders import load_tickets
    from app.pipeline.preprocess import dedupe, prepare
    records = load_tickets(ROOT / "data" / "tickets.json")
    canon, dupes = dedupe(records)
    seen, valid, quar = set(), [], []
    for r in canon:
        pt = prepare(r, seen, run_id="test")
        (valid if pt.status == "valid" else quar).append(pt)
    assert len(records) == 35
    assert len(dupes) == 3
    assert len(quar) == 2
    assert len(valid) == 30
    assert {p.ticket_id for p in quar} == {"TKT-9101", "TKT-9102"}