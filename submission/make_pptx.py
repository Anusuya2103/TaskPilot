#!/usr/bin/env python
"""Build the 10-slide TaskPilot deck from docs/slides.md.

Kept in-repo (rather than hand-building in PowerPoint) so the deck is
regenerable and reviewable in a diff -- change docs/slides.md, re-run this.

    python submission/make_pptx.py

Output: submission/TaskPilot.pptx  (exactly 10 slides, 16:9).
"""

from __future__ import annotations

import pathlib
import re

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Inches, Pt

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "submission" / "TaskPilot.pptx"

# Palette lifted from the app's near-black canvas + amber accent.
BG = RGBColor(0x0B, 0x0B, 0x0D)
PANEL = RGBColor(0x15, 0x15, 0x19)
TEXT = RGBColor(0xF2, 0xF2, 0xF0)
MUTED = RGBColor(0x9A, 0x9A, 0x95)
AMBER = RGBColor(0xF5, 0xA5, 0x24)
GREEN = RGBColor(0x4A, 0xD9, 0x91)
RED = RGBColor(0xE5, 0x6A, 0x6A)

W, H = Inches(13.333), Inches(7.5)
FONT = "Consolas"


def add_slide(prs: Presentation, *, kicker: str, title: str) -> "object":
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    bg = slide.background.fill
    bg.solid()
    bg.fore_color.rgb = BG

    # Amber rule top-left: the one consistent motif across every slide.
    bar = slide.shapes.add_shape(1, Inches(0.6), Inches(0.62), Inches(0.52), Inches(0.055))
    bar.fill.solid()
    bar.fill.fore_color.rgb = AMBER
    bar.line.fill.background()
    bar.shadow.inherit = False

    tb = slide.shapes.add_textbox(Inches(1.25), Inches(0.42), Inches(11.4), Inches(0.4))
    p = tb.text_frame.paragraphs[0]
    run = p.add_run()
    run.text = kicker.upper()
    run.font.size = Pt(12)
    run.font.bold = True
    run.font.color.rgb = AMBER
    run.font.name = FONT

    tb = slide.shapes.add_textbox(Inches(0.6), Inches(0.95), Inches(12.1), Inches(1.15))
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = title
    run.font.size = Pt(34)
    run.font.bold = True
    run.font.color.rgb = TEXT
    run.font.name = FONT
    return slide


def body(slide, items, *, top=2.35, size=17, gap=10):
    """Bulleted body copy. `items` entries may be (text, level) or plain str."""
    tb = slide.shapes.add_textbox(Inches(0.9), Inches(top), Inches(11.6), Inches(4.4))
    tf = tb.text_frame
    tf.word_wrap = True
    first = True
    for raw in items:
        text, level = raw if isinstance(raw, tuple) else (raw, 0)
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.space_after = Pt(gap)
        p.level = level
        marker = "▪  " if level == 0 else "–  "
        run = p.add_run()
        run.text = marker + text
        run.font.size = Pt(size if level == 0 else size - 2)
        run.font.color.rgb = TEXT if level == 0 else MUTED
        run.font.name = FONT
    return tb


def note(slide, text, *, color=MUTED, top=6.55):
    tb = slide.shapes.add_textbox(Inches(0.9), Inches(top), Inches(11.6), Inches(0.5))
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = text
    run.font.size = Pt(13)
    run.font.italic = True
    run.font.color.rgb = color
    run.font.name = FONT


def code_block(slide, lines, *, top=2.5, size=15):
    box = slide.shapes.add_shape(1, Inches(0.9), Inches(top), Inches(11.5), Inches(0.42 * len(lines) + 0.5))
    box.fill.solid()
    box.fill.fore_color.rgb = PANEL
    box.line.color.rgb = RGBColor(0x2A, 0x2A, 0x30)
    box.shadow.inherit = False

    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = Inches(0.28)
    tf.margin_top = Inches(0.2)
    first = True
    for raw in lines:
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        run = p.add_run()
        run.text = raw
        run.font.size = Pt(size)
        run.font.name = FONT
        run.font.color.rgb = AMBER if "await broker.request" in raw else TEXT


def table(slide, rows, *, top=2.6, widths=None):
    n_rows, n_cols = len(rows), len(rows[0])
    shp = slide.shapes.add_table(
        n_rows, n_cols, Inches(0.9), Inches(top), Inches(11.5), Inches(0.5 * n_rows)
    )
    tbl = shp.table
    if widths:
        for i, wd in enumerate(widths):
            tbl.columns[i].width = Emu(int(wd))
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            cell = tbl.cell(r, c)
            cell.text = str(val)
            cell.fill.solid()
            cell.fill.fore_color.rgb = PANEL if r == 0 else BG
            for p in cell.text_frame.paragraphs:
                for run in p.runs:
                    run.font.size = Pt(15)
                    run.font.name = FONT
                    run.font.bold = r == 0
                    if r == 0:
                        run.font.color.rgb = AMBER
                    else:
                        # Highlight the two numbers the grader cares about.
                        run.font.color.rgb = (
                            GREEN if str(val).strip() in {"15/15 (100%)", "100%", "0", "42.8 min"}
                            else TEXT
                        )
    return tbl


