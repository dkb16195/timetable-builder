"""Wizard steps: Check (7) and Build timetable (8)."""
from __future__ import annotations

import time

import streamlit as st

from . import runner, state, widgets


def step_check():
    cfg = state.school()
    st.header("Step 7 — Does everything add up?")
    st.caption("Before any heavy computing, the tool checks the "
               "arithmetic: do the lessons fit the week, can the "
               "teachers cover the classes, are there enough rooms? "
               "Every problem comes with where to fix it.")
    if st.button("Run the check", type="primary", key="btn_runcheck"):
        with st.spinner("Checking your school…"):
            try:
                st.session_state.check_result = runner.run_check(cfg)
            except Exception as e:   # noqa: BLE001 — never crash on a user
                st.session_state.check_result = None
                st.error("Something unexpected went wrong during the "
                         "check. Your school file is safe — download it "
                         "from the sidebar. Technical detail for support: "
                         f"{type(e).__name__}: {e}")
    cr = st.session_state.get("check_result")
    if cr is None:
        widgets.nav(next_label="Skip ahead ➜",
                    on_next=lambda: ["Run the check first — the button "
                                     "above."])
        return
    if cr.ok:
        st.success("✅ " + cr.summary)
        warns = [f for f in cr.findings if f.severity == "warn"]
        if warns:
            with st.expander(f"⚠️ {len(warns)} thing(s) worth knowing "
                             "(not blockers)"):
                for f in warns:
                    st.write("• " + f.message)
        widgets.nav(next_label="Build my timetable ➜",
                    on_next=lambda: True)
    else:
        st.error(cr.summary)
        for f in cr.findings:
            if f.severity != "block":
                continue
            with st.container(border=True):
                st.markdown(f"🔴 {f.message}")
                cols = st.columns([4, 1])
                if f.suggestion:
                    cols[0].caption("💡 " + f.suggestion)
                if cols[1].button(f"Fix in step {f.step}",
                                  key=f"fix_{hash(f.message)}"):
                    state.goto(f.step)
        widgets.nav(next_label="Re-check",
                    on_next=lambda: ["Use the 'Run the check' button "
                                     "after making changes."])


def step_solve():
    cfg = state.school()
    st.header("Step 8 — Build the timetable")
    cr = st.session_state.get("check_result")
    if not (cr and cr.ok):
        st.warning("Run Step 7's check first — building only starts from "
                   "a school that adds up.")
        if st.button("Go to the check"):
            state.goto(7)
        return

    effort = st.radio(
        "How long may it work on quality?",
        ["Quick draft (1 min)", "Standard (3 min)", "Thorough (7 min)"],
        index=1, horizontal=True,
        help="A valid, clash-free timetable usually appears quickly; "
             "extra time only improves quality (fewer teacher gaps, "
             "better spread). It never changes correctness.")
    time_limit = {"Quick draft (1 min)": 60, "Standard (3 min)": 180,
                  "Thorough (7 min)": 420}[effort]
    warm = st.session_state.get("solution")
    if warm:
        st.caption("↻ A previous timetable exists — the tool will start "
                   "from it and change as little as possible.")

    if st.button("🏗️ Build my timetable", type="primary",
                 key="btn_build", use_container_width=True):
        status = st.status("Building…", expanded=True)
        t0 = time.time()

        def progress(msg):
            status.write(f"• {msg}")

        try:
            outcome = runner.run_solve(cfg, time_limit, warm, progress)
        except Exception as e:   # noqa: BLE001
            status.update(label="Something unexpected went wrong",
                          state="error")
            st.error("The builder hit an unexpected error. Your school "
                     "file is safe — download it from the sidebar and "
                     "send it to support. Technical detail: "
                     f"{type(e).__name__}: {e}")
            return
        status.update(label=f"Finished in {time.time() - t0:.0f}s",
                      state="complete", expanded=False)
        st.session_state.results = outcome
        if outcome.schedule:
            st.session_state.solution = outcome.schedule

    outcome = st.session_state.get("results")
    if not outcome:
        return
    if outcome.status == "SOLVED":
        st.success("🎉 " + outcome.headline)
        st.caption(outcome.detail)
        st.download_button(
            "⬇️ Download everything (one zip)",
            data=outcome.zip_bytes, file_name="timetable_bundle.zip",
            type="primary", use_container_width=True,
            help="Master timetable, every teacher's timetable, year "
                 "group and department views, room usage, staff loading, "
                 "the quality scorecard and the independent verification "
                 "report — Excel and PDF.")
        with st.expander("📊 Quality scorecard", expanded=True):
            st.markdown(outcome.scorecard_md or "(scorecard unavailable)")
        with st.expander("🛡️ Independent verification report"):
            st.text(outcome.validation_text[:6000])
        st.markdown("**Want to try a what-if?** Change anything in the "
                    "earlier steps (one more teacher, a different room, "
                    "fewer lessons) and build again — the tool starts "
                    "from this timetable and changes as little as "
                    "possible.")
    elif outcome.status == "INFEASIBLE":
        st.error(outcome.headline)
        st.caption(outcome.detail)
        st.markdown("**These requirements collide:**")
        for c in outcome.causes[:12]:
            st.markdown("• " + c)
        st.markdown(
            "**The usual ways out** (pick the least painful):\n"
            "1. *More capacity* — another room of the named kind, or a "
            "higher maximum load for a named teacher (Steps 2 & 4).\n"
            "2. *Less demand* — one fewer lesson of a named subject, or "
            "allow it twice a day (Steps 5 & 3).\n"
            "3. *More flexibility* — remove a day-off restriction or "
            "split a class differently (Step 4 & 5).\n\n"
            "Change one thing, run Step 7's check, and build again.")
    else:
        st.error(outcome.headline)
        with st.expander("Details"):
            st.text(outcome.detail)
