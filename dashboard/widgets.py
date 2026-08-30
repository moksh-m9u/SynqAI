"""Reusable Streamlit UI: page headers with the mandatory "Why this matters"
section, JSON viewers with copy/download/search, data tables with CSV export,
and a thread-based live-run runner that streams pipeline logs into the UI.
"""
from __future__ import annotations

import time
from functools import lru_cache
from typing import Any, Callable

import pandas as pd
import streamlit as st

GLOBAL_CSS = """
<style>
.block-container {padding-top: 1.2rem; padding-bottom: 3rem;}
[data-testid="stSidebar"] .stRadio label {font-size: 0.95rem;}
.why-card {
  border-left: 3px solid #2f81f7; border-radius: 6px;
  background: rgba(47,129,247,0.08); padding: 0.6rem 0.9rem; margin: 0.4rem 0 1rem;
  color: #c9d4e3; font-size: 0.9rem;
}
.node-pill {
  display:inline-block; border-radius: 14px; padding: 0.25rem 0.7rem; margin: 0.12rem;
  font-size: 0.78rem; border: 1px solid #444;
}
.node-done {background:#173a20; color:#7ee787; border-color:#2ea043;}
.node-running {background:#2f1b00; color:#fbbf24; border-color:#f0883e;}
.node-wait {background:#161b22; color:#8b949e; border-color:#30363d;}
.legend {font-size:0.78rem; color:#8b949e;}
.kicker {letter-spacing: 0.12em; text-transform: uppercase; font-size: 0.7rem; color:#8b949e;}
</style>
"""


def inject_css() -> None:
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)


def page_header(title: str, blurb: str, why: str, icon: str = "") -> None:
    st.markdown(f"<div class='kicker'>{icon} Meridian Freight · Operations Console</div>",
                unsafe_allow_html=True)
    st.title(title)
    st.markdown(blurb)
    with st.expander("Why this matters", expanded=False):
        st.write(why)
    st.divider()


def _inline_name(key: str, widget: str) -> str:
    return f"{widget}__{key}"


def json_viewer(data: Any, key: str, expanded: bool = True) -> None:
    """Copy (streamlit-native), download, collapse/expand and search for any JSON."""
    import json as _json
    text = _json.dumps(data, indent=2, ensure_ascii=False, default=str) if data is not None else "null"
    c1, c2, c3, _ = st.columns([1, 1, 1, 2.4])
    with c1:
        expanded = st.toggle("Collapse / expand", value=expanded, key=_inline_name(key, "exp"))
    with c2:
        st.download_button("Download JSON", text, file_name=f"{key}.json", mime="application/json",
                           key=_inline_name(key, "dl"))
    with c3:
        q = st.text_input("Search inside JSON", "", key=_inline_name(key, "q"))

    view = _json_filter(data, q) if q.strip() else data
    box = st.expander(f"{key} — {len(text)} chars (browser JSON has a native copy button)", expanded=False)
    with box:
        st.json(view, expanded=expanded)


def _json_filter(obj: Any, q: str) -> Any:
    ql = q.lower()
    if isinstance(obj, dict):
        return {k: _json_filter(v, q) for k, v in obj.items() if ql in str(k).lower() or _matches(v, ql)}
    if isinstance(obj, list):
        return [_json_filter(x, q) for x in obj if _matches(x, ql)]
    return obj


def _matches(v: Any, ql: str) -> bool:
    try:
        return any(ql in str(x).lower() for x in v.values()) if isinstance(v, dict) else ql in str(v).lower()
    except Exception:
        return False


def data_table(rows: list[dict[str, Any]], key: str, columns: list[str] | None = None,
               rename: dict[str, str] | None = None, height: int = 420, sort_by: str | None = None) -> None:
    """Search + column sort + native column filter + CSV export over a list of dicts."""
    if not rows:
        st.info("No rows to show.")
        return
    face = ["text", "body", "raw_masked", "prompt", "llm_output", "vector"]
    cols = columns or [c for c in rows[0] if c not in face]
    keep = [c for c in cols if any(c in r for r in rows)]
    df = pd.DataFrame([{k: (_fmt(v)) for k, v in r.items() if k in keep} for r in rows])
    if rename:
        df = df.rename(columns={k: v for k, v in rename.items() if k in df})
    q = st.text_input("Search rows", "", key=_inline_name(key, "tq")).strip().lower()
    if q:
        mask = df.astype(str).agg("| ".join, axis=1).str.lower().str.contains(q, regex=False)
        df = df[mask]
    if sort_by and sort_by in df:
        df = df.sort_values(sort_by)
    st.caption(f"{len(df)} rows")
    st.dataframe(df.reset_index(drop=True), use_container_width=True, height=height,
                 key=_inline_name(key, "df"))
    st.download_button("Export CSV", df.to_csv(index=False).encode("utf-8"),
                       file_name=f"{key}.csv", mime="text/csv", key=_inline_name(key, "csv"))


def _fmt(v: Any) -> Any:
    if isinstance(v, (list, dict)):
        return json_dumps_short(v)
    return v


def json_dumps_short(v: Any) -> str:
    import json as _json
    try:
        s = _json.dumps(v, ensure_ascii=False, default=str)
        return s[:160] + ("…"if len(s) > 160 else "")
    except Exception:
        return str(v)


def run_job(label: str, fn: Callable) -> dict:
    """Run a blocking backend call on a worker thread while streaming its log
    lines into the UI. Returns {"ok": bool, "result": ...}."""
    import threading
    lines: list[str] = []
    holder: dict[str, Any] = {"ok": False, "error": None, "result": None}
    box = st.empty()
    bar = st.progress(0.0)

    def body():
        try:
            holder["result"] = fn(log=lambda s: lines.append(str(s)))
            holder["ok"] = True
        except Exception as exc:  # noqa: BLE001
            holder["error"] = exc
            lines.append(f"ERROR: {exc}")
        finally:
            holder["done"] = True

    t = threading.Thread(target=body, daemon=True)
    t.start()
    spin = st.status(label, expanded=True)
    while not holder.get("done"):
        bar.progress(min(0.99, len(lines) / 50))
        spin.write("\n".join(lines[-24:]) if lines else "starting…")
        time.sleep(0.35)
    bar.progress(1.0)
    spin.write("\n".join(lines[-40:]))
    with spin:
        if holder["ok"]:
            st.success(f"{label} — completed in {len(lines)} steps")
        else:
            st.error(f"{label} failed: {holder['error']}")
    return holder


def banner(ok: bool, msg: str) -> None:
    if ok:
        st.success(msg)
    else:
        st.error(msg)