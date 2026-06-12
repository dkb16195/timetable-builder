"""Internal solving entities, produced by model.builder from a Config."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Slot:
    week: str
    day: str
    period: str
    index: int           # global order across the cycle
    day_key: tuple = ()  # (week, day)

    @property
    def ref(self) -> str:
        return f"{self.week}.{self.day}.{self.period}"


@dataclass
class SectionInfo:
    id: str
    subject: str
    population: str
    size: int
    teachers: list[str]
    room_types: list[str]        # eligible types; [] = no room needed
    labels: list[str] = field(default_factory=list)
    room_pin: str | None = None
    grade: str | None = None


@dataclass
class Event:
    """The scheduled unit: a standalone section or a whole block."""
    id: str
    periods_per_cycle: int
    sections: list[SectionInfo]
    teachers: set[str]           # union over sections (all busy at its slots)
    atoms: set[str]              # population atoms it occupies
    max_per_day: int
    doubles: str                 # forbid/allow/prefer/require
    allowed: set[int]            # slot indices the event may use
    is_block: bool = False

    @property
    def room_demand(self) -> dict[tuple, int]:
        """eligible-type-tuple -> how many simultaneous rooms needed."""
        d: dict[tuple, int] = {}
        for s in self.sections:
            if s.room_types:
                key = tuple(sorted(s.room_types))
                d[key] = d.get(key, 0) + 1
        return d


@dataclass
class Instance:
    slots: list[Slot]
    teaching_slot_idx: list[int]
    events: list[Event]
    teachers: dict[str, "TeacherInfo"]
    atoms: list[str]
    room_type_capacity: dict[str, int]        # type -> Σ max_parallel
    rooms: list                               # config Room objects
    slots_by_day: dict[tuple, list[int]]      # (week, day) -> ordered indices
    slot_by_ref: dict[str, int]
    config: object                            # the originating Config
    # explicit clash pairs: label -> set of (event_idx_a, event_idx_b)
    conflict_pairs: dict[str, set[tuple[int, int]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class TeacherInfo:
    id: str
    name: str
    max_per_cycle: int
    unavailable: set[int]
    pinned: dict[int, str]       # slot index -> label (meetings, duties)
    tier: str