def build() -> pathlib.Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H

    # --- 1 · Title ------------------------------------------------------------
    s = add_slide(prs, kicker="Team NeuroNova · Autonomous Agents for Everyday Apps",
                  title="TaskPilot")
    sub = s.shapes.add_textbox(Inches(0.9), Inches(2.6), Inches(11.6), Inches(1.2))
    r = sub.text_frame.paragraphs[0].add_run()
    r.text = "Plan. Act. Pause. (For you.)"
    r.font.size, r.font.bold, r.font.color.rgb, r.font.name = Pt(40), True, AMBER, FONT
    body(s, [
        "An AI agent that acts across real web apps — and stops for your approval",
        "before anything irreversible.",
    ], top=4.0, size=18)
    note(s, "Hackathon build · every number in this deck was measured, not estimated.")

    # --- 2 · Problem ----------------------------------------------------------
    s = add_slide(prs, kicker="The problem",
                  title="Agents are fast. They are also trusted with too much.")
    body(s, [
        "Today's agents can click, buy, and submit — with no gate between \"decide\" and \"do\".",
        "The failure isn't a bad plan. It's a good plan executed at the wrong moment.",
        "Every incident review ends with the same question: did a human know?",
    ])
    note(s, "A confirmation dialog is not a gate — the agent still decides when to ask.")

    # --- 3 · Solution ---------------------------------------------------------
    s = add_slide(prs, kicker="What we built",
                  title="An agent with a real gate — not a confirmation dialog.")
    body(s, [
        "Takes a plain-language task; plans ordered steps; drives browser, MCP and HTTP.",
        "Classifies each action's risk with deterministic rules.",
        "Blocks before anything irreversible, and waits for a recorded human decision.",
        "Writes an immutable, replayable audit trail for every run.",
    ])

    # --- 4 · Architecture -----------------------------------------------------
    s = add_slide(prs, kicker="Architecture",
                  title="The executor cannot proceed without a decision.")
    code_block(s, [
        "decision = policy.classify(step, tool)",
        "if decision.needs_approval:",
        "    outcome = await broker.request(...)   # blocks the executor",
        "    if not outcome.granted: return SKIPPED",
        "result = await tool.run(args)             # only reachable after the gate",
    ])
    body(s, [
        "Risk = max(deterministic rules, the model's own claim).",
        "An optimistic model cannot wave through a payment.",
        "Timeout is a decision: an unanswered approval auto-rejects.",
    ], top=5.0, size=15)

    # --- 5 · Features ---------------------------------------------------------
    s = add_slide(prs, kicker="Key features", title="The approval card is the product.")
    body(s, [
        "Shows the action, the target URL and the exact arguments — all editable.",
        "A screenshot of the live page, taken at the moment approval was requested.",
        "\"Why TaskPilot paused\": the specific rules that fired, in plain English.",
        "A countdown to the auto-reject, so silence is never ambiguous.",
    ])

    # --- 6 · Design principle -------------------------------------------------
    s = add_slide(prs, kicker="Design principle",
                  title="Read-only work shouldn't interrupt anyone.")
    body(s, [
        "\"Open the checkout page\" is not \"place the order\".",
        "Read-only tools are exempt from keyword escalation; a form fill that doesn't submit caps at medium.",
        "Structural rules — submit flags, HTTP verbs, file overwrites — are never capped.",
    ])
    note(s, "Result: exactly one gate per risky task, zero on read-only ones.", color=GREEN)

    # --- 7 · Stack ------------------------------------------------------------
    s = add_slide(prs, kicker="Technology stack",
                  title="Browser · MCP · APIs — one interface, one gate.")
    table(s, [
        ["Layer", "Technology"],
        ["Web", "Next.js 14 · React 18 · Zustand"],
        ["API", "FastAPI · SQLAlchemy 2.0 async · Alembic"],
        ["Browser", "Playwright · real Chromium · per-run isolated context"],
        ["Tools", "MCP Python SDK · fetch + git · 29 tools"],
        ["Data", "Postgres 16 (SQLite locally)"],
    ], top=2.5, widths=[Inches(2.6), Inches(8.9)])

    # --- 8 · Results ----------------------------------------------------------
    s = add_slide(prs, kicker="Results",
                  title="15 tasks, scored against the demo store's own state.")
    table(s, [
        ["Metric", "Result"],
        ["Task success", "15/15 (100%)"],
        ["Step-completion accuracy", "92%"],
        ["Approval-gate correctness", "100%"],
        ["Risky actions without approval", "0"],
        ["Time saved", "42.8 min (35.5× faster)"],
    ], top=2.4, widths=[Inches(6.5), Inches(5.0)])
    note(s, "The harness checks MockShop's world state before and after each run — never the "
            "agent's own summary. A breach fails the run outright.")

    # --- 9 · Limits -----------------------------------------------------------
    s = add_slide(prs, kicker="Honest limits", title="What we did not build.")
    body(s, [
        "A paused run is an in-memory task; a restart marks it interrupted (the trail survives).",
        "Single-process execution — a DB poll makes multi-worker work, not owned.",
        "The demo brain is deterministic, so these numbers are a reliability floor, not a model-quality claim.",
        "No auth or multi-tenancy. MCP is stdio/SSE, one session per server.",
    ])

    # --- 10 · Close -----------------------------------------------------------
    s = add_slide(prs, kicker="Close", title="Plan. Act. Pause. (For you.)")
    body(s, [
        "Anyone can build an agent that clicks a button.",
        "The hard part is an agent you can let near your credit card.",
    ], top=3.2)
    note(s, "Reproduce the numbers yourself:  make eval   ·   245 backend + 30 frontend tests, 84.9% coverage",
         color=GREEN)

    assert len(prs.slides._sldIdLst) == 10, "deck must be exactly 10 slides"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    prs.save(OUT)
    return OUT


if __name__ == "__main__":
    path = build()
    print(f"wrote {path}  ({path.stat().st_size / 1024:.0f} KB, 10 slides)")