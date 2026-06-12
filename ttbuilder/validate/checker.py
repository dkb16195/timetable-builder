"""Independent validation suite for timetable solutions.

This module is the adversarial second opinion on the solver's output. It is
deliberately implementation-independent: it imports ONLY the standard library,
pandas and yaml — never ttbuilder.model, ttbuilder.solve or ttbuilder.config.
Everything it needs (the grid, blocks, tiers, room metadata, conflict pairs,
attendance exceptions, pinned commitments) is re-derived from the raw config
YAML, and the timetable itself is read from the canonical assignments.csv
artifact. Sharing code with the solver would defeat the purpose.

Usage:
    python -m ttbuilder.validate.checker config.yaml \
        outputs/assignments.csv [enrolments.csv]
"""

from __future__ import annotations

import sys
from collections import defaultdict

import pandas as pd
import yaml

# Order in which checks are run and reported.
CHECK_NAMES = [
    "teacher_clashes",
    "student_clashes",
    "room_clashes",
    "room_suitability",
    "period_counts",
    "block_integrity",
    "teacher_loads",
    "grid_validity",
    "max_per_day",
    "doubles",
    "conflicts",
]


class ValidationReport:
    """Result of a full validation run."""

    def __init__(self):
        self.failures: dict[str, list[str]] = {name: [] for name in CHECK_NAMES}
        self.warnings: dict[str, list[str]] = {name: [] for name in CHECK_NAMES}
        self.stats: dict[str, int] = {}

    @property
    def passed(self) -> bool:
        return not any(self.failures.values())

    def render(self) -> str:
        lines = []
        verdict = "PASS" if self.passed else "FAIL"
        n_fail = sum(len(v) for v in self.failures.values())
        n_warn = sum(len(v) for v in self.warnings.values())
        lines.append("=" * 70)
        lines.append(f"TIMETABLE VALIDATION: {verdict}   "
                     f"({n_fail} failure(s), {n_warn} warning(s))")
        lines.append("=" * 70)
        if self.stats:
            lines.append("Stats: " + ", ".join(f"{k}={v}" for k, v in self.stats.items()))
        lines.append("")
        for name in CHECK_NAMES:
            fails = self.failures.get(name, [])
            warns = self.warnings.get(name, [])
            status = "FAIL" if fails else "ok"
            lines.append(f"[{status:>4}] {name}: "
                         f"{len(fails)} failure(s), {len(warns)} warning(s)")
            for msg in fails[:10]:
                lines.append(f"         FAIL: {msg}")
            if len(fails) > 10:
                lines.append(f"         ... and {len(fails) - 10} more failures")
            for msg in warns[:10]:
                lines.append(f"         warn: {msg}")
            if len(warns) > 10:
                lines.append(f"         ... and {len(warns) - 10} more warnings")
        lines.append("")
        lines.append(f"Overall: {verdict}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Helpers that re-derive structure from the raw config dict.
# --------------------------------------------------------------------------

def _split_multi(value: str) -> list[str]:
    """Split a |-joined cell into its non-empty parts."""
    if not value:
        return []
    return [p for p in str(value).split("|") if p]


def _teaching_slots(grid: dict) -> set[str]:
    """All valid teaching slot refs 'week.day.period' from the grid."""
    period_kind = {p["id"]: p.get("kind", "teaching") for p in grid["periods"]}
    all_period_ids = [p["id"] for p in grid["periods"]]
    overrides = grid.get("day_overrides") or {}
    slots = set()
    for week in grid["weeks"]:
        for day in grid["days"]:
            day_periods = overrides.get(day, all_period_ids)
            for pid in day_periods:
                if period_kind.get(pid) == "teaching":
                    slots.add(f"{week}.{day}.{pid}")
    return slots


def _adjacent_period_pairs(grid: dict) -> dict[str, set[frozenset]]:
    """Per day: the set of unordered pairs of ADJACENT teaching periods.

    Adjacent means consecutive in that day's period list with no break or
    registration period between them.
    """
    period_kind = {p["id"]: p.get("kind", "teaching") for p in grid["periods"]}
    all_period_ids = [p["id"] for p in grid["periods"]]
    overrides = grid.get("day_overrides") or {}
    out: dict[str, set[frozenset]] = {}
    for day in grid["days"]:
        day_periods = overrides.get(day, all_period_ids)
        pairs = set()
        for i in range(len(day_periods) - 1):
            a, b = day_periods[i], day_periods[i + 1]
            if period_kind.get(a) == "teaching" and period_kind.get(b) == "teaching":
                pairs.add(frozenset((a, b)))
        out[day] = pairs
    return out


def _section_to_block(blocks: list[dict]) -> dict[str, str]:
    mapping = {}
    for block in blocks:
        for sec_id in block.get("sections", []):
            mapping[sec_id] = block["id"]
    return mapping


def _teacher_limit(teacher: dict, tiers: dict, overrides: dict) -> int | None:
    """Max teaching periods per week for one teacher (None = unknown tier)."""
    if teacher.get("max_per_week") is not None:
        return teacher["max_per_week"]
    # schema default: a teacher with no explicit tier is a classroom_teacher
    tier = overrides.get(teacher["id"], overrides.get(
        teacher.get("name"), teacher.get("tier") or "classroom_teacher"))
    return tiers.get(tier)


# --------------------------------------------------------------------------
# Main entry point.
# --------------------------------------------------------------------------

def validate(config_path, assignments_path, enrolments_path=None) -> ValidationReport:
    with open(config_path) as fh:
        cfg = yaml.safe_load(fh)

    asg = pd.read_csv(assignments_path, dtype=str).fillna("")
    enr = None
    if enrolments_path is not None:
        enr = pd.read_csv(enrolments_path, dtype=str).fillna("")

    report = ValidationReport()
    fail = report.failures
    warn = report.warnings

    grid = cfg["grid"]
    weeks = list(grid["weeks"])
    rooms_cfg = {r["id"]: r for r in cfg.get("rooms", [])}
    subjects_cfg = {s["id"]: s for s in cfg.get("subjects", [])}
    sections_cfg = {s["id"]: s for s in cfg.get("sections", [])}
    blocks_cfg = {b["id"]: b for b in cfg.get("blocks", [])}
    sec2block = _section_to_block(cfg.get("blocks", []))
    staffing = cfg.get("staffing", {})
    tiers = staffing.get("tiers", {})
    teachers_cfg = {t["id"]: t for t in staffing.get("teachers", [])}
    leadership_overrides = staffing.get("leadership_overrides") or {}

    valid_slots = _teaching_slots(grid)
    adjacency = _adjacent_period_pairs(grid)

    # Pre-index assignment rows.
    section_slots: dict[str, set[str]] = defaultdict(set)
    for sec_id, slot in zip(asg["section_id"], asg["slot_ref"]):
        section_slots[sec_id].add(slot)

    # ---- 1. teacher_clashes ------------------------------------------------
    # teacher -> slot -> set of event ids (same event = co-taught, fine).
    teacher_slot_events: dict[tuple[str, str], set[str]] = defaultdict(set)
    teacher_all_slots: dict[str, set[str]] = defaultdict(set)
    for _, row in asg.iterrows():
        for tid in _split_multi(row["teachers"]):
            teacher_slot_events[(tid, row["slot_ref"])].add(row["event_id"])
            teacher_all_slots[tid].add(row["slot_ref"])
    for (tid, slot), events in sorted(teacher_slot_events.items()):
        if len(events) > 1:
            fail["teacher_clashes"].append(
                f"teacher {tid} is double-booked at {slot} across events: "
                + ", ".join(sorted(events)))

    # ---- 2. student_clashes ------------------------------------------------
    students_checked = 0
    if enr is not None:
        exceptions = set()
        for pair in cfg.get("attendance_exceptions", []) or []:
            if len(pair) == 2:
                exceptions.add(frozenset(pair))

        # section -> list of (slot, event)
        section_meetings: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for _, row in asg.iterrows():
            section_meetings[row["section_id"]].append((row["slot_ref"], row["event_id"]))

        unknown_enrol_sections = sorted(
            set(enr["section_id"]) - set(asg["section_id"]))
        for sec_id in unknown_enrol_sections:
            warn["student_clashes"].append(
                f"enrolment refers to section '{sec_id}' that never appears in the "
                f"assignments — its students cannot be checked at those lessons")

        by_student = enr.groupby("student_id")
        students_checked = len(by_student)
        for student_id, grp in by_student:
            student_name = grp["student_name"].iloc[0]
            # slot -> event -> one representative section of the student's.
            slot_events: dict[str, dict[str, str]] = defaultdict(dict)
            for sec_id in set(grp["section_id"]):
                for slot, event in section_meetings.get(sec_id, []):
                    slot_events[slot][event] = sec_id
            for slot, events in slot_events.items():
                if len(events) < 2:
                    continue
                items = sorted(events.items())
                for i in range(len(items)):
                    for j in range(i + 1, len(items)):
                        (ev1, s1), (ev2, s2) = items[i], items[j]
                        # Exception pairs may name the section OR its block.
                        cands1 = {s1, sec2block.get(s1, s1)}
                        cands2 = {s2, sec2block.get(s2, s2)}
                        if any(frozenset((c1, c2)) in exceptions
                               for c1 in cands1 for c2 in cands2):
                            continue
                        fail["student_clashes"].append(
                            f"student {student_id} ({student_name}) is expected in two "
                            f"places at {slot}: {s1} (event {ev1}) and {s2} (event {ev2})")

    # ---- 3. room_clashes ---------------------------------------------------
    room_slot_events: dict[tuple[str, str], set[str]] = defaultdict(set)
    for _, row in asg.iterrows():
        if row["room"]:
            room_slot_events[(row["room"], row["slot_ref"])].add(row["event_id"])
    unknown_rooms_reported = set()
    for (room, slot), events in sorted(room_slot_events.items()):
        if room not in rooms_cfg:
            if room not in unknown_rooms_reported:
                unknown_rooms_reported.add(room)
                fail["room_clashes"].append(
                    f"room '{room}' is used in the assignments but does not exist "
                    f"in the config")
            continue
        max_parallel = rooms_cfg[room].get("max_parallel", 1)
        if len(events) > max_parallel:
            fail["room_clashes"].append(
                f"room '{room}' hosts {len(events)} different events at {slot} "
                f"but allows at most {max_parallel}: " + ", ".join(sorted(events)))

    # ---- 4. room_suitability -----------------------------------------------
    enrolled_count: dict[str, int] = {}
    if enr is not None:
        enrolled_count = enr.groupby("section_id")["student_id"].nunique().to_dict()

    seat_checked_sections = set()
    suitability_seen = set()  # (section, room) pairs already reported
    for _, row in asg.iterrows():
        sec_id, room, slot = row["section_id"], row["room"], row["slot_ref"]
        sec = sections_cfg.get(sec_id)
        if sec is None:
            fail["room_suitability"].append(
                f"section '{sec_id}' appears in assignments but not in the config")
            continue
        if "room_types" in sec and sec["room_types"] is not None:
            allowed = sec["room_types"]
        else:
            allowed = subjects_cfg.get(sec["subject"], {}).get("room_types", [])

        if not allowed:
            # Empty override list = section needs no room at all.
            if room:
                fail["room_suitability"].append(
                    f"section {sec_id} needs no room (empty room_types) but is "
                    f"assigned room '{room}' at {slot}")
            continue
        if not room:
            fail["room_suitability"].append(
                f"section {sec_id} (subject {sec['subject']}) has no room assigned "
                f"at {slot} but requires one of: " + ", ".join(allowed))
            continue
        if room not in rooms_cfg:
            if (sec_id, room) not in suitability_seen:
                suitability_seen.add((sec_id, room))
                fail["room_suitability"].append(
                    f"section {sec_id} is in unknown room '{room}' at {slot}; "
                    f"cannot verify suitability")
            continue
        actual_type = rooms_cfg[room].get("type")
        if actual_type not in allowed:
            if (sec_id, room) not in suitability_seen:
                suitability_seen.add((sec_id, room))
                fail["room_suitability"].append(
                    f"section {sec_id} (subject {sec['subject']}) is in room "
                    f"'{room}' of type {actual_type} at {slot}, but only these "
                    f"types are allowed: " + ", ".join(allowed))
            continue
        # Seat capacity (only checkable when enrolments are provided).
        if enr is not None and (sec_id, room) not in seat_checked_sections:
            seat_checked_sections.add((sec_id, room))
            n_students = enrolled_count.get(sec_id)
            seats = rooms_cfg[room].get("seats")
            if n_students is None:
                warn["room_suitability"].append(
                    f"section {sec_id}: no enrolment data, seat capacity of room "
                    f"'{room}' not verified")
            elif seats is not None and n_students > seats:
                fail["room_suitability"].append(
                    f"section {sec_id} has {n_students} enrolled students but room "
                    f"'{room}' only seats {seats}")

    # ---- 5. period_counts ----------------------------------------------------
    # Iterate over CONFIG sections so deleted/missing rows are caught too.
    for sec_id, sec in sorted(sections_cfg.items()):
        if sec_id in sec2block:
            expected = blocks_cfg[sec2block[sec_id]]["periods_per_cycle"]
            origin = f"block {sec2block[sec_id]}"
        else:
            expected = sec["periods_per_cycle"]
            origin = "section config"
        got = len(section_slots.get(sec_id, set()))
        if got != expected:
            fail["period_counts"].append(
                f"section {sec_id} meets {got} time(s) per cycle but should meet "
                f"{expected} ({origin})")

    # ---- 6. block_integrity --------------------------------------------------
    for block_id, block in sorted(blocks_cfg.items()):
        member_slots = {s: frozenset(section_slots.get(s, set()))
                        for s in block.get("sections", [])}
        if len(set(member_slots.values())) > 1:
            # Describe the disagreement against the most common slot set.
            detail = "; ".join(
                f"{s}: {sorted(slots) if slots else 'no meetings'}"
                for s, slots in sorted(member_slots.items()))
            fail["block_integrity"].append(
                f"block {block_id}: member sections do not share identical slot "
                f"sets — {detail}")

    # ---- 7. teacher_loads ------------------------------------------------------
    pinned: dict[str, dict[str, str]] = defaultdict(dict)  # teacher -> slot -> label
    for entry in cfg.get("pinned_busy", []) or []:
        for slot in entry.get("slots", []):
            pinned[entry["teacher"]][slot] = entry.get("label", "pinned commitment")

    for tid, slots in sorted(teacher_all_slots.items()):
        teacher = teachers_cfg.get(tid)
        if teacher is None:
            fail["teacher_loads"].append(
                f"teacher id '{tid}' appears in assignments but not in the config")
            continue
        limit_per_week = _teacher_limit(teacher, tiers, leadership_overrides)
        if limit_per_week is None:
            fail["teacher_loads"].append(
                f"teacher {tid} ({teacher.get('name')}) has unknown tier "
                f"'{teacher.get('tier')}' and no max_per_week — cannot bound load")
        else:
            limit = limit_per_week * len(weeks)
            if len(slots) > limit:
                fail["teacher_loads"].append(
                    f"teacher {tid} ({teacher.get('name')}) teaches "
                    f"{len(slots)} periods per cycle, above the limit of {limit} "
                    f"({limit_per_week}/week x {len(weeks)} weeks)")
        for slot in sorted(slots & set(pinned.get(tid, {}))):
            fail["teacher_loads"].append(
                f"teacher {tid} ({teacher.get('name')}) is assigned a lesson at "
                f"{slot} but is pinned busy then ({pinned[tid][slot]})")
        unavailable = set(teacher.get("unavailable") or [])
        for slot in sorted(slots & unavailable):
            fail["teacher_loads"].append(
                f"teacher {tid} ({teacher.get('name')}) is assigned a lesson at "
                f"{slot} but is listed as unavailable then")

    # ---- 8. grid_validity --------------------------------------------------------
    seen_bad_slots = set()
    for _, row in asg.iterrows():
        slot = row["slot_ref"]
        if slot not in valid_slots:
            key = (row["section_id"], slot)
            if key not in seen_bad_slots:
                seen_bad_slots.add(key)
                fail["grid_validity"].append(
                    f"section {row['section_id']} is scheduled at {slot}, which is "
                    f"not a valid teaching slot on this grid")

    # ---- 9. max_per_day ------------------------------------------------------------
    # Event identity re-derived from config: block id if the section is in a
    # block, else the section's own id.
    event_day_periods: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for _, row in asg.iterrows():
        event = sec2block.get(row["section_id"], row["section_id"])
        event_day_periods[(event, row["week"], row["day"])].add(row["period"])
    for (event, week, day), periods in sorted(event_day_periods.items()):
        if event in blocks_cfg:
            limit = blocks_cfg[event].get("max_per_day")
        elif event in sections_cfg:
            sec = sections_cfg[event]
            limit = sec.get("max_per_day")
            if limit is None:
                limit = subjects_cfg.get(sec["subject"], {}).get("max_per_day")
        else:
            limit = None
        if limit is not None and len(periods) > limit:
            fail["max_per_day"].append(
                f"event {event} meets {len(periods)} times on {week}.{day} "
                f"({', '.join(sorted(periods))}) but allows at most {limit} per day")

    # ---- 10. doubles ---------------------------------------------------------------
    forbid_subjects = {s["id"] for s in cfg.get("subjects", [])
                       if s.get("doubles") == "forbid"}
    if forbid_subjects:
        section_day_periods: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for _, row in asg.iterrows():
            section_day_periods[(row["section_id"], row["week"], row["day"])].add(
                row["period"])
        for (sec_id, week, day), periods in sorted(section_day_periods.items()):
            sec = sections_cfg.get(sec_id)
            if sec is None or sec["subject"] not in forbid_subjects:
                continue
            day_pairs = adjacency.get(day, set())
            plist = sorted(periods)
            for i in range(len(plist)):
                for j in range(i + 1, len(plist)):
                    if frozenset((plist[i], plist[j])) in day_pairs:
                        fail["doubles"].append(
                            f"section {sec_id} (subject {sec['subject']}, doubles "
                            f"forbidden) meets in adjacent periods {plist[i]} and "
                            f"{plist[j]} on {week}.{day}")

    # ---- 11. conflicts ----------------------------------------------------------------
    def slots_of(ident: str) -> set[str]:
        if ident in blocks_cfg:
            out: set[str] = set()
            for s in blocks_cfg[ident].get("sections", []):
                out |= section_slots.get(s, set())
            return out
        return set(section_slots.get(ident, set()))

    for group in cfg.get("conflicts", []) or []:
        label = group.get("label", "conflict")
        for pair in group.get("pairs", []):
            if len(pair) != 2:
                continue
            id_a, id_b = pair
            overlap = slots_of(id_a) & slots_of(id_b)
            if overlap:
                fail["conflicts"].append(
                    f"[{label}] {id_a} and {id_b} must never share a slot but "
                    f"overlap at: " + ", ".join(sorted(overlap)))

    # ---- stats ---------------------------------------------------------------
    report.stats = {
        "sections": int(asg["section_id"].nunique()),
        "meetings": int(len(asg)),
        "teachers": int(len(teacher_all_slots)),
        "rooms used": int(asg.loc[asg["room"] != "", "room"].nunique()),
        "students checked": int(students_checked),
    }
    return report


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) not in (2, 3):
        print("usage: python -m ttbuilder.validate.checker "
              "<config.yaml> <assignments.csv> [enrolments.csv]", file=sys.stderr)
        return 2
    config_path, assignments_path = argv[0], argv[1]
    enrolments_path = argv[2] if len(argv) == 3 else None
    report = validate(config_path, assignments_path, enrolments_path)
    print(report.render())
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
