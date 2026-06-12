"""Wizard steps: Welcome, School & week, Rooms, Subjects."""
from __future__ import annotations

import os

import streamlit as st

from . import state, widgets

DEMO_PATH = os.path.join(os.path.dirname(__file__), "..", "demo_school.yaml")


def step_welcome():
    st.title("Build your school's timetable")
    st.markdown(
        "This tool builds a **complete, clash-free timetable** from a "
        "description of your school. You answer questions step by step — "
        "rooms, subjects, teachers, year groups — and the tool does the "
        "rest. It checks its own work with an independent verifier and "
        "explains any problem in plain English.\n\n"
        "*No timetabling experience needed. You can stop at any time and "
        "download your progress as a file.*")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.subheader("🚀 Start fresh")
        st.caption("Answer the questions step by step. Allow an hour or "
                   "two for a whole school — less with lists you can "
                   "paste from a spreadsheet.")
        if st.button("Start a new school", type="primary", key="btn_start",
                     use_container_width=True):
            st.session_state.school = state.fresh_school()
            state.goto(1)
    with c2:
        st.subheader("📂 I have a school file")
        st.caption("Continue from a file saved earlier, or one a "
                   "colleague sent you (a .yaml school file).")
        up = st.file_uploader("Load a school file", type=["yaml", "yml"],
                              label_visibility="collapsed")
        if up is not None:
            cfg, err = state.from_yaml(up.getvalue().decode("utf-8",
                                                            "replace"))
            if err:
                st.error(err)
            else:
                st.session_state.school = cfg
                st.success(f"Loaded **{cfg['school'].get('name') or 'your school'}** — "
                           "taking you to the check step.")
                st.session_state.visited.update(range(1, 7))
                if st.button("Continue ➜", type="primary"):
                    state.goto(7)
    with c3:
        st.subheader("🎓 Try the demo")
        st.caption("Explore with “Greenfield Secondary”, a fictional "
                   "school that shows every feature, and build its "
                   "timetable in about a minute.")
        if st.button("Load the demo school", key="btn_demo",
                     use_container_width=True):
            cfg, err = state.from_yaml(open(DEMO_PATH).read())
            st.session_state.school = cfg
            st.session_state.visited.update(range(1, 7))
            state.goto(7)
    with st.expander("ℹ️ Getting data out of iSAMS, SIMS or another school system"):
        st.markdown(
            "You don't need any special export. Every step that needs a "
            "list (rooms, subjects, teachers) has a **“paste from a "
            "spreadsheet”** box — open your school system's staff or room "
            "list, copy the columns, and paste. iSAMS users: *School "
            "Manager → Teaching Staff* and *Estates → Rooms* exports work "
            "directly. The same applies to SIMS, Arbor, Engage and plain "
            "Excel lists.")


