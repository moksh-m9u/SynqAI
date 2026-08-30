"""Change-tolerance harness for the 'surprise ticket file' the evaluator drops in
the final hour. The file will not look exactly like the main queue; this runner:

- native load -> pydantic validation
- on failure -> LLM-aided schema recovery (Qwen maps keys, Python extracts values)
- records that still fail validation -> quarantine with an alert (never a crash)
- then runs the standard LangGraph pipeline end to end on whatever survived.

Usage:
    python scripts/surprise_harness.py --file path/to/surprise_tickets.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import ensure_dirs


def reveal_schema(fh: Path) -> None:
    try:
        data = json.loads(fh.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[surprise] FATAL: cannot even read the file as JSON — raising alert, no processing. {exc}")
        sys.exit(1)
    print(f"[surprise] file={fh.name} records={len(data)}")
    if data:
        print(f"[surprise] sample record keys: {list(data[0].keys())}")
        mismatches = [k for k in data[0] if k not in {
            "ticket_id", "created_at", "vehicle", "driver_id", "origin_hub",
            "km_from_origin_hub", "destination", "issue", "severity", "client", "status",
            "resolution_note"}]
        if mismatches:
            print(f"[surprise] unknown/renamed keys: {mismatches} -> schema recovery will be attempted")


def run(args) -> int:
    ensure_dirs()
    fh = Path(args.file)
    if not fh.exists():
        print(f"[surprise] file not found: {fh}")
        return 2
    reveal_schema(fh)

    from app.run import main as run_main
    sys.argv = ["run.py",
                "--queue", str(fh),
                "--approve", args.approve,
                "build-knowledge" if False else ""]
    sys.argv = [a for a in sys.argv if a]
    return run_main()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Surprise ticket-file harness")
    ap.add_argument("--file", required=True, help="path to the surprise ticket file")
    ap.add_argument("--approve", default="auto", choices=["auto", "ask"])
    args = ap.parse_args()
    sys.exit(run(args))