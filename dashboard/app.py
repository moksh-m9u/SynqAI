"""Meridian Freight — interactive operations console.

Entry point: navigation + page routing. Every page reads live artifacts / real
backend services (sandbox runs isolated, baseline untouched).
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard import base  # noqa: E402
from dashboard.widgets import inject_css  # noqa: E402
from dashboard.views import (  # noqa: E402
    analytics, audit, chaos, entities, hitl, knowledge,
    knowledge_upload, overview, pipeline, retrieval,
    rules, sandbox, tickets,
)

st.set_page_config(page_title="Meridian Freight Ops Console", layout="wide")
inject_css()

_PAGE = {
    "overview": overview,
    "sandbox": sandbox,
    "upload": knowledge_upload,
    "pipeline": pipeline,
    "ticket": tickets,
    "knowledge": knowledge,
    "rules": rules,
    "entities": entities,
    "retrieval": retrieval,
    "chaos": chaos,
    "hitl": hitl,
    "audit": audit,
    "analytics": analytics,
}


def main() -> None:
    st.sidebar.title("Meridian Freight")
    st.sidebar.caption("Breakdown \u2192 resolution ops console")
    current = st.session_state.get("nav", "overview")
    if current not in _PAGE:
        current = "overview"
    choice = st.sidebar.radio("Navigate", list(base.NAV.keys()),
                              index=list(base.NAV.values()).index(current),
                              key=f"nav_radio_{current}")
    selected = base.NAV.get(choice) or "overview"
    st.session_state["nav"] = selected
    _PAGE[selected].render()


if __name__ == "__main__":
    main()