def step_school():
    cfg = state.school()
    st.header("Step 1 — Your school and its week")
    st.caption("The basic shape of your timetable: which days, how many "
               "lessons a day, and whether you run a one-week or "
               "two-week cycle.")
    s = cfg["school"]
    c1, c2, c3 = st.columns([3, 2, 2])
    s["name"] = c1.text_input("School name", s.get("name", ""),
                              placeholder="e.g. Greenfield Secondary")
    s["academic_year"] = c2.text_input("Academic year",
                                       s.get("academic_year", ""),
                                       placeholder="e.g. 2026-27")
    s["class_size_max"] = c3.number_input(
        "Biggest allowed class", 10, 60, int(s.get("class_size_max", 26)),
        help="Used to work out how many classes each subject needs. You "
             "can override it per subject later.")

    grid = cfg["grid"]
    st.subheader("The weekly cycle")
    two = st.radio("Does your timetable repeat every week, or every two weeks?",
                   ["Every week", "Every two weeks (Week A / Week B)"],
                   index=0 if len(grid["weeks"]) == 1 else 1,
                   horizontal=True)
    grid["weeks"] = ["Wk"] if two == "Every week" else ["A", "B"]

    grid["days"] = st.multiselect(
        "School days", ["Monday", "Tuesday", "Wednesday", "Thursday",
                        "Friday", "Saturday", "Sunday"],
        default=grid["days"])

    st.subheader("The shape of a day")
    teach_ids = [p["id"] for p in grid["periods"]
                 if p.get("kind") == "teaching"]
    n_teach = st.slider("Teaching lessons per day", 3, 10, len(teach_ids))
    has_reg = st.checkbox("Morning registration / homeroom",
                          value=any(p.get("kind") == "registration"
                                    for p in grid["periods"]))
    breaks_after = st.multiselect(
        "Breaks come after which lessons?",
        [f"P{i}" for i in range(1, n_teach)],
        default=[p for p in
                 _current_breaks_after(grid) if p in
                 [f"P{i}" for i in range(1, n_teach)]],
        help="e.g. morning break after P3 and lunch after P5.")
    periods = []
    if has_reg:
        periods.append({"id": "HR", "kind": "registration"})
    for i in range(1, n_teach + 1):
        periods.append({"id": f"P{i}", "kind": "teaching"})
        if f"P{i}" in breaks_after:
            periods.append({"id": f"Break after P{i}", "kind": "break"})
    grid["periods"] = periods

    with st.expander("⏰ Short days (e.g. an early-finish Friday)"):
        st.caption("Tick the lessons that DO happen on a short day; "
                   "leave a day untouched if it is normal.")
        overrides = grid.get("day_overrides", {}) or {}
        new_overrides = {}
        for day in grid["days"]:
            ids_all = [p["id"] for p in periods]
            current = overrides.get(day)
            on = st.checkbox(f"{day} is a short day", value=current is not None,
                             key=f"short_{day}")
            if on:
                chosen = st.multiselect(
                    f"Lessons that happen on {day}", ids_all,
                    default=[i for i in (current or ids_all) if i in ids_all],
                    key=f"shortp_{day}")
                new_overrides[day] = chosen
        grid["day_overrides"] = new_overrides

    n_slots = state.teaching_slot_count(cfg)
    cyc = "week" if len(grid["weeks"]) == 1 else "two-week cycle"
    st.info(f"That gives **{n_slots} teaching lessons per {cyc}** — every "
            "pupil's subjects must add up to at most this number. The "
            "check step adds this up for you.")

    def validate():
        errs = []
        if not s["name"].strip():
            errs.append("Please give your school a name (top of the page).")
        if not grid["days"]:
            errs.append("Pick at least one school day.")
        return errs or True
    widgets.nav(back=False, on_next=validate)


def _current_breaks_after(grid):
    out, prev = [], None
    for p in grid["periods"]:
        if p.get("kind") == "break" and prev:
            out.append(prev)
        prev = p["id"] if p.get("kind") == "teaching" else prev
    return out


