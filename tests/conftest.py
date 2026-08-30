"""Redirect all artifact/output/audit paths to a throwaway temp dir so tests
never pollute the real artifacts/ (schema recovery files, summaries, etc.).
app.config reads these env vars at import time; pytest imports this module
before any test module, so the override is in place for every test."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="synq_test_")
os.environ.setdefault("SYNQ_ARTIFACTS_DIR", os.path.join(_TMP, "artifacts"))
os.environ.setdefault("SYNQ_OUTPUTS_DIR", os.path.join(_TMP, "outputs"))
os.environ.setdefault("SYNQ_AUDIT_DIR", os.path.join(_TMP, "audit"))