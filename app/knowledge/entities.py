"""Entity resolution: canonical vehicles, clients, drivers, hubs.

Rules of resolution (documented precedence — never a silent guess):
1. A registry row that carries a vehicle_id (MF-xxx) is the canonical master copy;
   ID-less duplicate-format rows are treated as alias copies (sync duplicates).
2. Field-level conflicts within fleet_master resolve to the vehicle_id row; the
   losing value is recorded in provenance with a conflict note.
3. Cross-source facts: fleet_master wins over email/hub claims for vehicle
   attributes (email thread_21 mandates verifying against the fleet master);
   maintenance_log wins over yard-check odometer claims (email thread_22).
4. Client names resolve through an alias table; unknown names are kept verbatim.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from app.config import ARTIFACTS_DIR, HUB_COORDS_PATH
from app.ingest.models import CanonicalClient, CanonicalVehicle, Driver, Vehicle
from app.utils import atomic_write_json, canonicalize_reg
from math import atan2, cos, radians, sin, sqrt

# ---------------------------------------------------------------- clients
_CLIENT_ALIASES: dict[str, list[str]] = {
    "Shakti Cement": ["shakti cement", "shakti", "shakticement", "shakti cement (hold)"],
    "Vertex Retail": ["vertex retail", "vertex", "vertexretail"],
    "Apex Chemicals": ["apex chemicals", "apex", "apexchem", "apex chemicals (rotation)"],
    "Orion Pharma": ["orion pharma", "orion", "orionpharma", "orion pharma (age-2020+)"],
    "Internal": ["internal", "meridian freight", "meridian pvt ltd", "mfl", "meridian", "synq", ""],
}

_ALIAS_TO_CANON: dict[str, str] = {}
for _canon, _aliases in _CLIENT_ALIASES.items():
    _ALIAS_TO_CANON[_canon.lower()] = _canon
    for _a in _aliases:
        _ALIAS_TO_CANON[_a.lower().strip()] = _canon


def canonicalize_client(name: str | None) -> str:
    key = (name or "").strip().lower()
    if key in _ALIAS_TO_CANON:
        return _ALIAS_TO_CANON[key]
    # fuzzy-ish: contains match for common names
    for canon, aliases in _CLIENT_ALIASES.items():
        if any(a in key for a in aliases if a):
            return canon
    return (name or "").strip() or "Internal"


def client_aliases() -> dict[str, list[str]]:
    return {k: v for k, v in _CLIENT_ALIASES.items()}


# ---------------------------------------------------------------- hubs
def _load_hub_coords() -> dict[str, dict[str, float]]:
    with open(HUB_COORDS_PATH) as fh:
        return json.load(fh)


_HUB_COORDS = _load_hub_coords()
_NCR = {"Delhi", "Gurgaon", "Faridabad", "Noida"}
_HILL = {"Rudrapur", "Nainital"}


def hub_lookup(name: str | None) -> dict[str, float] | None:
    if not name:
        return None
    key = name.strip().title()
    return _HUB_COORDS.get(key) or _HUB_COORDS.get(name.strip())


def haversine_km(p1: dict[str, float], p2: dict[str, float]) -> float:
    r = 6371.0
    dlat = radians(p2["lat"] - p1["lat"])
    dlon = radians(p2["lon"] - p1["lon"])
    a = sin(dlat / 2) ** 2 + cos(radians(p1["lat"])) * cos(radians(p2["lat"])) * sin(dlon / 2) ** 2
    return r * 2 * atan2(sqrt(a), sqrt(1 - a))


def hub_distance_km(a: str | None, b: str | None) -> float | None:
    pa, pb = hub_lookup(a), hub_lookup(b)
    if not pa or not pb:
        return None
    return round(haversine_km(pa, pb), 1)


def nearest_hubs(origin: str, excluded: set[str] | None = None) -> list[tuple[str, float]]:
    po = hub_lookup(origin)
    if not po:
        return []
    out = []
    for name, coord in _HUB_COORDS.items():
        if excluded and name in excluded:
            continue
        if name in _NCR or name in _HILL:
            continue
        out.append((name, round(haversine_km(po, coord), 1)))
    return sorted(out, key=lambda x: x[1])


def region_info(origin_hub: str | None, destination: str | None) -> dict[str, bool]:
    """Geographic flags a rule needs, evaluated deterministically."""
    o = (origin_hub or "").strip().title()
    d = (destination or "").strip().title()
    touches_ncr = o in _NCR or d in _NCR
    hill = d in _HILL
    # East of Lucknow (monsoon): destination longitude beyond Lucknow
    pl, pd_c = hub_lookup("Lucknow"), hub_lookup(d if d else "")
    east = bool(pl and pd_c and pd_c["lon"] > pl["lon"] + 0.5)
    return {"touches_ncr": touches_ncr, "hill": hill, "east_lucknow": east}


# ---------------------------------------------------------------- vehicles
def _pick_canonical(group: list[Vehicle]) -> tuple[CanonicalVehicle, dict[str, Any]]:
    """Choose the canonical record for a set of rows sharing one plate."""
    with_id = [v for v in group if v.vehicle_id]
    chosen = with_id[0] if with_id else group[0]
    provenance: dict[str, Any] = {
        "rows": [{"vehicle_id": v.vehicle_id, "registration_number": v.registration_number,
                  "year": v.year, "bs_stage": v.bs_stage, "engine_heater": v.engine_heater,
                  "home_hub": v.home_hub, "capacity": v.capacity_tonnes, "status": v.status,
                  "source": v.source_file} for v in group],
        "resolution": "row_with_vehicle_id_wins" if with_id else "best_available_row",
    }
    conflicts = []
    for attr in ("year", "bs_stage", "engine_heater", "home_hub", "capacity_tonnes"):
        vals = sorted({getattr(v, attr) for v in group if getattr(v, attr) not in (None, "")})
        if len(vals) > 1:
            conflicts.append({"attribute": attr, "values": [v for v in vals if v is not None],
                              "winner": getattr(chosen, attr),
                              "rationale": "canonical row (vehicle_id present) wins; losing value preserved in provenance"})
    if conflicts:
        provenance["conflicts"] = conflicts

    heater = (chosen.engine_heater or "").strip().lower()
    cv = CanonicalVehicle(
        canonical_reg=canonicalize_reg(chosen.registration_number),
        vehicle_id=chosen.vehicle_id,
        model=chosen.model,
        year=chosen.year,
        bs_stage=(chosen.bs_stage or "").upper(),
        engine_heater=heater in {"yes", "y"},
        home_hub=chosen.home_hub,
        capacity_tonnes=chosen.capacity_tonnes,
        status=chosen.status,
        aliases=[v.registration_number for v in group],
        provenance=provenance,
    )
    return cv, provenance


def build_vehicle_registry(vehicles: list[Vehicle]) -> dict[str, CanonicalVehicle]:
    groups: dict[str, list[Vehicle]] = defaultdict(list)
    for v in vehicles:
        groups[canonicalize_reg(v.registration_number)].append(v)

    registry: dict[str, CanonicalVehicle] = {}
    provenance_index: dict[str, Any] = {}
    conflicts_report: list[dict[str, Any]] = []
    for canon_reg, group in groups.items():
        cv, prov = _pick_canonical(group)
        registry[canon_reg] = cv
        provenance_index[canon_reg] = prov
        if prov.get("conflicts"):
            for c in prov["conflicts"]:
                conflicts_report.append({"plate": canon_reg, **c})

    artifact = {
        "generated_by": "app.knowledge.entities.build_vehicle_registry",
        "precedence": [
            "1. fleet_master row WITH vehicle_id is canonical",
            "2. fleet_master wins over email/hub claims for vehicle attributes",
            "3. maintenance_log wins over yard-check odometer claims",
        ],
        "conflicts_resolved": conflicts_report,
        "vehicles": [
            {**cv.model_dump(), "aliases": cv.aliases}
            for cv in registry.values()
        ],
    }
    atomic_write_json(ARTIFACTS_DIR / "entities" / "vehicles.json", artifact)
    return registry


def build_driver_registry(drivers: list[Driver]) -> dict[str, Driver]:
    registry = {d.driver_id: d for d in drivers if d.driver_id}
    artifact = [{"driver_id": d.driver_id, "home_hub": d.home_hub,
                 "joining_date": d.joining_date.isoformat(),
                 "phone_masked": d.phone, "dl_masked": d.dl_number, "aadhaar_masked": d.aadhaar}
                for d in drivers]
    atomic_write_json(ARTIFACTS_DIR / "entities" / "drivers.json", artifact)
    return registry


def build_client_registry() -> dict[str, CanonicalClient]:
    registry = {
        c: CanonicalClient(canonical_name=c, aliases=_CLIENT_ALIASES[c],
                           provenance={"source": "app.knowledge.entities", "basis": "alias table"})
        for c in _CLIENT_ALIASES
    }
    atomic_write_json(ARTIFACTS_DIR / "entities" / "clients.json",
                      [c.model_dump() for c in registry.values()])
    return registry