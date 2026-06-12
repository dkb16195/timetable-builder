"""Printable PDF renderings of the timetable (reportlab).

master_pdf: landscape-A3 master grid (teachers × teaching periods) per week.
teacher_pdf: one landscape-A4 page per teacher, classic personal grid.
Reads only configs/*.yaml + assignments.csv via excel.load_context.
"""
from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from .excel import _short, assigned_per_week, load_context

TINY = ParagraphStyle("tiny", fontName="Helvetica", fontSize=3.6, leading=4.0)
TINY_B = ParagraphStyle("tinyb", fontName="Helvetica-Bold", fontSize=4.2,
                        leading=4.6)
TINY_I = ParagraphStyle("tinyi", fontName="Helvetica-Oblique", fontSize=3.4,
                        leading=3.8, textColor=colors.grey)
SMALL = ParagraphStyle("small", fontName="Helvetica", fontSize=6, leading=7)
SMALL_B = ParagraphStyle("smallb", fontName="Helvetica-Bold", fontSize=6.5,
                         leading=7.5)
SMALL_I = ParagraphStyle("smalli", fontName="Helvetica-Oblique", fontSize=5.5,
                         leading=6.5, textColor=colors.grey)
H1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=11, leading=13)

GRID_STYLE = TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.25, colors.Color(0.6, 0.6, 0.6)),
    ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.85, 0.89, 0.95)),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("LEFTPADDING", (0, 0), (-1, -1), 1),
    ("RIGHTPADDING", (0, 0), (-1, -1), 1),
    ("TOPPADDING", (0, 0), (-1, -1), 0.5),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5),
])


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\n", "<br/>"))


def master_pdf(config_path: str, assignments_path: str, out_path: str,
               teachers_per_page: int = 46) -> None:
    ctx = load_context(config_path, assignments_path)
    grid = ctx.grid
    slots = [(d, p) for d in grid.days for p in grid.teaching(d)]
    doc = SimpleDocTemplate(out_path, pagesize=landscape(A3),
                            leftMargin=12, rightMargin=12,
                            topMargin=14, bottomMargin=12)
    page_w = landscape(A3)[0] - 24
    name_w = 52.0
    col_w = (page_w - name_w) / len(slots)
    story = []
    first = True
    for week in grid.weeks:
        chunks = [ctx.teachers[i:i + teachers_per_page]
                  for i in range(0, len(ctx.teachers), teachers_per_page)]
        for ci, chunk in enumerate(chunks):
            if not first:
                story.append(PageBreak())
            first = False
            story.append(Paragraph(
                f"{_esc(ctx.title)} — Master Timetable — Week {week} "
                f"(page {ci + 1}/{len(chunks)})", H1))
            story.append(Spacer(0, 3))
            header = [Paragraph("Teacher", TINY_B)] + [
                Paragraph(f"{d[:3]}<br/>{p}", TINY_B) for d, p in slots]
            data = [header]
            for t in chunk:
                tid = t["id"]
                tmeet = ctx.by_teacher.get(tid, {})
                tpin = ctx.pinned.get(tid, {})
                row = [Paragraph(_esc(t["name"]), TINY_B)]
                for d, p in slots:
                    slot = (week, d, p)
                    ms = tmeet.get(slot)
                    if ms:
                        txt = "<br/>".join(
                            f"{_esc(m.section)}<br/>{_esc(m.room)}"
                            for m in ms)
                        row.append(Paragraph(txt, TINY))
                    elif slot in tpin:
                        row.append(Paragraph(_esc(_short(tpin[slot], 24)),
                                             TINY_I))
                    else:
                        row.append("")
                data.append(row)
            tbl = Table(data, colWidths=[name_w] + [col_w] * len(slots),
                        repeatRows=1)
            tbl.setStyle(GRID_STYLE)
            story.append(tbl)
    doc.build(story)


def teacher_pdf(config_path: str, assignments_path: str, out_path: str) -> None:
    ctx = load_context(config_path, assignments_path)
    grid = ctx.grid
    doc = SimpleDocTemplate(out_path, pagesize=landscape(A4),
                            leftMargin=18, rightMargin=18,
                            topMargin=18, bottomMargin=14)
    page_w = landscape(A4)[0] - 36
    label_w = 60.0
    cols = [(w, d) for w in grid.weeks for d in grid.days]
    col_w = (page_w - label_w) / len(cols)
    story = []
    for ti, t in enumerate(ctx.teachers):
        if ti:
            story.append(PageBreak())
        tid = t["id"]
        mx = t.get("max_per_week", ctx.tier_max.get(t.get("tier"), 0))
        story.append(Paragraph(f"{_esc(ctx.title)} — {_esc(t['name'])} ({tid})",
                               H1))
        story.append(Paragraph(
            f"tier: {t.get('tier', '?')} — assigned "
            f"{assigned_per_week(ctx, tid):g}/wk of max {mx}", SMALL))
        story.append(Spacer(0, 4))
        header = [Paragraph("Period", SMALL_B)] + [
            Paragraph(f"{w} {d[:3]}", SMALL_B) for w, d in cols]
        data = [header]
        shade = []
        tmeet = ctx.by_teacher.get(tid, {})
        tpin = ctx.pinned.get(tid, {})
        for ri, pid in enumerate(grid.period_order, 1):
            st, en = grid.times.get((grid.days[0], pid), ("", ""))
            row = [Paragraph(f"<b>{pid}</b><br/>{st}–{en}", SMALL)]
            for ci, (w, d) in enumerate(cols, 1):
                if pid not in grid.day_periods[d]:
                    row.append("")
                    shade.append(("BACKGROUND", (ci, ri), (ci, ri),
                                  colors.Color(0.75, 0.75, 0.75)))
                    continue
                if grid.kind[pid] != "teaching":
                    row.append("")
                    shade.append(("BACKGROUND", (ci, ri), (ci, ri),
                                  colors.Color(0.9, 0.9, 0.9)))
                    continue
                slot = (w, d, pid)
                ms = tmeet.get(slot)
                if ms:
                    txt = "<br/>".join(
                        f"<b>{_esc(m.subject)}</b><br/>{_esc(m.section)}"
                        f"<br/>{_esc(m.room)}" for m in ms)
                    row.append(Paragraph(txt, SMALL))
                elif slot in tpin:
                    row.append(Paragraph(_esc(_short(tpin[slot], 40)),
                                         SMALL_I))
                else:
                    row.append("")
            data.append(row)
        tbl = Table(data, colWidths=[label_w] + [col_w] * len(cols),
                    repeatRows=1)
        tbl.setStyle(GRID_STYLE)
        for cmd in shade:
            tbl.setStyle(TableStyle([cmd]))
        story.append(tbl)
    doc.build(story)
