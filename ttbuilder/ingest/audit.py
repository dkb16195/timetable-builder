"""Static feasibility audit — arithmetic checks before any solving.

Each finding is (severity, message); severity 'block' means the solve cannot
succeed, 'warn' means it may struggle. Messages are written for school
leaders, not programmers.
"""
from __future__ import annotations

from collections import defaultdict

from ..model.entities import Instance


def run_audit(inst: Instance) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    n_slots = len(inst.slots)
    weeks = len(inst.config.grid.weeks)

    # 1. teacher demand vs maximum
    demand = defaultdict(int)
    for e in inst.events:
        for t in e.teachers:
            demand[t] += e.periods_per_cycle
    for tid, need in sorted(demand.items()):
        ti = inst.teachers[tid]
        cap = ti.max_per_cycle - len(ti.pinned)
        if need > cap:
            findings.append(("block",
                f"{ti.name} is asked to teach {need} periods per cycle but "
                f"their maximum is {ti.max_per_cycle} ({ti.tier}"
                f"{', minus ' + str(len(ti.pinned)) + ' pinned commitments' if ti.pinned else ''}). "
                f"Reduce their classes by {need - cap} periods or raise their tier limit."))
        free = n_slots - len(ti.unavailable) - len(ti.pinned)
        if need > free:
            findings.append(("block",
                f"{ti.name} needs {need} teaching periods but is only "
                f"available for {free} of the {n_slots} slots in the cycle."))

    # 2. room-type demand vs supply (Lach–Lübbecke counting over the cycle)
    type_demand = defaultdict(int)
    for e in inst.events:
        for key, count in e.room_demand.items():
            # single-type demand attributes fully; multi-type handled below
            if len(key) == 1:
                type_demand[key[0]] += count * e.periods_per_cycle
    for rt, need in sorted(type_demand.items()):
        supply = inst.room_type_capacity.get(rt, 0) * n_slots
        if need > supply:
            findings.append(("block",
                f"Subjects needing a {rt} require {need} room-periods per "
                f"cycle, but the school's {rt} capacity is "
                f"{inst.room_type_capacity.get(rt, 0)} rooms × {n_slots} "
                f"slots = {supply}. Add rooms of this type or reduce periods."))
        elif supply and need > 0.85 * supply:
            findings.append(("warn",
                f"{rt} utilisation will be {100 * need // supply}% "
                f"({need}/{supply} room-periods) — feasible but very tight."))

    # 3. per-atom (student population) period budget vs grid
    atom_load = defaultdict(int)
    for e in inst.events:
        for a in e.atoms:
            atom_load[a] += e.periods_per_cycle
    for a, load in sorted(atom_load.items()):
        if load > n_slots:
            findings.append(("block",
                f"Student group '{a}' is scheduled for {load} periods per "
                f"cycle but the grid only has {n_slots} teaching slots. "
                f"Remove {load - n_slots} periods from its curriculum."))

    # 4. event-level joint availability (already flagged at build, re-check)
    for e in inst.events:
        if len(e.allowed) < e.periods_per_cycle:
            names = ", ".join(sorted(
                inst.teachers[t].name for t in e.teachers))
            findings.append(("block",
                f"'{e.id}' needs {e.periods_per_cycle} periods but its "
                f"teacher(s) {names} are jointly free for only "
                f"{len(e.allowed)} slots."))
        # max_per_day arithmetic: can the event fit under its daily cap?
        day_caps = 0
        for (wk, day), idxs in inst.slots_by_day.items():
            avail = len([i for i in idxs if i in e.allowed])
            day_caps += min(avail, e.max_per_day)
        if day_caps < e.periods_per_cycle:
            findings.append(("block",
                f"'{e.id}' needs {e.periods_per_cycle} periods at no more "
                f"than {e.max_per_day}/day, but availability allows at most "
                f"{day_caps} across the cycle. Allow more per day or free up "
                f"teacher time."))

    # 5. simultaneous room pressure from blocks
    for e in inst.events:
        needed = sum(e.room_demand.values())
        total_at_once = sum(
            inst.room_type_capacity.get(t, 0)
            for t in {rt for key in e.room_demand for rt in key})
        if needed > total_at_once:
            findings.append(("block",
                f"Block '{e.id}' needs {needed} rooms at the same time but "
                f"only {total_at_once} suitable rooms exist."))

    # 6. teacher weekly-average sanity (warn only)
    per_week_cap = {t.id: t.max_per_cycle / weeks for t in inst.teachers.values()}
    for tid, need in sorted(demand.items()):
        if need / weeks > per_week_cap[tid] * 0.96 and need <= inst.teachers[tid].max_per_cycle:
            findings.append(("warn",
                f"{inst.teachers[tid].name} is at "
                f"{need / weeks:.1f}/{per_week_cap[tid]:.0f} periods per week "
                f"— at or near full load, no slack for changes."))
    return findings
