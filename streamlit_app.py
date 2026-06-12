"""Timetable Builder — guided web app.

A non-technical colleague opens this, answers questions step by step,
and downloads a complete, independently verified, clash-free timetable.
"""
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(__file__))   # find ttbuilder + tt_ui

from tt_ui import state, widgets                      # noqa: E402
from tt_ui.steps_basics import (step_rooms, step_school,   # noqa: E402
                                step_subjects, step_welcome)
from tt_ui.steps_final import step_check, step_solve  # noqa: E402
from tt_ui.steps_people import (step_blocks, step_teachers,  # noqa: E402
                                step_years)

st.set_page_config(page_title="Timetable Builder", page_icon="🗓️",
                   layout="wide")

state.init_state()
widgets.progress_sidebar()

STEP_PAGES = [step_welcome, step_school, step_rooms, step_subjects,
              step_teachers, step_years, step_blocks, step_check,
              step_solve]

step = st.session_state.step
if st.session_state.school is None and step != 0:
    st.session_state.step = 0
    step = 0

try:
    STEP_PAGES[step]()
except Exception as e:   # noqa: BLE001 — last-resort guard: never a stack trace
    st.error(
        "Something unexpected went wrong on this page. Your school is "
        "not lost — use **Download my school file** in the sidebar, then "
        "reload the page and load the file back in. Technical detail "
        f"for support: {type(e).__name__}: {e}")
