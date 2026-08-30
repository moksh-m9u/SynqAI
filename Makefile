PYTHON=.venv/bin/python
UNAME_S := $(shell uname -s)

.PHONY: setup build run verify api dashboard test clean

setup:
	python3 -m venv .venv
	$(PYTHON) -m pip install -U pip
	$(PYTHON) -m pip install -r requirements.txt

build:
	$(PYTHON) -c "from scripts.build_knowledge import build_knowledge_stack; build_knowledge_stack()"

run:
	$(PYTHON) -m app.run --queue data/tickets.json --approve auto

run-ask:
	$(PYTHON) -m app.run --queue data/tickets.json --approve ask

verify:
	$(PYTHON) scripts/verify_idempotent.py

api:
	$(PYTHON) -m uvicorn app.api.main:APP --host 0.0.0.0 --port 8000

dashboard:
	$(PYTHON) -m streamlit run dashboard/app.py

test:
	$(PYTHON) -m pytest -q

clean:
	rm -f artifacts/state.sqlite outputs/work_orders.jsonl outputs/comms_pending.jsonl \
	      outputs/comms_sent.jsonl outputs/quarantine.jsonl outputs/run_summary.json \
	      outputs/preprocess_report.json audit/audit.jsonl