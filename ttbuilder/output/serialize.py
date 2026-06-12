"""Canonical solution artifact: assignments.csv.

One row per (section, meeting). Every downstream consumer — Excel books,
PDFs, the independent validator — reads THIS file, never solver state.
"""
from __future__ import annotations

import csv

from ..model.entities import Instance

COLUMNS = ["section_id", "event_id", "subject", "labels", "teachers",
           "teacher_names", "grade", "size", "week", "day", "period",
           "slot_ref", "room", "room_type"]


def write_assignments(inst: Instance, schedule: dict[str, list[int]],
                      rooms: dict[tuple[str, int], str], path: str) -> int:
    room_type = {r.id: r.type for r in inst.rooms}
    rows = []
    for e in inst.events:
        for t in schedule.get(e.id, []):
            s = inst.slots[t]
            for sec in e.sections:
                rid = rooms.get((sec.id, t), "")
                rows.append({
                    "section_id": sec.id, "event_id": e.id,
                    "subject": sec.subject,
                    "labels": "|".join(sec.labels),
                    "teachers": "|".join(sec.teachers),
                    "teacher_names": "|".join(
                        inst.teachers[x].name for x in sec.teachers
                        if x in inst.teachers),
                    "grade": sec.grade or "",
                    "size": sec.size,
                    "week": s.week, "day": s.day, "period": s.period,
                    "slot_ref": s.ref,
                    "room": rid, "room_type": room_type.get(rid, ""),
                })
    rows.sort(key=lambda r: (r["week"], r["day"], r["period"],
                             r["section_id"]))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)
