"""Typed configuration model. One YAML file describes a school.

Design notes (see PLAN.md / RESEARCH.md):
- every school-specific fact lives here, never in code;
- soft constraints carry the XHSTT-style cost cell (weight, linear/quadratic);
- blocks are sets of co-scheduled sections, never atomic subjects;
- curriculum mode generates sections from numbers; explicit mode lists them.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

PeriodKind = Literal["teaching", "registration", "break"]
DoublesPolicy = Literal["forbid", "allow", "prefer", "require"]


class Period(BaseModel):
    id: str
    kind: PeriodKind = "teaching"
    start: Optional[str] = None   # "08:05" — printing only
    end: Optional[str] = None


class Grid(BaseModel):
    weeks: list[str] = Field(min_length=1)          # ["A","B"] or ["Wk"]
    days: list[str] = Field(min_length=1)
    periods: list[Period] = Field(min_length=1)
    day_overrides: dict[str, list[str]] = {}        # day -> period ids present
    friday_times: dict[str, dict[str, str]] = {}    # period id -> {start,end}

    @model_validator(mode="after")
    def _check(self):
        ids = [p.id for p in self.periods]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate period ids in grid.periods")
        for day, plist in self.day_overrides.items():
            if day not in self.days:
                raise ValueError(f"day_overrides refers to unknown day '{day}'")
            unknown = set(plist) - set(ids)
            if unknown:
                raise ValueError(
                    f"day_overrides[{day}] refers to unknown periods {sorted(unknown)}")
        return self

    def periods_for_day(self, day: str) -> list[Period]:
        if day in self.day_overrides:
            keep = set(self.day_overrides[day])
            return [p for p in self.periods if p.id in keep]
        return list(self.periods)


class Room(BaseModel):
    id: str
    type: str
    seats: int = 24
    max_parallel: int = 1        # sports hall = 4
    notes: str = ""


class Subject(BaseModel):
    id: str
    name: str = ""
    department: str = ""                     # for department workbooks
    room_types: list[str] = ["classroom"]   # eligible types (any of)
    max_per_day: int = 1
    doubles: DoublesPolicy = "allow"
    no_room_needed: bool = False             # e.g. off-site PE

    @model_validator(mode="after")
    def _name(self):
        if not self.name:
            self.name = self.id
        return self


class Teacher(BaseModel):
    id: str
    name: str = ""
    subjects: list[str] = []
    tier: str = "classroom_teacher"
    max_per_week: Optional[int] = None       # overrides tier if set
    unavailable: list[str] = []              # slot refs "A.Monday.P1"
    notes: str = ""

    @model_validator(mode="after")
    def _name(self):
        if not self.name:
            self.name = self.id
        return self


class Staffing(BaseModel):
    tiers: dict[str, int] = {
        "classroom_teacher": 25, "middle_leader": 18,
        "assistant_head": 12, "slt": 0,
    }
    teachers: list[Teacher] = []
    # name/id -> tier; applied AFTER teachers load, so the list can arrive later
    leadership_overrides: dict[str, str] = {}


class Section(BaseModel):
    """One teaching group meeting N periods per cycle."""
    id: str
    subject: str
    population: str                  # population id (e.g. "G7.X", "G11")
    periods_per_cycle: int
    size: int = 0
    teachers: list[str] = []         # all occupied at this section's slots
    labels: list[str] = []           # extra class codes (merged HL/SL etc.)
    room_pin: Optional[str] = None
    block: Optional[str] = None      # set when generated inside a block
    grade: Optional[str] = None
    max_per_day: Optional[int] = None  # overrides the subject's setting
    room_types: Optional[list[str]] = None  # overrides the subject's setting
                                            # ([] = this section needs no room)
    needs_teacher: bool = True       # false = deliberately unstaffed
                                     # (supervised/self-study group)


class ConflictGroup(BaseModel):
    """Explicit pairs of sections/blocks that must never share a slot.

    Used when clash structure comes from real student enrolments rather
    than a clean population tree (overlapping bands, option mixes).
    """
    label: str
    pairs: list[list[str]] = []      # each entry: [id_a, id_b]


class Block(BaseModel):
    """Sections scheduled simultaneously (option line / band block)."""
    id: str
    sections: list[str] = []         # section ids (explicit mode)
    periods_per_cycle: Optional[int] = None
    max_per_day: int = 2             # lines may meet twice a day by default
    doubles: DoublesPolicy = "allow"


class CurriculumRow(BaseModel):
    subject: str
    periods_per_cycle: int
    classes: Optional[int] = None    # default: ceil(participants / size_max)
    participants: Optional[int] = None  # default: whole population
    size_max: Optional[int] = None   # default: school.class_size_max
    population: Optional[str] = None  # default: the year group itself
    simultaneous: bool = True        # parallel sets form a block


class Population(BaseModel):
    """A pool of students treated as a unit. Tree via parent."""
    id: str
    size: int = 0
    parent: Optional[str] = None


class YearGroup(BaseModel):
    id: str                          # "G7"
    students: int
    populations: list[Population] = []   # bands etc.; parent defaults to year
    curriculum: list[CurriculumRow] = []


class PinnedBusy(BaseModel):
    """A teacher commitment at fixed slots (meeting, duty, supervision)."""
    teacher: str
    slots: list[str]                 # "A.Monday.P5"
    label: str = ""


class Weights(BaseModel):
    spread_same_day: int = 20        # second meeting of a section same day
    spread_week_imbalance: int = 6   # |weekA - weekB| beyond 1
    teacher_gaps: int = 3            # idle period between lessons
    teacher_daily_overload: int = 5  # beyond soft daily max
    doubles_prefer: int = 10         # missing wanted double
    room_stability_class: int = 2    # used by the room-assignment stage
    room_stability_teacher: int = 1
    cost: Literal["linear", "quadratic"] = "linear"


class SolverCfg(BaseModel):
    time_limit_s: int = 120
    workers: int = 0
    seed: int = 7
    soft_daily_max: int = 6          # teacher periods/day before S5 penalty
    log: bool = False


class SchoolMeta(BaseModel):
    name: str
    academic_year: str = ""
    class_size_max: int = 24
    logo: Optional[str] = None


class Config(BaseModel):
    school: SchoolMeta
    grid: Grid
    room_types: list[str] = []
    rooms: list[Room] = []
    subjects: list[Subject] = []
    staffing: Staffing = Staffing()
    year_groups: list[YearGroup] = []
    sections: list[Section] = []     # explicit mode
    blocks: list[Block] = []
    conflicts: list[ConflictGroup] = []
    # event/section id pairs allowed to overlap despite shared students
    # (per-meeting membership: e.g. elective students leave PE once a cycle)
    attendance_exceptions: list[list[str]] = []
    pinned_busy: list[PinnedBusy] = []
    solver: SolverCfg = SolverCfg()
    weights: Weights = Weights()

    @field_validator("room_types", mode="after")
    @classmethod
    def _default_types(cls, v):
        return v or ["classroom"]
