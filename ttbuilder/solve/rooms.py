"""Concrete room assignment, after scheduling.

Per slot this is a bipartite matching (sections needing rooms vs room
capacity), guaranteed solvable by the Hall conditions enforced in the engine.
Processing slots in order with stability-aware costs gives each section and
teacher a steady room without touching the schedule. Solved per-slot with a
tiny CP-SAT model (sizes are ~dozens of sections × ~100 rooms).
"""
from __future__ import annotations

from collections import defaultdict

from ortools.sat.python import cp_model

from ..model.entities import Instance


def assign_rooms(inst: Instance, schedule: dict[str, list[int]]
                 ) -> dict[tuple[str, int], str]:
    cfg = inst.config
    rooms = list(cfg.rooms)
    by_type = defaultdict(list)
    for r in rooms:
        by_type[r.type].append(r)

    # who needs a room when
    demand_by_slot: dict[int, list] = defaultdict(list)
    ev_by_id = {e.id: e for e in inst.events}
    for eid, slots in schedule.items():
        e = ev_by_id[eid]
        for sec in e.sections:
            if not sec.room_types:
                continue
            for t in slots:
                demand_by_slot[t].append(sec)

    assignment: dict[tuple[str, int], str] = {}
    last_room: dict[str, str] = {}            # section -> last room used
    teacher_last: dict[tuple[str, int], str] = {}

    for t in sorted(demand_by_slot):
        secs = demand_by_slot[t]
        m = cp_model.CpModel()
        y = {}
        cost_terms = []
        for si, sec in enumerate(secs):
            eligible = []
            for rt in sec.room_types:
                eligible.extend(by_type.get(rt, []))
            if sec.room_pin:
                pinned = [r for r in eligible if r.id == sec.room_pin]
                eligible = pinned or eligible
            choices = []
            for r in eligible:
                if r.seats < sec.size:
                    continue
                v = m.new_bool_var(f"y[{si},{r.id}]")
                y[si, r.id] = v
                choices.append(v)
                cost = 0
                if last_room.get(sec.id) == r.id:
                    cost -= cfg.weights.room_stability_class
                for tch in sec.teachers:
                    if teacher_last.get((tch, t - 1)) == r.id:
                        cost -= cfg.weights.room_stability_teacher
                if sec.room_pin == r.id:
                    cost -= 10
                if cost:
                    cost_terms.append((v, cost))
            if not choices:
                # seats filter starved it — retry ignoring seats, flag later
                for r in eligible:
                    v = m.new_bool_var(f"y[{si},{r.id}]")
                    y[si, r.id] = v
                    choices.append(v)
            if not choices:
                raise RuntimeError(
                    f"No eligible room for section {sec.id} at slot {t} "
                    f"(types {sec.room_types}) — engine capacity constraint "
                    f"should have prevented this.")
            m.add_exactly_one(choices)
        for r in rooms:
            vs = [y[si, r.id] for si in range(len(secs)) if (si, r.id) in y]
            if vs:
                m.add(sum(vs) <= r.max_parallel)
        if cost_terms:
            m.minimize(sum(v * c for v, c in cost_terms))
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 10
        solver.parameters.num_workers = 1   # tiny models; see TEST_LOG #24
        st = solver.solve(m)
        if solver.status_name(st) not in ("OPTIMAL", "FEASIBLE"):
            raise RuntimeError(
                f"Room matching failed at slot {t} — Hall conditions in the "
                f"engine should make this impossible; please report.")
        for (si, rid), v in y.items():
            if solver.value(v):
                sec = secs[si]
                assignment[sec.id, t] = rid
                last_room[sec.id] = rid
                for tch in sec.teachers:
                    teacher_last[tch, t] = rid
    return assignment
