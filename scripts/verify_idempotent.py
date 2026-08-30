"""Idempotency verifier: run the pipeline, snapshot business-truth outputs, run
again, diff. Identical bytes prove exactly-once across reruns."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FILES = ["outputs/work_orders.jsonl", "outputs/comms_pending.jsonl",
         "outputs/comms_sent.jsonl", "outputs/quarantine.jsonl", "audit/audit.jsonl"]

VOLATILE = {"at"}  # audit wall-clock stamp differs run-to-run by design


def normalize_line(f: str, line: str) -> str:
    """Business-truth files are byte-identical across replays. The only exception
    is the audit log's wall-clock 'at' stamp (provenance, not a decision)."""
    if f == "audit/audit.jsonl":
        import json
        d = json.loads(line)
        for k in VOLATILE:
            d.pop(k, None)
        return json.dumps(d, sort_keys=True)
    return line


def snapshot(root: Path) -> dict[str, str]:
    out = {}
    for f in FILES:
        p = root / f
        if not p.exists():
            raise SystemExit(f"FATAL: missing business-truth file {f} — pipeline run failed")
        norm = [normalize_line(f, ln) for ln in p.read_text().splitlines() if ln.strip()]
        out[f] = hashlib.sha256("\n".join(norm).encode()).hexdigest()
    return out


def run_once(root: Path):
    r = subprocess.run([sys.executable, "-m", "app.run", "--queue", str(root / "data/tickets.json"),
                        "--approve", "auto"], cwd=root, capture_output=True, text=True)
    print(r.stdout[-1200:])
    if r.returncode != 0:
        print(r.stderr[-2000:])
        raise SystemExit(f"pipeline run failed rc={r.returncode}")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    print("run #1...")
    run_once(root)
    h1 = snapshot(root)
    print("run #2...")
    run_once(root)
    h2 = snapshot(root)
    ok = h1 == h2
    print("\n== idempotency check ==")
    for f in FILES:
        print(f"  {'OK ' if h1[f] == h2[f] else 'DIFF'}  {f}")
    print("IDENTICAL OUTPUTS" if ok else "NON-IDENTICAL OUTPUTS")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())