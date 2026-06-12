"""Wizard steps: Teachers, Year groups & curriculum, Option blocks."""
from __future__ import annotations

import copy

import streamlit as st

from . import state, widgets

TIER_LABELS = {
    "classroom_teacher": "Classroom teacher",
    "middle_leader": "Middle leader (HoD / coordinator)",
    "assistant_head": "Assistant head",
    "slt": "Senior leadership (no teaching)",
}
LABEL_TO_TIER = {v: k for k, v in TIER_LABELS.items()}


def step_teachers():
    cfg = state.school()
    st.header("Step 4 — Teachers")
    st.caption("Who teaches what, their role (which sets their maximum "
               "weekly load), and any days they are not in school.")
    tiers = cfg["staffing"]["tiers"]
    with st.expander("Maximum teaching loads per week (tap to adjust)"):
        for tier, label in TIER_LABELS.items():
            tiers[tier] = st.number_input(label, 0, 40,
                                          int(tiers.get(tier, 25)),
                                          key=f"tier_{tier}")

    subj_names = [s["id"] for s in cfg["subjects"]]
    if not subj_names:
        st.warning("Add your subjects in Step 3 first — teachers need "
                   "subjects to teach.")
    days = cfg["grid"]["days"]

    pasted = widgets.paste_table(
        "teachers", ["name", "subjects", "days_off"],
        "Columns: name, subjects (separated by ; or ,), days off "
        "(e.g. Friday).")
    if pasted:
        for r in pasted:
            cfg["staffing"]["teachers"].append({
                "id": _make_id(r["name"], cfg),
                "name": r["name"],
                "subjects": _match_subjects(r["subjects"], subj_names),
                "tier": "classroom_teacher",
                "unavailable": [],
                "_days_off": r.get("days_off", ""),
            })
        st.rerun()

    rows = []
    for t in cfg["staffing"]["teachers"]:
        rows.append({
            "Name": t.get("name", ""),
            "Subjects (separate with ;)": "; ".join(t.get("subjects", [])),
            "Role": TIER_LABELS.get(t.get("tier", "classroom_teacher"),
                                    "Classroom teacher"),
            "Days off": t.get("_days_off",
                              _days_off_from_unavailable(t, cfg)),
        })
    edited = st.data_editor(
        rows, num_rows="dynamic", use_container_width=True,
        key="teachers_editor",
        column_config={
            "Role": st.column_config.SelectboxColumn(
                options=list(TIER_LABELS.values()), required=True),
            "Days off": st.column_config.TextColumn(
                help="For part-time staff: whole days they are never in, "
                     "e.g. 'Friday' or 'Thursday; Friday'."),
        })

    def validate():
        errs, teachers, seen = [], [], set()
        for i, row in enumerate(edited, 1):
            name = str(row.get("Name") or "").strip()
            if not name:
                continue
            if name.lower() in seen:
                errs.append(f"Row {i}: '{name}' appears twice.")
                continue
            seen.add(name.lower())
            subjects = _match_subjects(
                row.get("Subjects (separate with ;)") or "", subj_names)
            bad = [s for s in _split(row.get("Subjects (separate with ;)"))
                   if _match_one(s, subj_names) is None]
            if bad:
                errs.append(
                    f"Row {i} ({name}): I don't recognise the subject(s) "
                    f"{', '.join(bad)} — they must match Step 3 exactly "
                    f"(known: {', '.join(subj_names[:12])}…).")
            day_off_raw = _split(row.get("Days off"))
            bad_days = [d for d in day_off_raw
                        if d.title() not in days]
            if bad_days:
                errs.append(f"Row {i} ({name}): '{', '.join(bad_days)}' "
                            f"isn't one of your school days ({', '.join(days)}).")
            unavailable = []
            teach_ids = [p["id"] for p in cfg["grid"]["periods"]
                         if p.get("kind") == "teaching"]
            for d in day_off_raw:
                d = d.title()
                pids = cfg["grid"].get("day_overrides", {}).get(d, teach_ids)
                for wk in cfg["grid"]["weeks"]:
                    unavailable += [f"{wk}.{d}.{p}" for p in pids
                                    if p in teach_ids]
            teachers.append({
                "id": _make_id(name, cfg, fresh=False),
                "name": name, "subjects": subjects,
                "tier": LABEL_TO_TIER[row.get("Role") or
                                      "Classroom teacher"],
                "unavailable": unavailable,
                "_days_off": "; ".join(day_off_raw),
            })
        if not teachers:
            errs.append("Add at least one teacher.")
        if not errs:
            cfg["staffing"]["teachers"] = teachers
        return errs or True
    widgets.nav(on_next=validate)


def _split(text):
    return [x.strip() for x in str(text or "").replace(",", ";").split(";")
            if x.strip()]


def _match_one(name, known):
    for k in known:
        if k.lower() == name.lower():
            return k
    for k in known:
        if k.lower().startswith(name.lower()) and len(name) >= 3:
            return k
    return None


