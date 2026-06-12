"""Shared widgets: bulk paste, nav buttons, friendly bits."""
from __future__ import annotations

import csv
import io

import streamlit as st

from . import state


def nav(back: bool = True, next_label: str = "Save & continue ➜",
        on_next=None):
    """Bottom navigation. on_next() returns True to advance (or a list of
    error strings to show)."""
    st.divider()
    cols = st.columns([1, 3, 2])
    if back and cols[0].button("⬅ Back"):
        state.goto(st.session_state.step - 1)
    if cols[2].button(next_label, type="primary",
                      use_container_width=True):
        result = on_next() if on_next else True
        if result is True:
            st.session_state.visited.add(st.session_state.step)
            state.goto(st.session_state.step + 1)
        else:
            for msg in (result or []):
                st.error(msg)


def paste_table(label: str, columns: list[str], help_text: str):
    """Expander that parses pasted spreadsheet rows (TSV or CSV).
    Returns list of dicts or None."""
    with st.expander(f"📋 Paste {label} from a spreadsheet instead"):
        st.caption(help_text + " Copy the cells in your spreadsheet "
                   "(no header row needed) and paste below, then press "
                   "the button.")
        raw = st.text_area(f"Paste {label} here", height=140,
                           key=f"paste_{label}",
                           placeholder="\t".join(columns))
        if st.button(f"Add these {label}", key=f"pastebtn_{label}"):
            rows = []
            sniff_delim = "\t" if "\t" in raw else ","
            for line in csv.reader(io.StringIO(raw.strip()),
                                   delimiter=sniff_delim):
                cells = [c.strip() for c in line]
                if not any(cells):
                    continue
                rows.append({col: (cells[i] if i < len(cells) else "")
                             for i, col in enumerate(columns)})
            if not rows:
                st.warning("Nothing recognisable was pasted.")
                return None
            return rows
    return None


def progress_sidebar():
    ss = st.session_state
    st.sidebar.title("🗓️ Timetable Builder")
    if ss.school:
        name = ss.school["school"].get("name") or "Unnamed school"
        st.sidebar.caption(f"Working on: **{name}**")
        st.sidebar.download_button(
            "💾 Download my school file",
            data=state.to_yaml(ss.school),
            file_name="my_school.yaml",
            help="Your whole school in one file. Keep it safe — you can "
                 "load it again any time, on any computer.",
            use_container_width=True)
    st.sidebar.divider()
    for i, label in enumerate(state.STEPS):
        if i == 0 and ss.school:
            continue
        marker = "✅ " if i in ss.visited else ""
        disabled = ss.school is None and i > 0
        if st.sidebar.button(f"{marker}{label}", key=f"nav{i}",
                             disabled=disabled,
                             use_container_width=True,
                             type="secondary" if i != ss.step else "primary"):
            state.goto(i)
    st.sidebar.divider()
    if ss.school and st.sidebar.button("🗑 Start over"):
        for k in ("school", "solution", "results", "visited"):
            ss.pop(k, None)
        ss.step = 0
        st.rerun()
