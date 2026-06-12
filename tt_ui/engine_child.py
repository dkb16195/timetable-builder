"""Dedicated child-process entrypoint for all engine work.

Launched as `python -m tt_ui.engine_child <task.json>` by runner.py —
deliberately NOT multiprocessing (spawn re-executes the parent's
__main__, which breaks under pytest/Streamlit/stdin hosts).

Task file: {"mode": "check"|"solve", "cfg": {...}, "time_limit": int,
            "warm": {...}|null, "zip_path": str,
            "progress_path": str, "result_path": str}
Progress: one plain-text line appended per stage.
Result: JSON written atomically (tmp+rename) to result_path.

Order of work in check: the cheap arithmetic audit runs BEFORE the
staffing solver, so the commonest mistakes (an overfull curriculum, a
missing room kind) are explained instantly without ever invoking CP-SAT.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import yaml


def _progress(path: str, msg: str):
    with open(path, "a") as f:
        f.write(msg + "\n")


def _write_result(path: str, payload: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def _audit_findings(cfg):
    from ttbuilder.ingest.audit import run_audit
    from ttbuilder.model.builder import build_instance
    return run_audit(build_instance(cfg))


def do_check(task) -> dict:
    from ttbuilder.config.loader import ConfigLoadError, load_config
    from ttbuilder.model.builder import ConfigError, materialize_curriculum
    from ttbuilder.solve.assign_teachers import StaffingError, apply_staffing
    prog = task["progress_path"]
    with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                     delete=False) as f:
        yaml.safe_dump(task["cfg"], f, sort_keys=False)
        path = f.name
    try:
        cfg = load_config(path)
    except ConfigLoadError as e:
        return {"problems": e.problems, "findings": [], "n_units": 0}
    problems: list[str] = []
    findings: list = []
    n_units = 0
    try:
        materialize_curriculum(cfg)
        _progress(prog, "Checking the arithmetic…")
        findings = [list(x) for x in _audit_findings(cfg)]
        blocking = [m for s, m in findings if s == "block"]
        if not blocking and any(not s.teachers and s.needs_teacher
                                for s in cfg.sections):
            _progress(prog, "Assigning teachers to classes…")
            apply_staffing(cfg)
            _progress(prog, "Re-checking with teachers in place…")
            findings = [list(x) for x in _audit_findings(cfg)]
        from ttbuilder.model.builder import build_instance
        n_units = len(build_instance(cfg).events)
    except (ConfigError, StaffingError) as e:
        problems = list(e.problems)
    return {"problems": problems, "findings": findings, "n_units": n_units}


def do_solve(task) -> dict:
    from ttbuilder.config.loader import load_config
    from ttbuilder.model.builder import (build_instance,
                                         materialize_curriculum)
    from ttbuilder.output.excel import generate_all
    from ttbuilder.output.scorecard import build_scorecard
    from ttbuilder.output.serialize import write_assignments
    from ttbuilder.solve.assign_teachers import apply_staffing
    from ttbuilder.solve.engine import diagnose, solve
    from ttbuilder.solve.rooms import assign_rooms
    from ttbuilder.validate.checker import validate

    prog = task["progress_path"]
    time_limit = task["time_limit"]
    with tempfile.TemporaryDirectory() as td:
        cfg_path = os.path.join(td, "school.yaml")
        yaml.safe_dump(task["cfg"], open(cfg_path, "w"), sort_keys=False)
        cfg = load_config(cfg_path)
        # single-worker everywhere in the web pipeline: the CP-SAT
        # multi-worker portfolio can deadlock pre-search in child-process
        # environments (TEST_LOG #24/#25); reliability beats speed here
        cfg.solver.workers = 1
        materialize_curriculum(cfg)
        if any(not s.teachers and s.needs_teacher for s in cfg.sections):
            _progress(prog, "Assigning teachers to classes…")
            apply_staffing(cfg)
        inst = build_instance(cfg)

        _progress(prog, f"Placing every lesson (up to "
                        f"{max(1, time_limit // 60)} minute(s) of "
                        "computing)…")
        res = solve(inst, hint=task.get("warm"), time_limit=time_limit)
        if res.status not in ("OPTIMAL", "FEASIBLE"):
            _progress(prog, "No valid timetable exists — working out why…")
            diag = diagnose(inst, time_limit=min(120, time_limit))
            return {"status": "INFEASIBLE", "core": diag.core}

        _progress(prog, "Choosing rooms…")
        rooms = assign_rooms(inst, res.schedule)
        out_dir = os.path.join(td, "out")
        os.makedirs(out_dir)
        asg = os.path.join(out_dir, "assignments.csv")
        write_assignments(inst, res.schedule, rooms, asg)
        resolved = os.path.join(out_dir, "resolved_config.yaml")
        yaml.safe_dump(cfg.model_dump(exclude_none=True),
                       open(resolved, "w"), sort_keys=False)

        _progress(prog, "Double-checking every lesson independently…")
        report = validate(resolved, asg, None)
        if not report.passed:
            return {"status": "ERROR", "detail": report.render()[:4000]}

        _progress(prog, "Building your workbooks and PDFs…")
        generate_all(resolved, asg, None, out_dir)
        build_scorecard(resolved, asg, out_dir)
        try:
            from ttbuilder.output.pdf import master_pdf, teacher_pdf
            master_pdf(resolved, asg,
                       os.path.join(out_dir, "master_timetable.pdf"))
            teacher_pdf(resolved, asg,
                        os.path.join(out_dir, "teacher_timetables.pdf"))
        except Exception:   # noqa: BLE001 — PDFs are nice-to-have
            pass
        json.dump(res.schedule,
                  open(os.path.join(out_dir, "solution.json"), "w"))
        sc = os.path.join(out_dir, "scorecard.md")
        scorecard_md = open(sc).read() if os.path.exists(sc) else ""

        import zipfile
        with zipfile.ZipFile(task["zip_path"], "w",
                             zipfile.ZIP_DEFLATED) as z:
            for name in sorted(os.listdir(out_dir)):
                z.write(os.path.join(out_dir, name), name)
        gap = ""
        if res.objective and res.bound is not None and res.objective > 0:
            g = 100 * (res.objective - res.bound) / res.objective
            if g < 0.5:
                gap = " Quality is essentially the best possible."
            elif g < 30:
                gap = f" Quality is within {g:.0f}% of the best possible."
            else:
                gap = (" This is a quick draft — build again with a longer "
                       "preset for a more polished version.")
        return {"status": "SOLVED",
                "detail": f"Status {res.status.lower()} in "
                          f"{res.wall_s:.0f}s.{gap}",
                "scorecard_md": scorecard_md,
                "validation_text": report.render(),
                "schedule": res.schedule}


def main():
    task = json.load(open(sys.argv[1]))
    try:
        if task["mode"] == "check":
            payload = {"kind": "done", **{"payload": do_check(task)}}
        else:
            payload = {"kind": "done", **{"payload": do_solve(task)}}
    except Exception as e:   # noqa: BLE001
        payload = {"kind": "fatal",
                   "payload": f"{type(e).__name__}: {e}"}
    _write_result(task["result_path"], payload)


if __name__ == "__main__":
    main()
