"""Record the TaskPilot demo to MP4.

Drives the real UI with Playwright (which records video natively), following
docs/demo-script.md. Output lands in submission/demo.webm; make_demo_video.sh
converts it to MP4 with ffmpeg.

    python submission/record_demo.py

Why scripted rather than screencast: a live recording of an agent run is
unreproducible — if the pause lands at a different step the video is wrong.
This pins the exact beats so the same command always yields the same video.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
SUB = ROOT / "submission"
OUT_DIR = SUB / "video-raw"
RAW = OUT_DIR

WEB = "http://localhost:3010"
API = "http://localhost:8010"
SHOP = "http://localhost:9100"

TASK = "Buy the cheapest USB-C cable on MockShop"


def reset_shop() -> None:
    """Start from a known-empty store so the run is deterministic."""
    try:
        import urllib.request

        urllib.request.urlopen(f"{SHOP}/admin/reset", timeout=10).read()
        print("mockshop reset")
    except Exception as exc:  # non-fatal: the run still works from any state
        print(f"reset failed ({exc}); continuing")


def hold(page, seconds: float, label: str) -> None:
    """Dead air, but purposeful — the script paces the read."""
    print(f"  hold {seconds:>4.1f}s  {label}")
    time.sleep(seconds)


def run() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    reset_shop()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            record_video_dir=str(OUT_DIR),
            record_video_size={"width": 1600, "height": 1000},
        )
        page = ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # --- 0:00 cold open — the problem ------------------------------------
        print("shot 1: landing")
        page.goto(WEB, wait_until="networkidle")
        hold(page, 4.0, "landing hero")

        # --- 0:20 run the task ----------------------------------------------
        print("shot 2: enter task")
        page.get_by_test_id("task-input").click()
        page.get_by_test_id("task-input").fill(TASK)
        hold(page, 1.5, "typed task")

        page.get_by_test_id("run-task").click()
        hold(page, 6.0, "run starting / planning")

        # --- 0:45 the pause (the money shot) --------------------------------
        print("shot 3: wait for approval gate")
        deadline = time.time() + 100
        while time.time() < deadline:
            card = page.get_by_test_id("approval-card")
            if card.count() and card.first.is_visible():
                break
            hold(page, 3.0, "agent working")

        # Scroll the card into frame. Without this the money shot records the
        # step timeline instead of the approval card, which defeats the point.
        try:
            page.get_by_test_id("approval-card").first.scroll_into_view_if_needed()
        except Exception:
            pass
        page.mouse.wheel(0, -120)  # a touch of headroom above the card
        hold(page, 9.0, "APPROVAL CARD — hold for the judge to read")
        page.screenshot(path=str(OUT_DIR / "shot-approval.png"))

        # --- 1:15 approve and finish -----------------------------------------
        print("shot 4: approve")
        try:
            page.get_by_test_id("approval-approve").first.scroll_into_view_if_needed()
        except Exception:
            pass
        approve = page.get_by_test_id("approval-approve")
        if approve.count():
            approve.first.click()
        else:
            print("  !! approve button not found")
        hold(page, 9.0, "executor resumes")

        # Let the run reach completion before we cut away.
        for _ in range(12):
            if page.get_by_test_id("completion-card").count():
                break
            hold(page, 2.0, "order placed / reading confirmation")
        try:
            page.get_by_test_id("completion-card").first.scroll_into_view_if_needed()
        except Exception:
            pass
        hold(page, 8.0, "completion card + stats")
        page.screenshot(path=str(OUT_DIR / "shot-complete.png"))

        # --- 1:45 the proof — the audit trail behind the approval -----------
        print("shot 5: audit trail")
        url = page.url
        run_id = url.rstrip("/").split("/")[-1] if "/" in url else ""
        if run_id:
            page.goto(f"{WEB}/audit/{run_id}", wait_until="networkidle")
            hold(page, 7.0, "immutable audit log")

        # --- 2:20 close -------------------------------------------------------
        print("shot 6: close")
        page.goto(WEB, wait_until="networkidle")
        hold(page, 8.0, "landing — Plan. Act. Pause. (For you.)")

        ctx.close()  # flushes the video file
        browser.close()

        print(f"\nconsole errors: {len(errors)}")
        for e in errors[:5]:
            print(f"  ! {e}")
        print(f"raw video -> {OUT_DIR}")
        return 0


def re_run() -> str:
    return "Run task|Start run|run task"


def re_approve() -> str:
    return "Approve|approve"


# --- Part 2: title cards + assembly -----------------------------------------
#
# The recorded screen capture runs ~65s (the agent is genuinely fast). A
# 3-minute submission needs breathing room for the narration, so the video is
# padded with four still cards that carry the script's spoken beats:
#   title card (8s) -> capture (~65s) -> results card (20s)
#   -> proof card (14s) -> closing card (10s)

CARDS = SUB / "cards"
FINAL = SUB / "demo.mp4"

_W, _H = 1600, 1000
_BG = (11, 11, 13)
_TEXT = (242, 242, 240)
_MUTED = (154, 154, 149)
_AMBER = (245, 165, 36)
_GREEN = (74, 217, 145)

_REG = "C:/Windows/Fonts/consola.ttf"
_BOLD = "C:/Windows/Fonts/consolab.ttf"


def card(title, lines, *, sub=None) -> pathlib.Path:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (_W, _H), _BG)
    d = ImageDraw.Draw(img)

    # Amber rule, same motif as the deck and the app header.
    d.rectangle([160, 150, 218, 157], fill=_AMBER)

    y = 330
    d.text((160, y), title, font=ImageFont.truetype(_BOLD, 74), fill=_TEXT)
    y += 108
    for line in lines:
        # A leading "!" marks a line that deserves the green accent (the zero).
        hot = line.startswith("!")
        d.text((160, y), line.lstrip("! "),
               font=ImageFont.truetype(_REG, 44),
               fill=_GREEN if hot else _TEXT)
        y += 66

    if sub:
        d.text((160, y + 34), sub, font=ImageFont.truetype(_REG, 30), fill=_MUTED)

    CARDS.mkdir(parents=True, exist_ok=True)
    out = CARDS / (title.replace(" ", "_").replace(":", "")[:24] + ".png")
    img.save(out)
    return out


def video_of(png: pathlib.Path, seconds: int) -> pathlib.Path:
    """Freeze the card for `seconds` and encode it as an H.264 clip."""
    out = png.with_suffix(".mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-loop", "1", "-t", str(seconds), "-i", str(png),
         "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        check=True, capture_output=True,
    )
    return out


def build_video() -> int:
    webms = sorted(RAW.glob("*.webm"))
    if not webms:
        print("no screen capture found — run record_demo.py first", file=sys.stderr)
        return 1

    # Durations are tuned so the total lands on ~3:00 for a 3-minute slot:
    # cards (~114s) + screen capture (~66s). Re-tune if the capture length shifts.
    cards = [
        video_of(card("TaskPilot", [
            "Plan. Act. Pause. (For you.)",
            "",
            "An agent that acts across real web apps —",
            "and stops for your approval before",
            "anything irreversible.",
        ], sub="Team NeuroNova"), 10),
        video_of(card("The numbers", [
            "15 / 15 tasks succeeded",
            "100% approval-gate correctness",
            "! 0 risky actions without approval",
            "42.8 min saved  (35.5x faster)",
        ], sub="15 eval tasks, scored against the demo store's own state."), 34),
        video_of(card("How we know", [
            "The harness checks MockShop's world state",
            "before and after every run — never the agent's",
            "own summary. A breach fails the run outright.",
        ], sub="Reproduce with:  make eval"), 34),
        video_of(card("TaskPilot", [
            "Plan. Act. Pause. (For you.)",
            "",
            "Anyone can build an agent that clicks a button.",
            "The hard part is one you can let near",
            "your credit card.",
        ], sub="Repo  ·  Live demo  ·  make eval"), 24),
    ]

    # Transcode the capture to a uniform format, then concatenate everything.
    cap = SUB / "_capture.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(webms[0]), "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-r", "30", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", str(cap)],
        check=True, capture_output=True,
    )

    listing = SUB / "_concat.txt"
    listing.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in [*cards, cap])
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c", "copy", "-movflags", "+faststart", str(FINAL)],
        check=True, capture_output=True,
    )

    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(FINAL)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    for tmp in (cap, listing):
        tmp.unlink(missing_ok=True)
    shutil.rmtree(CARDS, ignore_errors=True)

    print(f"wrote {FINAL}  ({float(dur):.1f}s = {int(float(dur)) // 60}:{int(float(dur)) % 60:02d}, "
          f"{FINAL.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    # `record_demo.py record` captures the screen; `build` assembles the MP4.
    # With no argument, do both in the order a full rebuild needs.
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    rc = 0
    if mode in ("record", "all"):
        rc = run()
    if rc == 0 and mode in ("build", "all"):
        rc = build_video()
    sys.exit(rc)