"""Scripted UI tests for the Timetable Builder web app (AppTest).

Covers: demo happy path end-to-end (check -> build -> verified bundle),
fresh-wizard navigation with validation, broken-school explanations,
and the never-crash guard.
"""
import os
import sys

import yaml
from streamlit.testing.v1 import AppTest

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

APP = os.path.join(HERE, "streamlit_app.py")


def make_at():
    at = AppTest.from_file(APP, default_timeout=30)
    return at


def test_welcome_renders():
    at = make_at().run()
    assert not at.exception
    assert any("Build your school's timetable" in str(t.value)
               for t in at.title)


def test_demo_check_and_build():
    at = make_at().run()
    at.button(key="btn_demo").click().run()
    # demo jumps straight to the check step
    assert st_text(at).count("Does everything add up") >= 1
    at.button(key="btn_runcheck").click().run(timeout=120)
    assert not at.exception
    assert any("Ready to build" in str(s.value) for s in at.success), \
        [str(e.value) for e in at.error]
    # go to build step and run a quick draft
    at.session_state.step = 8
    at.run()
    at.radio[0].set_value("Quick draft (1 min)").run()
    at.button(key="btn_build").click().run(timeout=400)
    assert not at.exception
    assert any("independently verified" in str(s.value)
               for s in at.success), \
        [str(e.value) for e in at.error] or "no success banner"
    # downloads exist
    assert at.session_state.results.zip_bytes
    assert len(at.session_state.results.zip_bytes) > 50_000
    assert at.session_state.results.scorecard_md


def test_fresh_wizard_validates_school_name():
    at = make_at().run()
    at.button(key="btn_start").click().run()
    # step 1 shown; try to continue without a name
    nexts = [b for b in at.button if "continue" in str(b.label).lower()]
    nexts[0].click().run()
    assert any("name" in str(e.value).lower() for e in at.error)


def test_overfull_curriculum_explained():
    """A school whose pupils have more lessons than the week holds must be
    blocked at the check with a plain-English message and a fix hint."""
    cfg = yaml.safe_load(open(os.path.join(HERE, "demo_school.yaml")))
    # G9 maths from 5 to 20 periods -> overflows the 30-slot week
    for row in cfg["year_groups"][2]["curriculum"]:
        if row["subject"] == "Maths":
            row["periods_per_cycle"] = 20
    at = make_at()
    at.session_state.school = cfg
    at.session_state.step = 7
    at.session_state.visited = set(range(1, 7))
    at.run()
    at.button(key="btn_runcheck").click().run(timeout=120)
    assert not at.exception
    texts = " ".join(str(e.value) for e in at.error) + " " + st_text(at)
    assert "G9" in texts and "teaching slots" in texts, texts[:500]


def test_impossible_staffing_explained():
    cfg = yaml.safe_load(open(os.path.join(HERE, "demo_school.yaml")))
    cfg["staffing"]["teachers"] = [
        t for t in cfg["staffing"]["teachers"]
        if t["id"] not in ("SCI1", "SCI2", "SCI3")]
    at = make_at()
    at.session_state.school = cfg
    at.session_state.step = 7
    at.session_state.visited = set(range(1, 7))
    at.run()
    at.button(key="btn_runcheck").click().run(timeout=120)
    assert not at.exception
    texts = st_text(at) + " ".join(str(e.value) for e in at.error)
    assert "Science" in texts
    # the fix hint points at the teachers step
    assert "Step 4" in texts or "step 4" in texts or "teacher" in texts.lower()


def test_corrupt_school_never_crashes():
    at = make_at()
    at.session_state.school = {"school": {"name": "X"}, "grid": "garbage"}
    at.session_state.step = 7
    at.run()
    at.button(key="btn_runcheck").click().run(timeout=60)
    assert not at.exception   # guarded: error message, never a stack trace


def st_text(at) -> str:
    """All visible markdown/caption/header text joined."""
    bits = []
    for kind in ("markdown", "caption", "header", "subheader", "error",
                 "success", "info", "warning"):
        try:
            for el in getattr(at, kind):
                bits.append(str(el.value))
        except Exception:   # noqa: BLE001
            pass
    return " ".join(bits)
