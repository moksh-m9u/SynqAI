#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$PWD/.venv/bin:$PATH"
if [[ "${1:-}" == "api" ]]; then shift; exec uvicorn app.api.main:APP --host 0.0.0.0 --port 8000 "$@"; fi
if [[ "${1:-}" == "dashboard" ]]; then shift; exec streamlit run dashboard/app.py "$@"; fi
exec python -m app.run "$@"