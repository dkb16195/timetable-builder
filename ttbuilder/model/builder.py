"""Config → solving Instance.

Responsibilities:
- enumerate teaching slots (Friday short day etc.);
- expand curriculum mode into sections + blocks;
- resolve population tree into atoms per section/event;
- apply leadership overrides and tier load limits;
- fold blocks into single events (LinkEvents-as-meta-event).

Raises ConfigError with plain-English messages on any inconsistency.
"""
from __future__ import annotations

import math
from collections import defaultdict

from ..config.schema import Block, Config, Section
from .entities import Event, Instance, SectionInfo, Slot, TeacherInfo


class ConfigError(Exception):
    """A configuration problem, with a human-readable message list."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


def build_slots(cfg: Config):
    slots, idx = [], 0
    slots_by_day = {}
    for week in cfg.grid.weeks:
        for day in cfg.grid.days:
            day_idx = []
            for p in cfg.grid.periods_for_day(day):
                if p.kind != "teaching":
                    continue
                s = Slot(week=week, day=day, period=p.id, index=idx,
                         day_key=(week, day))
                slots.append(s)
                day_idx.append(idx)
                idx += 1
            slots_by_day[(week, day)] = day_idx
    return slots, slots_by_day


def _population_tree(cfg: Config):
    """Return (parent map, atom map). Atoms are leaf populations.

    Population ids: every YearGroup id is a population; its `populations`
    entries are children (bands/houses). A section's atom set is the leaves
    under (or above) its population — overlap = shared leaves.
    """
    parent: dict[str, str | None] = {}
    children = defaultdict(list)
    for yg in cfg.year_groups:
        parent.setdefault(yg.id, None)
        for p in yg.populations:
            par = p.parent or yg.id
            parent[p.id] = par
            children[par].append(p.id)
    # populations referenced by sections but never declared become standalone
    for s in cfg.sections:
        if s.population not in parent:
            parent[s.population] = None
    leaves = {}

    def collect_leaves(pid):
        if pid in leaves:
            return leaves[pid]
        kids = children.get(pid, [])
        out = frozenset([pid]) if not kids else frozenset().union(
            *(collect_leaves(k) for k in kids))
        leaves[pid] = out
        return out

    for pid in list(parent):
        collect_leaves(pid)
    return parent, leaves


def expand_curriculum(cfg: Config) -> tuple[list[Section], list[Block]]:
    """Generate sections/blocks for curriculum-mode year groups."""
    gen_sections: list[Section] = []
    gen_blocks: list[Block] = []
    explicit_ids = {s.id for s in cfg.sections}
    explicit_blocks = {b.id for b in cfg.blocks}
    counters: dict[tuple[str, str], int] = {}
    pop_sizes = {p.id: p.size for yg in cfg.year_groups
                 for p in yg.populations}
    for yg in cfg.year_groups:
        for row in yg.curriculum:
            pop = row.population or yg.id
            participants = (row.participants or pop_sizes.get(pop)
                            or yg.students)
            size_max = row.size_max or cfg.school.class_size_max
            n = row.classes or math.ceil(participants / size_max)
            size = math.ceil(participants / n)
            ids = []
            for _ in range(n):
                counters[(pop, row.subject)] = \
                    counters.get((pop, row.subject), 0) + 1
                sid = f"{pop}/{row.subject}{counters[(pop, row.subject)]}"
                if sid in explicit_ids:
                    continue
                gen_sections.append(Section(
                    id=sid, subject=row.subject, population=pop,
                    periods_per_cycle=row.periods_per_cycle, size=size,
                    grade=yg.id))
                ids.append(sid)
            if row.simultaneous and len(ids) > 1:
                bid = f"{pop}/{row.subject}-block"
                if bid not in explicit_blocks:
                    sj = next((s for s in cfg.subjects
                               if s.id == row.subject), None)
                    gen_blocks.append(Block(
                        id=bid, sections=ids,
                        periods_per_cycle=row.periods_per_cycle,
                        max_per_day=sj.max_per_day if sj else 1,
                        doubles=sj.doubles if sj else "allow"))
    return gen_sections, gen_blocks


def materialize_curriculum(cfg: Config) -> int:
    """Move curriculum-generated sections/blocks INTO cfg (so the staffing
    stage and any inspection see them). Idempotent. Returns count added."""
    gen_sections, gen_blocks = expand_curriculum(cfg)
    cfg.sections.extend(gen_sections)
    cfg.blocks.extend(gen_blocks)
    return len(gen_sections)


def build_instance(cfg: Config) -> Instance:
    problems: list[str] = []
    warnings: list[str] = []
    slots, slots_by_day = build_slots(cfg)
    slot_by_ref = {s.ref: s.index for s in slots}
    n_slots = len(slots)

    subj = {s.id: s for s in cfg.subjects}
    rooms_by_type = defaultdict(int)
    for r in cfg.rooms:
        rooms_by_type[r.type] += r.max_parallel

    # ---------------- teachers
    tiers = cfg.staffing.tiers
    teachers: dict[str, TeacherInfo] = {}
    name_to_id = {}
    for t in cfg.staffing.teachers:
        tier = cfg.staffing.leadership_overrides.get(
            t.id, cfg.staffing.leadership_overrides.get(t.name, t.tier))
        if tier not in tiers:
            problems.append(
                f"Teacher '{t.name}': tier '{tier}' is not defined in "
                f"staffing.tiers ({', '.join(tiers)}).")
            tier = "classroom_teacher"
        per_week = t.max_per_week if t.max_per_week is not None else tiers[tier]
        unavailable = set()
        for ref in t.unavailable:
            if ref not in slot_by_ref:
                problems.append(
                    f"Teacher '{t.name}': unavailable slot '{ref}' does not "
                    f"exist in the grid (format week.day.period).")
            else:
                unavailable.add(slot_by_ref[ref])
        teachers[t.id] = TeacherInfo(
            id=t.id, name=t.name,
            max_per_cycle=per_week * len(cfg.grid.weeks),
            unavailable=unavailable, pinned={}, tier=tier)
        name_to_id[t.name] = t.id

    for pb in cfg.pinned_busy:
        tid = pb.teacher if pb.teacher in teachers else name_to_id.get(pb.teacher)
        if tid is None:
            problems.append(
                f"pinned_busy refers to unknown teacher '{pb.teacher}'.")
            continue
        for ref in pb.slots:
            if ref not in slot_by_ref:
                problems.append(
                    f"pinned_busy for '{pb.teacher}': slot '{ref}' not in grid.")
            else:
                teachers[tid].pinned[slot_by_ref[ref]] = pb.label

    # ---------------- sections (explicit + curriculum-generated)
    gen_sections, gen_blocks = expand_curriculum(cfg)
    all_sections = list(cfg.sections) + gen_sections
    all_blocks = list(cfg.blocks) + gen_blocks
    _, leaves = _population_tree(cfg)

    sec_by_id: dict[str, SectionInfo] = {}
    sec_periods: dict[str, int] = {}
    for s in all_sections:
        if s.id in sec_by_id:
            problems.append(f"Duplicate section id '{s.id}'.")
            continue
        if s.subject not in subj:
            problems.append(
                f"Section '{s.id}': subject '{s.subject}' is not defined "
                f"under subjects:.")
            continue
        sj = subj[s.subject]
        if s.size > cfg.school.class_size_max:
            warnings.append(
                f"Section '{s.id}' has size {s.size}, above the school "
                f"class-size cap of {cfg.school.class_size_max}.")
        for tid in s.teachers:
            if tid not in teachers and tid not in name_to_id:
                problems.append(
                    f"Section '{s.id}': teacher '{tid}' is not in staffing.teachers.")
        if s.room_types is not None:
            rt = list(s.room_types)
        else:
            rt = [] if sj.no_room_needed else list(sj.room_types)
        for one in rt:
            if rooms_by_type.get(one, 0) == 0 and one not in [r.type for r in cfg.rooms]:
                problems.append(
                    f"Subject '{sj.id}' (section '{s.id}') wants room type "
                    f"'{one}' but no room of that type exists.")
        sec_by_id[s.id] = SectionInfo(
            id=s.id, subject=s.subject, population=s.population,
            size=s.size,
            teachers=[t if t in teachers else name_to_id.get(t, t)
                      for t in s.teachers],
            room_types=rt, labels=list(s.labels), room_pin=s.room_pin,
            grade=s.grade)
        sec_periods[s.id] = s.periods_per_cycle

    # ---------------- events: blocks first, then leftover sections
    events: list[Event] = []
    in_block: set[str] = set()
    block_by_id: dict[str, Block] = {}
    for b in all_blocks:
        if b.id in block_by_id:
            problems.append(f"Duplicate block id '{b.id}'.")
            continue
        block_by_id[b.id] = b
        members = []
        for sid in b.sections:
            if sid not in sec_by_id:
                problems.append(
                    f"Block '{b.id}' refers to unknown section '{sid}'.")
                continue
            if sid in in_block:
                problems.append(
                    f"Section '{sid}' appears in more than one block.")
                continue
            members.append(sec_by_id[sid])
            in_block.add(sid)
        if not members:
            continue
        periods = b.periods_per_cycle or max(sec_periods[m.id] for m in members)
        mismatched = [m.id for m in members if sec_periods[m.id] != periods]
        if mismatched:
            problems.append(
                f"Block '{b.id}' runs {periods} periods/cycle but member(s) "
                f"{', '.join(mismatched)} declare a different count — block "
                f"members must all meet for the block's full duration.")
        events.append(_make_event(
            b.id, periods, members, leaves, subj, n_slots, teachers,
            max_per_day=b.max_per_day, doubles=b.doubles, is_block=True,
            problems=problems))

    sec_mpd = {s.id: s.max_per_day for s in all_sections}
    for sid, info in sec_by_id.items():
        if sid in in_block:
            continue
        sj = subj[info.subject]
        events.append(_make_event(
            sid, sec_periods[sid], [info], leaves, subj, n_slots, teachers,
            max_per_day=sec_mpd.get(sid) or sj.max_per_day,
            doubles=sj.doubles, is_block=False, problems=problems))

    # explicit conflict groups -> event-index pairs
    owner: dict[str, int] = {}
    for ei, e in enumerate(events):
        owner[e.id] = ei
        for s in e.sections:
            owner[s.id] = ei
    conflict_pairs: dict[str, set[tuple[int, int]]] = {}
    for grp in cfg.conflicts:
        pairs = set()
        for pair in grp.pairs:
            if len(pair) != 2:
                problems.append(
                    f"conflicts['{grp.label}'] has a non-pair entry: {pair}")
                continue
            a, b = pair
            if a not in owner or b not in owner:
                missing = [x for x in (a, b) if x not in owner]
                problems.append(
                    f"conflicts['{grp.label}'] refers to unknown id(s): "
                    f"{', '.join(missing)}")
                continue
            ea, eb = owner[a], owner[b]
            if ea != eb:
                pairs.add((min(ea, eb), max(ea, eb)))
        if pairs:
            conflict_pairs[grp.label] = pairs

    if problems:
        raise ConfigError(problems)

    atoms = sorted({a for e in events for a in e.atoms})
    return Instance(
        slots=slots, teaching_slot_idx=[s.index for s in slots],
        events=events, teachers=teachers, atoms=atoms,
        room_type_capacity=dict(rooms_by_type), rooms=list(cfg.rooms),
        slots_by_day=slots_by_day, slot_by_ref=slot_by_ref, config=cfg,
        conflict_pairs=conflict_pairs, warnings=warnings)


def _make_event(eid, periods, members, leaves, subj, n_slots, teachers,
                max_per_day=1, doubles="allow", is_block=False, problems=None):
    tset = set()
    for m in members:
        tset.update(t for t in m.teachers if t in teachers)
    atoms = set()
    for m in members:
        atoms |= leaves.get(m.population, frozenset([m.population]))
    allowed = set(range(n_slots))
    for t in tset:
        allowed -= teachers[t].unavailable
        allowed -= set(teachers[t].pinned)
    if len(allowed) < periods and problems is not None:
        problems.append(
            f"Event '{eid}' needs {periods} periods but its teachers are "
            f"jointly available for only {len(allowed)} slots — check "
            f"unavailability/meetings of: "
            f"{', '.join(sorted(teachers[t].name for t in tset))}.")
    return Event(id=eid, periods_per_cycle=periods, sections=members,
                 teachers=tset, atoms=atoms, max_per_day=max_per_day,
                 doubles=doubles, allowed=allowed, is_block=is_block)