def _match_subjects(text, known):
    out = []
    for part in _split(text):
        m = _match_one(part, known)
        if m and m not in out:
            out.append(m)
    return out


def _make_id(name, cfg, fresh=True):
    base = "".join(w[:4].upper() for w in name.split()[:2]) or "T"
    existing = {t["id"] for t in cfg["staffing"]["teachers"]} if fresh else set()
    tid, n = base, 1
    while tid in existing:
        n += 1
        tid = f"{base}{n}"
    return tid


def _days_off_from_unavailable(t, cfg):
    days = set()
    for ref in t.get("unavailable", []):
        parts = ref.split(".")
        if len(parts) == 3:
            days.add(parts[1])
    teach_ids = [p["id"] for p in cfg["grid"]["periods"]
                 if p.get("kind") == "teaching"]
    full = []
    for d in sorted(days):
        pids = cfg["grid"].get("day_overrides", {}).get(d, teach_ids)
        refs = {f"{wk}.{d}.{p}" for wk in cfg["grid"]["weeks"]
                for p in pids if p in teach_ids}
        if refs <= set(t.get("unavailable", [])):
            full.append(d)
    return "; ".join(full)


# --------------------------------------------------------------------------
def step_years():
    cfg = state.school()
    st.header("Step 5 — Year groups and their subjects")
    st.caption("For each year group: how many pupils, and how many "
               "lessons of each subject they get. The tool works out how "
               "many classes that needs and staffs them automatically.")
    n_slots = state.teaching_slot_count(cfg)
    cyc = "per week" if len(cfg["grid"]["weeks"]) == 1 else "per two-week cycle"
    subj_names = [s["id"] for s in cfg["subjects"]]

    new_name = st.text_input("Add a year group",
                             placeholder="e.g. Year 7 or G7")
    if st.button("Add year group") and new_name.strip():
        if any(y["id"] == new_name.strip() for y in cfg["year_groups"]):
            st.warning("That year group already exists.")
        else:
            cfg["year_groups"].append({"id": new_name.strip(),
                                       "students": 100,
                                       "populations": [],
                                       "curriculum": []})
            st.rerun()

    for yg in list(cfg["year_groups"]):
        with st.expander(f"**{yg['id']}** — {yg.get('students', 0)} pupils, "
                         f"{len(yg.get('curriculum', []))} subject lines",
                         expanded=len(cfg["year_groups"]) <= 2):
            c1, c2 = st.columns([1, 2])
            yg["students"] = c1.number_input(
                "Pupils", 1, 1000, int(yg.get("students", 100)),
                key=f"stu_{yg['id']}")
            if c2.button(f"Remove {yg['id']}", key=f"del_{yg['id']}"):
                cfg["year_groups"].remove(yg)
                st.rerun()

            with st.popover("Split into bands (optional, for bigger years)"):
                st.caption(
                    "A *band* is half (or a third) of the year that moves "
                    "together. Parallel classes inside a band run at the "
                    "same time, so a band of 60 with 30-pupil classes "
                    "needs only 2 teachers per subject at once instead "
                    "of 4. Most schools over ~120 pupils per year use "
                    "bands.")
                n_bands = st.number_input(
                    "Number of bands (1 = no bands)", 1, 4,
                    max(1, len(yg.get("populations", []) or [])),
                    key=f"bands_{yg['id']}")
                if n_bands > 1:
                    per = yg["students"] // n_bands
                    yg["populations"] = [
                        {"id": f"{yg['id']}.{chr(88 + i)}",
                         "size": per + (yg["students"] % n_bands
                                        if i == 0 else 0)}
                        for i in range(int(n_bands))]
                    st.write("Bands: " + ", ".join(
                        f"{p['id']} ({p['size']})"
                        for p in yg["populations"]))
                else:
                    yg["populations"] = []

            band_opts = ["whole year"] + [p["id"]
                                          for p in yg.get("populations", [])]
            rows = []
            for r in yg.get("curriculum", []):
                rows.append({
                    "Subject": r.get("subject", ""),
                    f"Lessons {cyc}": r.get("periods_per_cycle", 2),
                    "Who": r.get("population") or "whole year",
                    "Classes (blank = auto)": r.get("classes") or None,
                    "Pupils taking it (blank = all)":
                        r.get("participants") or None,
                })
            edited = st.data_editor(
                rows, num_rows="dynamic", use_container_width=True,
                key=f"cur_{yg['id']}",
                column_config={
                    "Subject": st.column_config.SelectboxColumn(
                        options=subj_names, required=True),
                    f"Lessons {cyc}": st.column_config.NumberColumn(
                        min_value=1, max_value=20, step=1, required=True),
                    "Who": st.column_config.SelectboxColumn(
                        options=band_opts, required=True),
                    "Classes (blank = auto)": st.column_config.NumberColumn(
                        min_value=1, max_value=20, step=1),
                    "Pupils taking it (blank = all)":
                        st.column_config.NumberColumn(min_value=1,
                                                      max_value=1000,
                                                      step=1),
                })
            cur = []
            for row in edited:
                if not row.get("Subject"):
                    continue
                item = {"subject": row["Subject"],
                        "periods_per_cycle": int(row.get(f"Lessons {cyc}")
                                                 or 1)}
                if row.get("Who") and row["Who"] != "whole year":
                    item["population"] = row["Who"]
                if row.get("Classes (blank = auto)"):
                    item["classes"] = int(row["Classes (blank = auto)"])
                if row.get("Pupils taking it (blank = all)"):
                    item["participants"] = int(
                        row["Pupils taking it (blank = all)"])
                    item["simultaneous"] = False
                cur.append(item)
            yg["curriculum"] = cur

            # live per-pupil total — the #1 mistake, caught instantly
            for scope in band_opts:
                total = sum(r["periods_per_cycle"] for r in cur
                            if (r.get("population") or "whole year") in
                            ("whole year", scope) and "participants" not in r)
                if scope == "whole year" and len(band_opts) > 1:
                    continue
                label = yg["id"] if scope == "whole year" else scope
                if total > n_slots:
                    st.error(f"**{label}** pupils would have {total} "
                             f"lessons but the {cyc.replace('per ', '')} "
                             f"only has {n_slots} — remove "
                             f"{total - n_slots}.")
                elif total and total < n_slots:
                    st.caption(f"{label}: {total}/{n_slots} lessons filled "
                               f"({n_slots - total} free periods per pupil"
                               " — fine if intended, e.g. for options "
                               "added in Step 6).")
                elif total:
                    st.caption(f"{label}: {total}/{n_slots} — a full "
                               "timetable. ✅")

    def validate():
        if not cfg["year_groups"]:
            return ["Add at least one year group."]
        for yg in cfg["year_groups"]:
            if not yg.get("curriculum"):
                return [f"{yg['id']} has no subjects yet — add at least "
                        "one row to its table."]
        return True
    widgets.nav(on_next=validate)


