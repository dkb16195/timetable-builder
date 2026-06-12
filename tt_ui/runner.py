"""Engine orchestration for the web app.

All engine work runs in a child process (see engine_worker.py) with a
hard wall-clock kill, so the app stays responsive even if a solver
misbehaves. Engine messages are translated into (message, which step
fixes it, suggestion); the app never shows a stack trace to a user.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

_PKG_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


@dataclass
class Finding:
    severity: str            # "block" | "warn"
    message: str
    step: int                # wizard step that fixes it
    suggestion: str = ""


@dataclass
class CheckResult:
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""
    n_units: int = 0


@dataclass
class SolveOutcome:
    status: str                       # SOLVED | INFEASIBLE | ERROR
    headline: str = ""
    detail: str = ""
    zip_bytes: bytes | None = None
    scorecard_md: str = ""
    validation_text: str = ""
    schedule: dict | None = None
    causes: list[str] = field(default_factory=list)


STALL_MSG = ("The engine stalled on this school — that usually means the "
             "requirements are a very long way over capacity (for example "
             "one subject given far more lessons than the week holds). "
             "Re-check the lesson totals in Step 5; if everything looks "
             "right, save your school file from the sidebar and send it "
             "to support.")


def _step_for(msg: str) -> tuple[int, str]:
    """Map an engine message to (wizard step, suggestion)."""
    m = msg.lower()
    if "room type" in m or "room of that type" in m or "lab" in m \
            or "room-periods" in m or "rooms at the same time" in m:
        return 2, ("Add a room of the kind mentioned in Step 2, or reduce "
                   "that subject's lessons in Step 5.")
    if "subject" in m and "not defined" in m:
        return 3, "Add the missing subject in Step 3."
    if "teacher" in m and ("qualified" in m or "cover all classes" in m):
        return 4, ("In Step 4, add this subject to another teacher, raise "
                   "a maximum load, or add a teacher.")
    if "maximum is" in m or "available for only" in m or "jointly" in m \
            or "load" in m:
        return 4, ("In Step 4: raise that teacher's role/load, remove a "
                   "day off, or share their classes with a colleague.")
    if "periods per cycle but the grid" in m or "teaching slots" in m:
        return 5, ("In Step 5, reduce that year group's lessons until "
                   "they fit the week (the live counter shows the total).")
    if "block" in m:
        return 6, "Adjust the block's classes or lessons in Step 6."
    if "unavailable slot" in m or "does not exist in the grid" in m:
        return 4, ("A day-off or unavailability refers to a day/period "
                   "your week no longer has — re-save Step 4 after "
                   "changing the week shape.")
    return 7, ""


def _run_worker(mode: str, cfg_dict: dict, time_limit: int,
                warm, zip_path: str, timeout_s: int, progress):
    """Run the engine child process; stream its progress lines; hard-kill
    on overrun. Returns ('done', payload) / ('fatal', msg) / ('stall', None).
    A plain subprocess (not multiprocessing) so it works identically under
    Streamlit, pytest and any other host."""
    td = tempfile.mkdtemp(prefix="ttb_")
    task_path = os.path.join(td, "task.json")
    progress_path = os.path.join(td, "progress.txt")
    result_path = os.path.join(td, "result.json")
    open(progress_path, "w").close()
    json.dump({"mode": mode, "cfg": cfg_dict, "time_limit": time_limit,
               "warm": warm, "zip_path": zip_path,
               "progress_path": progress_path,
               "result_path": result_path}, open(task_path, "w"))
    env = dict(os.environ)
    env["PYTHONPATH"] = _PKG_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "tt_ui.engine_child", task_path],
        cwd=_PKG_ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + timeout_s
    seen = 0
    try:
        while time.time() < deadline:
            try:
                lines = open(progress_path).read().splitlines()
                for line in lines[seen:]:
                    progress(line)
                seen = len(lines)
            except OSError:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
            return ("stall", None)
        if os.path.exists(result_path):
            data = json.load(open(result_path))
            return (data["kind"], data["payload"])
        err = (proc.stderr.read() or "")[-800:]
        return ("fatal", f"the engine process ended unexpectedly. {err}")
    finally:
        for p in (task_path, progress_path, result_path):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(td)
        except OSError:
            pass


def run_check(cfg_dict: dict, progress=lambda m: None) -> CheckResult:
    kind, payload = _run_worker("check", cfg_dict, 0, None, "",
                                timeout_s=180, progress=progress)
    if kind == "stall":
        return CheckResult(False, [Finding("block", STALL_MSG, 5)],
                           "The check could not finish.")
    if kind == "fatal":
        return CheckResult(False, [Finding(
            "block", "Something unexpected went wrong during the check. "
            f"Technical detail for support: {payload}", 7)],
            "The check hit an unexpected error.")
    findings: list[Finding] = []
    for p in payload["problems"]:
        step, sug = _step_for(p)
        findings.append(Finding("block", p, step, sug))
    for sev, msg in payload["findings"]:
        step, sug = _step_for(msg)
        findings.append(Finding(sev, msg, step, sug))
    blocking = [f for f in findings if f.severity == "block"]
    ok = not blocking
    summary = (f"Everything adds up: {payload['n_units']} groups of "
               "lessons to place. Ready to build."
               if ok else
               f"{len(blocking)} problem(s) make this timetable "
               "impossible as described. Each one below says where to "
               "fix it.")
    return CheckResult(ok, findings, summary, payload["n_units"])


def run_solve(cfg_dict: dict, time_limit: int,
              warm: dict | None, progress) -> SolveOutcome:
    fd, zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        kind, payload = _run_worker(
            "solve", cfg_dict, time_limit, warm, zip_path,
            timeout_s=time_limit + 420, progress=progress)
        if kind == "stall":
            return SolveOutcome("ERROR", "The build ran far over its "
                                "time budget and was stopped.", STALL_MSG)
        if kind == "fatal":
            return SolveOutcome(
                "ERROR", "The builder hit an unexpected error.",
                f"Your school file is safe — download it from the sidebar "
                f"and send it to support. Technical detail: {payload}")
        status = payload["status"]
        if status == "INFEASIBLE":
            return SolveOutcome(
                "INFEASIBLE",
                "No valid timetable exists for this school as described.",
                "That is a fact about the requirements, not a glitch: "
                "some of them cannot all hold at once.",
                causes=_friendly_causes(payload.get("core", [])))
        if status == "ERROR":
            return SolveOutcome(
                "ERROR", "The independent check found a problem with the "
                "generated timetable, so no files were produced.",
                payload.get("detail", ""))
        zip_bytes = open(zip_path, "rb").read()
        return SolveOutcome(
            "SOLVED",
            "Your timetable is ready — built, room-matched and "
            "independently verified clash-free.",
            payload["detail"], zip_bytes=zip_bytes,
            scorecard_md=payload["scorecard_md"],
            validation_text=payload["validation_text"],
            schedule=payload["schedule"])
    finally:
        try:
            os.unlink(zip_path)
        except OSError:
            pass


def _friendly_causes(core: list[str]) -> list[str]:
    nice = {
        "teacher": "The availability or no-clash rules for teacher",
        "students": "Keeping pupils un-clashed in",
        "rooms": "The number of rooms of kind",
        "load": "The maximum teaching load of",
        "periods": "The required number of lessons for",
        "daycap": "The lessons-per-day limit of",
        "doubles": "The double-period rule of",
    }
    out = []
    for fam in core[:30]:
        kind, _, name = fam.partition(":")
        out.append(f"{nice.get(kind, kind)} **{name}**")
    seen, dedup = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            dedup.append(x)
    return dedup
