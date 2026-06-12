"""Command-line interface.

  python -m ttbuilder check    config.yaml
  python -m ttbuilder solve    config.yaml [--out DIR] [--time SECONDS]
                               [--enrolments CSV] [--fresh]
  python -m ttbuilder diagnose config.yaml [--time SECONDS]

`solve` runs the full pipeline: validate config -> static audit -> solve
(warm-started from the previous solution if present) -> assign rooms ->
write assignments.csv -> generate workbooks/PDFs/scorecard -> run the
independent validator. It refuses to emit outputs that fail validation.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _load(config_path: str):
    from .config.loader import ConfigLoadError, load_config
    from .model.builder import (ConfigError, build_instance,
                                materialize_curriculum)
    from .solve.assign_teachers import StaffingError, apply_staffing
    try:
        cfg = load_config(config_path)
    except ConfigLoadError as e:
        print("Your config file has problems:\n")
        for p in e.problems:
            print("  •", p)
        sys.exit(2)
    try:
        n_gen = materialize_curriculum(cfg)
        if n_gen:
            print(f"Curriculum mode: generated {n_gen} class groups from "
                  "year-group numbers.")
        if any(not s.teachers and s.needs_teacher for s in cfg.sections):
            assigned = apply_staffing(cfg)
            if assigned:
                print(f"Staffing stage: assigned teachers to "
                      f"{len(assigned)} class groups.")
        inst = build_instance(cfg)
    except (ConfigError, StaffingError) as e:
        print("Your config file has problems:\n")
        for p in e.problems:
            print("  •", p)
        sys.exit(2)
    for w in inst.warnings:
        print("  note:", w)
    return cfg, inst


def cmd_check(args):
    from .ingest.audit import run_audit
    _, inst = _load(args.config)
    print(f"Config OK: {len(inst.events)} scheduled units, "
          f"{len(inst.teachers)} teachers, {len(inst.slots)} teaching slots, "
          f"{sum(inst.room_type_capacity.values())} room-capacity.")
    findings = run_audit(inst)
    blocking = [m for s, m in findings if s == "block"]
    warns = [m for s, m in findings if s == "warn"]
    for m in blocking:
        print("\n✗", m)
    for m in warns:
        print("\n⚠", m)
    if blocking:
        print(f"\n{len(blocking)} problem(s) make this timetable impossible. "
              "Fix them before solving.")
        sys.exit(1)
    print(f"\nNo blocking problems found ({len(warns)} warnings). "
          "Ready to solve.")


def cmd_solve(args):
    from .ingest.audit import run_audit
    from .output.serialize import write_assignments
    from .solve.engine import solve
    from .solve.rooms import assign_rooms

    cfg, inst = _load(args.config)
    findings = run_audit(inst)
    blocking = [m for s, m in findings if s == "block"]
    if blocking:
        print("The timetable is impossible as configured:\n")
        for m in blocking:
            print("  ✗", m)
        print("\nFix these and re-run, or run `diagnose` for deeper analysis.")
        sys.exit(1)

    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    sol_path = os.path.join(out_dir, "solution.json")
    hint = None
    if not args.fresh and os.path.exists(sol_path):
        try:
            hint = json.load(open(sol_path))
            print("Warm-starting from the previous solution "
                  "(use --fresh to ignore it).")
        except Exception:  # noqa: BLE001
            hint = None

    print(f"Solving (time limit {args.time}s)...")
    res = solve(inst, hint=hint, time_limit=args.time)
    if res.status not in ("OPTIMAL", "FEASIBLE"):
        print(f"\nResult: {res.status} after {res.wall_s:.0f}s.")
        print("No valid timetable exists for this configuration.")
        print("Run `diagnose` to see which requirements conflict.")
        sys.exit(1)
    gap = ""
    if res.objective and res.bound is not None and res.objective > 0:
        gap = f", quality gap {100 * (res.objective - res.bound) / res.objective:.1f}%"
    print(f"Result: {res.status} in {res.wall_s:.0f}s "
          f"(quality cost {res.objective:.0f}{gap}).")
    json.dump(res.schedule, open(sol_path, "w"))

    print("Assigning rooms...")
    rooms = assign_rooms(inst, res.schedule)
    asg_path = os.path.join(out_dir, "assignments.csv")
    n = write_assignments(inst, res.schedule, rooms, asg_path)
    print(f"Wrote {asg_path} ({n} lesson-meetings).")

    # write the resolved config (curriculum expanded, staffing applied) —
    # the exact contract the schedule was built against
    import yaml as _yaml
    resolved_path = os.path.join(out_dir, "resolved_config.yaml")
    with open(resolved_path, "w") as f:
        _yaml.safe_dump(cfg.model_dump(exclude_none=True), f,
                        sort_keys=False, allow_unicode=True, width=120)

    # independent validation BEFORE generating human outputs
    from .validate.checker import validate
    report = validate(resolved_path, asg_path, args.enrolments)
    rep_path = os.path.join(out_dir, "validation_report.txt")
    with open(rep_path, "w") as f:
        f.write(report.render())
    if not report.passed:
        print("\n*** VALIDATION FAILED — outputs withheld. ***")
        print(report.render())
        sys.exit(1)
    print(f"Independent validation PASSED (report: {rep_path}).")

    print("Generating workbooks...")
    from .output.excel import generate_all
    from .output.scorecard import build_scorecard
    generate_all(args.config, asg_path, args.enrolments, out_dir)
    build_scorecard(args.config, asg_path, out_dir)
    try:
        from .output.pdf import master_pdf, teacher_pdf
        master_pdf(args.config, asg_path,
                   os.path.join(out_dir, "master_timetable.pdf"))
        teacher_pdf(args.config, asg_path,
                    os.path.join(out_dir, "teacher_timetables.pdf"))
    except Exception as e:  # noqa: BLE001
        print("  (PDF generation skipped:", e, ")")
    print(f"\nDone. Everything is in {out_dir}/ — start with "
          f"validation_report.txt and scorecard.md.")


def cmd_diagnose(args):
    from .solve.engine import diagnose
    _, inst = _load(args.config)
    print("Analysing which requirements conflict (this can take a while)...")
    res = diagnose(inst, time_limit=args.time)
    if res.status != "INFEASIBLE":
        print(f"Solver result: {res.status} — the configuration appears "
              "solvable; run `solve`.")
        return
    print("\nThe following requirements cannot all hold together:\n")
    for fam in res.core:
        kind, _, name = fam.partition(":")
        nice = {
            "teacher": "Teacher availability/clash rules for",
            "students": "Student clash protection for",
            "rooms": "Room capacity for type(s)",
            "load": "Maximum teaching load of",
            "periods": "Required lesson count of",
            "daycap": "Per-day limit of",
            "doubles": "Double-period policy of",
            "availability": "Joint availability of",
        }.get(kind, kind)
        print(f"  • {nice} {name}")
    print("\nRelax one of these (more rooms, higher load cap, fewer periods, "
          "or unpin commitments) and re-run `check`.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="timetable")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("check", cmd_check), ("solve", cmd_solve),
                     ("diagnose", cmd_diagnose)):
        sp = sub.add_parser(name)
        sp.add_argument("config")
        sp.set_defaults(fn=fn)
        if name in ("solve", "diagnose"):
            sp.add_argument("--time", type=int, default=None)
        if name == "solve":
            sp.add_argument("--out", default="outputs")
            sp.add_argument("--enrolments", default=None)
            sp.add_argument("--fresh", action="store_true")
    args = p.parse_args(argv)
    if getattr(args, "time", None) is None and args.cmd == "solve":
        args.time = None
    if args.cmd == "diagnose" and args.time is None:
        args.time = 120
    args.fn(args)


if __name__ == "__main__":
    main()