# --------------------------------------------------------------------------
def step_blocks():
    cfg = state.school()
    st.header("Step 6 — Option blocks (if you have them)")
    st.caption(
        "An option block (or “column”) is a set of different subjects "
        "taught **at the same time** — each pupil picks one. If your "
        "school has no options, just continue.")

    gen = _generated_optional_sections(cfg)
    if not gen:
        st.info("No optional subjects found. To create options, go back "
                "to Step 5 and give a subject a number in **“Pupils "
                "taking it”** (fewer than the whole year). Each such "
                "subject becomes a class you can place into a block here.")
    else:
        st.write("Optional classes available "
                 "(from Step 5's “pupils taking it” rows):")
        st.code("  ".join(sorted(gen)), language=None)
        blocks = cfg.get("blocks", [])
        bname = st.text_input("New block name", placeholder="e.g. Year 9 Block A")
        bsecs = st.multiselect("Classes in this block (taught simultaneously)",
                               sorted(gen))
        bper = st.number_input("Lessons per cycle for this block", 1, 20, 3)
        if st.button("Add block") and bname.strip() and len(bsecs) >= 2:
            blocks.append({"id": bname.strip(), "sections": bsecs,
                           "periods_per_cycle": int(bper)})
            cfg["blocks"] = blocks
            st.rerun()
        for b in list(blocks):
            c1, c2 = st.columns([5, 1])
            c1.write(f"**{b['id']}** — {', '.join(b['sections'])} "
                     f"({b.get('periods_per_cycle', '?')} lessons)")
            if c2.button("remove", key=f"delb_{b['id']}"):
                blocks.remove(b)
                st.rerun()

    widgets.nav(on_next=lambda: True, next_label="Continue to the check ➜")


def _generated_optional_sections(cfg):
    """Predict the section ids the engine will generate for optional
    curriculum rows (participants set, simultaneous false)."""
    out = []
    counters = {}
    for yg in cfg.get("year_groups", []):
        for row in yg.get("curriculum", []):
            pop = row.get("population") or yg["id"]
            key = (pop, row["subject"])
            n = row.get("classes") or 1 if row.get("participants") else None
            if n is None:
                # non-optional rows still consume the counter
                import math
                participants = row.get("participants") or next(
                    (p["size"] for p in yg.get("populations", [])
                     if p["id"] == pop), yg.get("students", 0))
                size_max = row.get("size_max") or cfg["school"].get(
                    "class_size_max", 26)
                n = row.get("classes") or max(
                    1, math.ceil(participants / size_max))
                for _ in range(n):
                    counters[key] = counters.get(key, 0) + 1
                continue
            for _ in range(int(n)):
                counters[key] = counters.get(key, 0) + 1
                if row.get("participants"):
                    out.append(f"{pop}/{row['subject']}{counters[key]}")
    return out
