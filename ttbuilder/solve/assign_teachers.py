"""Optional staffing stage: assign teachers to unstaffed sections.

Runs BEFORE scheduling (the workflow every mature product uses). A small
CP-SAT assignment model: each unstaffed section gets exactly one teacher
qualified in its subject, loads stay within tier maxima, no teacher takes
two sections of the same block (they'd be simultaneous), and the objective
balances loads while minimising how many distinct teachers each subject's
sections are split across.
"""
from __future__ import annotations

from collections import defaultdict

from ortools.sat.python import cp_model

from ..config.schema import Config
from ..model.builder import build_slots


class StaffingError(Exception):
    def __init__(self, problems):
        self.problems = problems
        super().__init__("\n".join(problems))


def assign_teachers(cfg: Config) -> dict[str, str]:
    """Return {section_id: teacher_id} for sections with no teachers.

    Mutates nothing; caller applies the result to the config/sections.
    """
    weeks = len(cfg.grid.weeks)
    tiers = cfg.staffing.tiers
    teachers = list(cfg.staffing.teachers)
    by_id = {t.id: t for t in teachers}

    slots, _ = build_slots(cfg)
    grid_refs = {s.ref for s in slots}

    cap = {}
    for t in teachers:
        tier = cfg.staffing.leadership_overrides.get(
            t.id, cfg.staffing.leadership_overrides.get(t.name, t.tier))
        per_week = t.max_per_week if t.max_per_week is not None \
            else tiers.get(tier, 25)
        # the tier ceiling (per-week × weeks) can exceed the slots a teacher
        # can actually attend — e.g. a short multi-week grid — and a load the
        # grid can't hold would only surface later as an audit block
        attendable = len(grid_refs) - len(grid_refs & set(t.unavailable))
        cap[t.id] = min(per_week * weeks, attendable)

    # existing committed load from already-staffed sections
    committed = defaultdict(int)
    unstaffed = []
    for s in cfg.sections:
        if s.teachers:
            for tid in s.teachers:
                if tid in cap:
                    committed[tid] += s.periods_per_cycle
        elif s.needs_teacher:
            unstaffed.append(s)
    for pb in cfg.pinned_busy:
        if pb.teacher in cap:
            committed[pb.teacher] += len(pb.slots)
    if not unstaffed:
        return {}

    qualified = {}
    problems = []
    for s in unstaffed:
        q = [t.id for t in teachers if s.subject in t.subjects
             and cap[t.id] - committed[t.id] > 0]
        if not q:
            problems.append(
                f"No teacher is qualified and available for '{s.id}' "
                f"({s.subject}, {s.periods_per_cycle} periods). Add the "
                f"subject to a teacher's list or hire for it.")
        qualified[s.id] = q
    if problems:
        raise StaffingError(problems)

    m = cp_model.CpModel()
    y = {}
    for s in unstaffed:
        choices = []
        for tid in qualified[s.id]:
            v = m.new_bool_var(f"y[{s.id},{tid}]")
            y[s.id, tid] = v
            choices.append(v)
        m.add_exactly_one(choices)

    block_of = {}
    for b in cfg.blocks:
        for sid in b.sections:
            block_of[sid] = b.id
    # a teacher can't take two sections of the same block
    per_block_teacher = defaultdict(list)
    for s in unstaffed:
        bid = block_of.get(s.id)
        if bid:
            for tid in qualified[s.id]:
                per_block_teacher[bid, tid].append(y[s.id, tid])
    staffed_in_block = defaultdict(set)
    for s in cfg.sections:
        if s.teachers and s.id in block_of:
            for tid in s.teachers:
                staffed_in_block[block_of[s.id]].add(tid)
    for (bid, tid), vs in per_block_teacher.items():
        limit = 0 if tid in staffed_in_block[bid] else 1
        if limit == 0:
            for v in vs:
                m.add(v == 0)
        elif len(vs) > 1:
            m.add(sum(vs) <= 1)

    # loads within caps; track deviation from a balanced target
    devs = []
    for t in teachers:
        terms = [(y[s.id, t.id], s.periods_per_cycle) for s in unstaffed
                 if (s.id, t.id) in y]
        if not terms:
            continue
        load = committed[t.id] + sum(v * p for v, p in terms)
        m.add(load <= cap[t.id])
        over = m.new_int_var(0, 200, f"dev+[{t.id}]")
        target = min(cap[t.id], 22 * weeks)   # comfortable default target
        m.add(over >= load - target)
        devs.append(over)

    # minimise subject splitting: penalise each (subject, teacher) pairing used
    pair_used = []
    by_subject = defaultdict(list)
    for s in unstaffed:
        by_subject[s.subject].append(s)
    for subj, secs in by_subject.items():
        tids = {tid for s in secs for tid in qualified[s.id]}
        for tid in tids:
            u = m.new_bool_var(f"used[{subj},{tid}]")
            vs = [y[s.id, tid] for s in secs if (s.id, tid) in y]
            m.add_max_equality(u, vs)
            pair_used.append(u)

    m.minimize(3 * sum(devs) + sum(pair_used))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 20
    # single worker: staffing models are tiny (sub-second), and the
    # multi-worker portfolio can deadlock pre-search on this model shape
    # (observed ortools 9.15 / py3.14 / macOS) — see TEST_LOG.md #24
    solver.parameters.num_workers = 1
    st = solver.solve(m)
    if solver.status_name(st) not in ("OPTIMAL", "FEASIBLE"):
        # name the shortfall: per-subject demand vs the free capacity of
        # its qualified pool (capacity shared across subjects is credited
        # to each, so a named shortfall here is a certain shortfall)
        msgs = []
        for subj, secs in sorted(by_subject.items()):
            demand = sum(s.periods_per_cycle for s in secs)
            pool = {tid for s in secs for tid in qualified[s.id]}
            free = sum(cap[t] - committed[t] for t in pool)
            if demand > free:
                names = ", ".join(sorted(by_id[t].name for t in pool))
                msgs.append(
                    f"{subj} needs {demand} periods per cycle but its "
                    f"qualified teacher(s) ({names}) have only {free} "
                    f"free between them. Add {subj} to another teacher's "
                    f"subjects, raise a load cap, or reduce its lessons.")
        if not msgs:
            msgs = ["Teachers cannot cover all classes within their "
                    "maximum loads — the shortfall comes from teachers "
                    "shared between subjects. Add capacity to whichever "
                    "of these subjects is easiest to staff: " +
                    ", ".join(sorted(by_subject)) + "."]
        raise StaffingError(msgs)
    out = {}
    for (sid, tid), v in y.items():
        if solver.value(v):
            out[sid] = tid
    return out


def apply_staffing(cfg: Config) -> dict[str, str]:
    """Assign + write back into cfg.sections. Returns the assignment map."""
    chosen = assign_teachers(cfg)
    for s in cfg.sections:
        if s.id in chosen:
            s.teachers = [chosen[s.id]]
    return chosen