def step_rooms():
    cfg = state.school()
    st.header("Step 2 — Rooms")
    st.caption("Every teaching room, and what kind it is. The kind is how "
               "the tool knows science must be in a lab.")
    types = cfg.get("room_types") or ["classroom"]
    new_type = st.text_input("Add a room kind (optional)",
                             placeholder="e.g. swimming_pool")
    if new_type and new_type.strip() and new_type.strip() not in types:
        types.append(new_type.strip().replace(" ", "_"))
        cfg["room_types"] = types

    pasted = widgets.paste_table(
        "rooms", ["id", "type", "seats"],
        "Three columns: room name, kind, seats.")
    if pasted:
        known = set(types)
        for r in pasted:
            cfg["rooms"].append({
                "id": r["id"], "type": r["type"] if r["type"] in known
                else "classroom",
                "seats": int(r["seats"]) if str(r["seats"]).isdigit() else 30,
                "max_parallel": 1})
        st.rerun()

    rows = [{"Room": r["id"], "Kind": r.get("type", "classroom"),
             "Seats": r.get("seats", 30),
             "Classes at once": r.get("max_parallel", 1)}
            for r in cfg["rooms"]]
    edited = st.data_editor(
        rows, num_rows="dynamic", use_container_width=True,
        key="rooms_editor",
        column_config={
            "Kind": st.column_config.SelectboxColumn(options=types,
                                                     required=True),
            "Seats": st.column_config.NumberColumn(min_value=1,
                                                   max_value=500, step=1),
            "Classes at once": st.column_config.NumberColumn(
                min_value=1, max_value=10, step=1,
                help="Normally 1. A sports hall that holds three PE "
                     "classes at the same time would be 3."),
        })

    def validate():
        errs, seen, rooms = [], set(), []
        for i, row in enumerate(edited, 1):
            name = str(row.get("Room") or "").strip()
            if not name:
                continue
            if name in seen:
                errs.append(f"Row {i}: room '{name}' appears twice.")
                continue
            seen.add(name)
            rooms.append({"id": name, "type": row.get("Kind") or "classroom",
                          "seats": int(row.get("Seats") or 30),
                          "max_parallel": int(row.get("Classes at once") or 1)})
        if not rooms:
            errs.append("Add at least one room — paste a list or type "
                        "into the table.")
        if not errs:
            cfg["rooms"] = rooms
        return errs or True
    widgets.nav(on_next=validate)


def step_subjects():
    cfg = state.school()
    st.header("Step 3 — Subjects")
    st.caption("Each subject, its department, and the kind of room it "
               "needs. “Twice a day?” controls whether a class may have "
               "the same subject twice in one day.")
    types = cfg.get("room_types") or ["classroom"]
    pasted = widgets.paste_table(
        "subjects", ["id", "department", "room"],
        "Three columns: subject, department, room kind.")
    if pasted:
        for r in pasted:
            cfg["subjects"].append({
                "id": r["id"], "department": r["department"] or r["id"],
                "room_types": [r["room"] if r["room"] in types
                               else "classroom"]})
        st.rerun()

    rows = [{"Subject": sj["id"],
             "Department": sj.get("department", ""),
             "Room kind": (sj.get("room_types") or ["classroom"])[0],
             "Twice a day?": {"forbid": "Never", "allow": "Allowed",
                              "prefer": "As a double", "require":
                              "Always a double"}.get(
                                  sj.get("doubles", "allow"), "Allowed")
             if sj.get("max_per_day", 1) > 1 or sj.get("doubles") else "Never"}
            for sj in cfg["subjects"]]
    edited = st.data_editor(
        rows, num_rows="dynamic", use_container_width=True,
        key="subjects_editor",
        column_config={
            "Room kind": st.column_config.SelectboxColumn(options=types,
                                                          required=True),
            "Twice a day?": st.column_config.SelectboxColumn(
                options=["Never", "Allowed", "As a double",
                         "Always a double"], required=True,
                help="“As a double” / “Always a double” mean two lessons "
                     "back-to-back (science practicals, DT)."),
        })

    def validate():
        errs, seen, subjects = [], set(), []
        for i, row in enumerate(edited, 1):
            name = str(row.get("Subject") or "").strip()
            if not name:
                continue
            if name in seen:
                errs.append(f"Row {i}: subject '{name}' appears twice.")
                continue
            seen.add(name)
            twice = row.get("Twice a day?") or "Never"
            doubles, mpd = {"Never": ("allow", 1),
                            "Allowed": ("allow", 2),
                            "As a double": ("prefer", 2),
                            "Always a double": ("require", 2)}[twice]
            subjects.append({
                "id": name,
                "department": str(row.get("Department") or name).strip(),
                "room_types": [row.get("Room kind") or "classroom"],
                "doubles": doubles, "max_per_day": mpd})
        if not subjects:
            errs.append("Add at least one subject.")
        if not errs:
            cfg["subjects"] = subjects
        return errs or True
    widgets.nav(on_next=validate)
