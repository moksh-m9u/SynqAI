# SynqAI — Meridian Freight breakdown-to-resolution

## One command to run everything

```bash
make setup        # create .venv + install deps (uses .env if present)
make build        # build knowledge store (chunks -> local embeddings -> Qdrant)
make run          # run the full pipeline on data/tickets.json (auto-approves drafts)
make verify       # prove idempotency: run twice, diff business-truth output
make api          # FastAPI service (get -> process -> approve)
make dashboard    # Streamlit operations dashboard (the demo surface)
make test         # pytest
```

`make run` is the primary entry point. It is a pure function of the queue file:
re-running it produces byte-identical `outputs/` and `audit/` files (see
`scripts/verify_idempotent.py`).

## What the system does

1. **Validate** — native load, Pydantic validation (`app/ingest/`).
2. **Deduplicate** — first occurrence wins; sync copies collapse.
3. **Quarantine** broken records with a reason + alert (never a crash).
4. **Schema recovery** for unknown file formats: Qwen (Groq) suggests the
   key-to-key mapping, Python performs the extraction (`app/ingest/schema_recovery.py`).
5. **Enrich** — canonical entities for vehicle / driver / client / SLA /
   maintenance / trip history, with citations.
6. **Apply dispatcher rules** — 14 deterministic rules encoded from Rajender's
   interview + the 40 email threads (`app/rules/rules.yaml`, `app/rules/engine.py`).
7. **Select replacement** — rule-first, deterministic tie-break,
   documented eliminations for every candidate.
8. **Work order** — exactly one per ticket (outbox pattern, `app/pipeline/outbox.py`).
9. **Client comms draft** then **HITL gate** (`interrupt()`) — sent only after approval.
10. **Audit** — every decision replayable (`audit/audit.jsonl` + SQLite + LangGraph
    thread + node artifacts under `artifacts/`).

## Layout

```
app/            orchestration, ingest, knowledge, rules, pipeline, api
dashboard/      Streamlit operations dashboard (9 pages, reads artifacts from disk)
scripts/        build_knowledge, verify_idempotent, surprise_harness
data/           challenge sources (tickets, fleet, trips, maintenance, roster,
                dispatcher interview, emails/)
artifacts/      knowledge chunks, rules, entities, schema, retrieval, graph, traces
outputs/        business truth: work_orders.jsonl, comms_pending.jsonl,
                comms_sent.jsonl, quarantine.jsonl, run_summary.json
audit/          audit.jsonl (one line per step per ticket)
```

## Business truth (never violate these schemas)

- `outputs/work_orders.jsonl`  `{work_order_id,ticket_id,vehicle_reg,created_at,citations}`
- `outputs/comms_pending.jsonl`  drafts awaiting HITL
- `outputs/comms_sent.jsonl`  `{message_id,ticket_id,recipient,body,approved_by,sent_at}`
- `outputs/quarantine.jsonl`  broken records + reasons
- `audit/audit.jsonl`  every decision, per run

Idempotency: `run_id` is derived from the input queue hash and `approved_by` is
`Approver(auto)`; `sent_at` is the canonical ticket event time. The only field
that differs across replays is the audit log's wall-clock `at` stamp (provenance).

## LLM policy

- Groq `qwen/qwen3.8-27b` (override via `GROQ_MODEL` in `.env`).
- Qwen is used ONLY for schema recovery, structured extraction, and client
  message drafting. Duplicate detection, vehicle eligibility, rule execution,
  ETA, idempotency and filtering are deterministic Python.

## Change-tolerance drill (final-hour "surprise file")

```bash
python scripts/surprise_harness.py --file data/surprise_demo/surprise_upload.json --approve auto
```

Feeds a file with renamed keys (`tkt_no`, `truck`, `from_hub`, ...) and a broken
record: 2 records rescued by schema recovery, 1 quarantined, zero crashes, and
full audit/artifacts for everything.

## Env

Configure `.env` (already present): `GROQ_API_KEY`, `QDRANT_API_KEY`,
`QDRANT_CLUSTER_ENDPOINT`, `LANGCHAIN_API_KEY`, `HUGGINGFACEHUB_API_TOKEN`,
`LANGCHAIN_PROJECT=SynqAI`, `LANGCHAIN_TRACING_V2=true`.