"""Session state: the school being built lives as a plain dict mirroring
the YAML config. Nothing is ever lost — the dict is downloadable at every
moment and any YAML can be loaded back in."""
from __future__ import annotations

import copy

import streamlit as st
import yaml

STEPS = [
    "Welcome",
    "1 · School & week",
    "2 · Rooms",
    "3 · Subjects",
    "4 · Teachers",
    "5 · Year groups",
    "6 · Option blocks",
    "7 · Check",
    "8 · Build timetable",
]

FRESH_SCHOOL = {
    "school": {"name": "", "academic_year": "", "class_size_max": 26},
    "grid": {
        "weeks": ["Wk"],
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "periods": [
            {"id": "HR", "kind": "registration"},
            {"id": "P1", "kind": "teaching"},
            {"id": "P2", "kind": "teaching"},
            {"id": "P3", "kind": "teaching"},
            {"id": "Break", "kind": "break"},
            {"id": "P4", "kind": "teaching"},
            {"id": "P5", "kind": "teaching"},
            {"id": "Lunch", "kind": "break"},
            {"id": "P6", "kind": "teaching"},
        ],
        "day_overrides": {},
    },
    "room_types": ["classroom", "science_lab", "arts_space", "pe_space"],
    "rooms": [],
    "subjects": [],
    "staffing": {
        "tiers": {"classroom_teacher": 25, "middle_leader": 18,
                  "assistant_head": 12, "slt": 0},
        "teachers": [],
        "leadership_overrides": {},
    },
    "year_groups": [],
    "sections": [],
    "blocks": [],
    "conflicts": [],
    "attendance_exceptions": [],
    "pinned_busy": [],
    "solver": {"time_limit_s": 180, "workers": 0, "seed": 7},
    "weights": {},
}


def init_state():
    ss = st.session_state
    ss.setdefault("step", 0)
    ss.setdefault("school", None)        # the config dict (None until chosen)
    ss.setdefault("solution", None)      # last solve schedule (warm start)
    ss.setdefault("results", None)       # last results bundle for display
    ss.setdefault("visited", set())


def fresh_school() -> dict:
    return copy.deepcopy(FRESH_SCHOOL)


def school() -> dict:
    return st.session_state.school


def to_yaml(cfg: dict) -> str:
    return yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, width=120)


def from_yaml(text: str) -> tuple[dict | None, str]:
    """Returns (config dict, '') or (None, friendly error)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return None, ("That file isn't valid YAML. The exact problem "
                      f"reported was: {e}")
    if not isinstance(data, dict) or "grid" not in data:
        return None, ("That file doesn't look like a school file — it has "
                      "no 'grid' section. Use a file saved by this app or "
                      "the config template.")
    base = fresh_school()
    base.update(data)   # tolerate missing optional keys
    return base, ""


def goto(step: int):
    st.session_state.step = max(0, min(step, len(STEPS) - 1))
    st.rerun()


def teaching_slot_count(cfg: dict) -> int:
    """Teaching slots per cycle, honouring day overrides."""
    grid = cfg["grid"]
    teach = [p["id"] for p in grid["periods"] if p.get("kind", "teaching") == "teaching"]
    total = 0
    for _week in grid["weeks"]:
        for day in grid["days"]:
            ids = grid.get("day_overrides", {}).get(day)
            total += len([p for p in (ids if ids else teach)
                          if p in teach])
    return total
