"""CP-SAT scheduling engine.

Model shape (see RESEARCH.md):
- sparse booleans x[event, slot] over each event's allowed slots;
- blocks are single events (member sections inherit times);
- rooms enter only as per-slot type-capacity (Hall) constraints — concrete
  rooms are assigned afterwards in rooms.py (exact decomposition);
- soft goals are penalty terms in one weighted sum;
- diagnosis mode wraps constraint families in assumption literals.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from ortools.sat.python import cp_model

from ..model.entities import Instance


@dataclass
class SolveResult:
    status: str                      # OPTIMAL/FEASIBLE/INFEASIBLE/UNKNOWN
    schedule: dict[str, list[int]] = field(default_factory=dict)
    objective: float | None = None
    bound: float | None = None
    wall_s: float = 0.0
    core: list[str] = field(default_factory=list)   # diagnosis families
    soft_breakdown: dict[str, int] = field(default_factory=dict)


def _adjacent_pairs(inst: Instance) -> list[tuple[int, int]]:
    """Teaching-slot pairs that are consecutive with no break between."""
    cfg = inst.config
    pairs = []
    for week in cfg.grid.weeks:
        for day in cfg.grid.days:
            prev = None
            for p in cfg.grid.periods_for_day(day):
                if p.kind == "teaching":
                    cur = inst.slot_by_ref[f"{week}.{day}.{p.id}"]
                    if prev is not None:
                        pairs.append((prev, cur))
                    prev = cur
                else:
                    prev = None     # break/registration interrupts adjacency
    return pairs


def _eligibility_unions(events) -> list[frozenset]:
    base = {frozenset(key) for e in events for key in e.room_demand}
    unions = set(base)
    # closure under union, bounded; school room taxonomies are tiny
    for a, b in combinations(list(base), 2):
        unions.add(a | b)
    return sorted(unions, key=lambda u: (len(u), sorted(u)))


class EngineModel:
    def __init__(self, inst: Instance, diagnose: bool = False):
        self.inst = inst
        self.diagnose = diagnose
        self.m = cp_model.CpModel()
        self.x: dict[tuple[int, int], cp_model.IntVar] = {}
        self.assumptions: dict[str, cp_model.IntVar] = {}
        self.penalties: list[tuple[str, cp_model.IntVar, int]] = []
        self._build()

    # -- assumption helper ------------------------------------------------
    def _family(self, name: str):
        if not self.diagnose:
            return None
        if name not in self.assumptions:
            self.assumptions[name] = self.m.new_bool_var(f"assume::{name}")
        return self.assumptions[name]

    def _add(self, ct, family: str | None):
        lit = self._family(family) if family else None
        if lit is not None:
            ct.only_enforce_if(lit)
        return ct

    # -- model ------------------------------------------------------------
    def _build(self):
        inst, m = self.inst, self.m
        cfg = inst.config
        weeks = cfg.grid.weeks

        for ei, e in enumerate(inst.events):
            for t in sorted(e.allowed):
                self.x[ei, t] = m.new_bool_var(f"x[{e.id}@{t}]")

        # H3: exact period counts
        for ei, e in enumerate(inst.events):
            vars_e = [self.x[ei, t] for t in sorted(e.allowed)]
            self._add(m.add(sum(vars_e) == e.periods_per_cycle),
                      f"periods:{e.id}")

        # H1: teacher at most one event per slot
        ev_by_teacher = defaultdict(list)
        for ei, e in enumerate(inst.events):
            for tch in e.teachers:
                ev_by_teacher[tch].append(ei)
        for tch, eis in ev_by_teacher.items():
            for t in range(len(inst.slots)):
                vs = [self.x[ei, t] for ei in eis if (ei, t) in self.x]
                if len(vs) > 1:
                    self._add(m.add(sum(vs) <= 1),
                              f"teacher:{inst.teachers[tch].name}")

        # H2: student populations never clash
        ev_by_atom = defaultdict(list)
        for ei, e in enumerate(inst.events):
            for a in e.atoms:
                ev_by_atom[a].append(ei)
        for a, eis in ev_by_atom.items():
            for t in range(len(inst.slots)):
                vs = [self.x[ei, t] for ei in eis if (ei, t) in self.x]
                if len(vs) > 1:
                    self._add(m.add(sum(vs) <= 1), f"students:{a}")

        # H2b: explicit conflict pairs (real-enrolment clash structure)
        for label, pairs in inst.conflict_pairs.items():
            for ea, eb in pairs:
                shared = (inst.events[ea].allowed & inst.events[eb].allowed)
                for t in shared:
                    self._add(m.add(self.x[ea, t] + self.x[eb, t] <= 1),
                              f"students:{label}")

        # H5/H6: room-type capacity per slot (Hall conditions)
        unions = _eligibility_unions(inst.events)
        for U in unions:
            cap = sum(inst.room_type_capacity.get(rt, 0) for rt in U)
            terms = []
            for ei, e in enumerate(inst.events):
                c = sum(n for key, n in e.room_demand.items()
                        if frozenset(key) <= U)
                if c:
                    terms.append((ei, c))
            if not terms:
                continue
            for t in range(len(inst.slots)):
                vs = [(self.x[ei, t], c) for ei, c in terms if (ei, t) in self.x]
                if sum(c for _, c in vs) > cap:
                    self._add(
                        m.add(sum(v * c for v, c in vs) <= cap),
                        f"rooms:{'+'.join(sorted(U))}")

        # H8: teacher max load
        for tch, eis in ev_by_teacher.items():
            ti = inst.teachers[tch]
            load = sum(self.x[ei, t] for ei in eis
                       for t in sorted(inst.events[ei].allowed))
            self._add(m.add(load + len(ti.pinned) <= ti.max_per_cycle),
                      f"load:{ti.name}")

        # H12/H13 + S1/S2 day-structure terms
        w = cfg.weights
        adj = _adjacent_pairs(inst)
        adj_by_first = defaultdict(list)
        for a, b in adj:
            adj_by_first[a].append(b)
        for ei, e in enumerate(inst.events):
            day_counts = []
            for (wk, day), idxs in inst.slots_by_day.items():
                vs = [self.x[ei, t] for t in idxs if (ei, t) in self.x]
                if not vs:
                    continue
                dc = m.new_int_var(0, min(len(vs), e.max_per_day),
                                   f"dc[{e.id}|{wk}{day}]")
                self._add(m.add(sum(vs) == dc), f"daycap:{e.id}")
                day_counts.append((dc, vs))
                # S1: prefer one per day when two are allowed
                if e.max_per_day > 1 and w.spread_same_day:
                    over = m.new_int_var(0, e.max_per_day - 1, f"over{e.id}{wk}{day}")
                    m.add(over >= dc - 1)
                    self.penalties.append(("spread_same_day", over,
                                           w.spread_same_day))
                if e.doubles == "require" and e.periods_per_cycle % 2 == 0:
                    self._add(m.add(dc != 1), f"doubles:{e.id}")
            # doubles forbid: no two adjacent meetings
            if e.doubles == "forbid" and e.max_per_day > 1:
                for a, b in adj:
                    if (ei, a) in self.x and (ei, b) in self.x:
                        self._add(m.add(self.x[ei, a] + self.x[ei, b] <= 1),
                                  f"doubles:{e.id}")
            if e.doubles in ("require", "prefer"):
                # meetings on a day should come in adjacent pairs
                for (wk, day), idxs in inst.slots_by_day.items():
                    for t in idxs:
                        if (ei, t) not in self.x:
                            continue
                        nbrs = [self.x[ei, b] for b in adj_by_first.get(t, [])
                                if (ei, b) in self.x]
                        prevs = [self.x[ei, a] for a, b in adj if b == t
                                 and (ei, a) in self.x]
                        lone = m.new_bool_var(f"lone[{e.id}@{t}]")
                        m.add(self.x[ei, t] - sum(nbrs) - sum(prevs) <= lone)
                        if e.doubles == "require":
                            self._add(m.add(lone == 0), f"doubles:{e.id}")
                        else:
                            self.penalties.append(
                                ("doubles_prefer", lone, w.doubles_prefer))
            # S1b: week balance for two-week cycles
            if len(weeks) == 2 and e.periods_per_cycle >= 2 and w.spread_week_imbalance:
                wa = sum(self.x[ei, t] for t in sorted(e.allowed)
                         if inst.slots[t].week == weeks[0])
                imb = m.new_int_var(0, e.periods_per_cycle, f"imb[{e.id}]")
                half = e.periods_per_cycle
                m.add(2 * wa - half <= imb)
                m.add(half - 2 * wa <= imb)
                excess = m.new_int_var(0, e.periods_per_cycle, f"imbx[{e.id}]")
                m.add(excess >= imb - 1)
                self.penalties.append(("week_imbalance", excess,
                                       w.spread_week_imbalance))

        # S2/S5: teacher gaps and daily overload
        if w.teacher_gaps or w.teacher_daily_overload:
            for tch, eis in ev_by_teacher.items():
                ti = inst.teachers[tch]
                for (wk, day), idxs in inst.slots_by_day.items():
                    busy = []
                    for t in idxs:
                        vs = [self.x[ei, t] for ei in eis if (ei, t) in self.x]
                        if not vs and t not in ti.pinned:
                            busy.append(None)
                            continue
                        b = m.new_bool_var(f"busy[{tch}|{wk}{day}|{t}]")
                        if t in ti.pinned:
                            m.add(b == 1)
                        else:
                            m.add_max_equality(b, vs)
                        busy.append(b)
                    real = [b for b in busy if b is not None]
                    if len(real) < 3:
                        continue
                    if w.teacher_daily_overload:
                        over = m.new_int_var(0, len(real), f"od[{tch}{wk}{day}]")
                        m.add(over >= sum(real) - inst.config.solver.soft_daily_max)
                        self.penalties.append(
                            ("daily_overload", over, w.teacher_daily_overload))
                    if w.teacher_gaps:
                        # idle = not busy, but busy earlier and later that day
                        n = len(real)
                        for i in range(1, n - 1):
                            before = m.new_bool_var(f"bf[{tch}{wk}{day}{i}]")
                            m.add_max_equality(before, real[:i])
                            after = m.new_bool_var(f"af[{tch}{wk}{day}{i}]")
                            m.add_max_equality(after, real[i + 1:])
                            idle = m.new_bool_var(f"idle[{tch}{wk}{day}{i}]")
                            m.add_bool_and(
                                real[i].Not(), before, after
                            ).only_enforce_if(idle)
                            m.add_bool_or(
                                real[i], before.Not(), after.Not()
                            ).only_enforce_if(idle.Not())
                            self.penalties.append(
                                ("teacher_gaps", idle, w.teacher_gaps))

        if self.penalties:
            m.minimize(sum(v * wt for _, v, wt in self.penalties))


def solve(inst: Instance, hint: dict[str, list[int]] | None = None,
          time_limit: int | None = None) -> SolveResult:
    em = EngineModel(inst, diagnose=False)
    solver = cp_model.CpSolver()
    cfg = inst.config.solver
    solver.parameters.max_time_in_seconds = float(time_limit or cfg.time_limit_s)
    solver.parameters.num_workers = cfg.workers
    solver.parameters.random_seed = cfg.seed
    solver.parameters.log_search_progress = cfg.log
    if hint:
        idx = {e.id: i for i, e in enumerate(inst.events)}
        for eid, slots in hint.items():
            if eid not in idx:
                continue
            ei = idx[eid]
            for t in slots:
                if (ei, t) in em.x:
                    em.m.add_hint(em.x[ei, t], 1)
        solver.parameters.repair_hint = True
    t0 = time.time()
    status = solver.solve(em.m)
    wall = time.time() - t0
    name = solver.status_name(status)
    if name not in ("OPTIMAL", "FEASIBLE"):
        return SolveResult(status=name, wall_s=wall)
    sched = {}
    for ei, e in enumerate(inst.events):
        sched[e.id] = sorted(t for t in e.allowed
                             if solver.value(em.x[ei, t]) == 1)
    breakdown = defaultdict(int)
    for label, v, wt in em.penalties:
        breakdown[label] += solver.value(v) * wt
    return SolveResult(
        status=name, schedule=sched,
        objective=solver.objective_value if em.penalties else 0.0,
        bound=solver.best_objective_bound if em.penalties else 0.0,
        wall_s=wall, soft_breakdown=dict(breakdown))


def diagnose(inst: Instance, time_limit: int = 60) -> SolveResult:
    """On INFEASIBLE: name the conflicting constraint families."""
    em = EngineModel(inst, diagnose=True)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    solver.parameters.num_workers = 1          # required for assumptions
    em.m.add_assumptions(list(em.assumptions.values()))
    status = solver.solve(em.m)
    name = solver.status_name(status)
    if name != "INFEASIBLE":
        return SolveResult(status=name)
    rev = {v.index: fam for fam, v in em.assumptions.items()}
    core = [rev[i] for i in solver.sufficient_assumptions_for_infeasibility()
            if i in rev]
    return SolveResult(status="INFEASIBLE", core=sorted(set(core)))
