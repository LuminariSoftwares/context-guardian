"""tools/make_dsh_demo_gif.py (run: pip install pillow; python tools/make_dsh_demo_gif.py; writes the GIF in the cwd; needs the DejaVu fonts at F) -- render the DSH-engine demo GIF for the context-guardian README.

Frames are computed from the STEPS table below (no hand drawing). Mechanics shown match
dsh-context-guardian 0.1.0-alpha.9: compaction at compactThreshold 0.85 of the window,
LLM summary first / deterministic checkpoint as fallback, span archive with seq pointers,
pins first in every checkpoint, stale recall adds ONE '[context-guardian recall]' message.
The results strip is the 2026-10-04 benchmark from CHANGELOG.md (real numbers).
"""
from PIL import Image, ImageDraw, ImageFont

W, H = 900, 470
F = "/usr/share/fonts/truetype/dejavu/"
font = lambda n, s: ImageFont.truetype(F + n, s)
TITLE, SUB, BODY, SMALL, MONO, BIG = (font("DejaVuSans-Bold.ttf", 26), font("DejaVuSans.ttf", 14),
                                      font("DejaVuSans.ttf", 13), font("DejaVuSans.ttf", 12),
                                      font("DejaVuSansMono.ttf", 12), font("DejaVuSans.ttf", 15))
BG, PANEL, TRACK, FG, MUTED = "#0d1117", "#161b22", "#21262d", "#e6edf3", "#8b949e"
GREEN, AMBER, RED, BLUE, VIOLET = "#3fb950", "#d29922", "#f85149", "#58a6ff", "#bc8cff"
WINDOW, THRESH = 32768, 0.85

# (pressure 0..1, chip text, chip colour, caption, [event lines], hold ms)
STEPS = []
def step(p, chip, col, cap, events, ms=110):
    STEPS.append((p, chip, col, cap, events, ms))

ev0 = [("turn 3", "You: \"Important, keep this for later: HERON = vault-7, KITE = 0412 ...\"", FG),
       ("turn 3", "pinned 12 facts verbatim (they lead every checkpoint)", VIOLET)]
for i, p in enumerate([0.04, 0.08, 0.12, 0.16]):
    step(p, "working", MUTED, "The session fills its window turn by turn.", ev0[: 1 + (i >= 2)])
ev1 = ev0 + [("turn 9-40", "reads 40 documents; the context meter climbs", FG)]
for p in [0.24, 0.34, 0.44, 0.54, 0.64, 0.72, 0.79, 0.84]:
    step(p, "working", MUTED, "The session fills its window turn by turn.", ev1)
step(0.86, "compacting...", AMBER, "At 85 % of the window, compaction runs before the model can overflow.", ev1, 600)
ev2 = ev1[1:] + [("compact", "LLM summary first; if it fails or cannot fit: deterministic checkpoint, no model call", AMBER),
                 ("archive", "older turns saved on disk as span 0001 (seq 1-212), recall-able", BLUE)]
for p in [0.74, 0.58, 0.44, 0.36]:
    step(p, "compacting...", AMBER, "At 85 % of the window, compaction runs before the model can overflow.", ev2, 140)
ev3 = [("checkpoint", "Pinned (12) | Decisions | Files | To-dos  -- pins first, every time", VIOLET),
       ("archive", "span 0001 on disk; every checkpoint line carries a seq pointer", BLUE)]
step(0.36, "compacted", GREEN, "The session keeps going, with the pinned facts still in front of the model.", ev3, 1100)
ev4 = ev3[1:] + [("turn 47", "You: \"What was the code on doc01 L017?\"  (only the archive still has it)", FG)]
for p in [0.40, 0.45, 0.50]:
    step(p, "working", MUTED, "Later, a question needs a detail that was compacted away.", ev4, 160)
ev5 = ev4 + [("recall", "[context-guardian recall] adds ONE message with up to 3 matching original lines", GREEN)]
step(0.51, "stale recall", GREEN, "Stale recall puts the original line back before the step runs.", ev5, 1300)
ev6 = ev4[1:] + [("recall", "[context-guardian recall] adds ONE message with up to 3 matching original lines", GREEN),
                 ("answer", "model answers from the original line -- no guess, no invented value", GREEN)]
step(0.53, "answered", GREEN, "Stale recall puts the original line back before the step runs.", ev6, 3600)

def draw(p, chip, col, cap, events, final):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((40, 26), "Context Guardian", font=TITLE, fill=FG)
    d.text((40 + d.textlength("Context Guardian", font=TITLE) + 12, 36), "inside DeepSeek Harness", font=SUB, fill=MUTED)
    cw = d.textlength(chip, font=SMALL) + 24
    d.rounded_rectangle((W - 40 - cw, 30, W - 40, 56), 12, fill=PANEL)
    d.text((W - 40 - cw + 12, 36), chip, font=SMALL, fill=col)
    d.text((40, 76), cap, font=BODY, fill=MUTED)
    # context meter
    x0, x1, y0, y1 = 40, W - 40, 128, 156
    tok = int(p * WINDOW)
    d.text((40, 104), f"context {tok:,} / {WINDOW:,} tok  ({p * 100:.0f} %)", font=BIG, fill=FG)
    d.rounded_rectangle((x0, y0, x1, y1), 6, fill=TRACK)
    fill = GREEN if p < 0.70 else (AMBER if p < THRESH else RED)
    if p > 0:
        d.rounded_rectangle((x0, y0, x0 + (x1 - x0) * min(p, 1), y1), 6, fill=fill)
    tx = x0 + (x1 - x0) * THRESH
    d.line((tx, y0 - 8, tx, y1 + 8), fill=AMBER, width=3)
    d.text((tx - d.textlength("compacts at 85 %", font=SMALL) - 8, y1 + 10), "compacts at 85 %", font=SMALL, fill=AMBER)
    d.text((x0, y1 + 10), "0", font=SMALL, fill=MUTED)
    d.text((x1 - d.textlength("100 %", font=SMALL), y1 + 10), "100 %", font=SMALL, fill=MUTED)
    # event log
    d.rounded_rectangle((40, 200, W - 40, 200 + 30 + 24 * 4), 8, fill=PANEL)
    d.text((56, 208), "session log", font=SMALL, fill=MUTED)
    for i, (who, text, c) in enumerate(events[-4:]):
        y = 232 + 24 * i
        d.text((56, y), who, font=MONO, fill=MUTED)
        d.text((150, y), text, font=SMALL, fill=c)
    # results strip (real benchmark numbers, 2026-10-04)
    yb = 372
    if final:
        d.text((40, yb), "Benchmark: 48-prompt session, 12 facts, codenames changed mid-session, qwen3-coder:30b",
               font=SMALL, fill=MUTED)
        cols = [40, 330, 520, 690]
        rows = [(("with stale recall (alpha.9)", "12/12 current", "0 stale", "early facts 3/3"), GREEN),
                (("pins only (alpha.8)", "12/12, 9/12", "0, 3 stale", "early facts 0/3"), MUTED)]
        for r, (cells, c) in enumerate(rows):
            for x, t in zip(cols, cells):
                d.text((x, yb + 24 + 22 * r), t, font=BODY, fill=c)
    else:
        d.text((40, yb + 24), "illustration of one session; the benchmark result appears at the end", font=SMALL, fill=MUTED)
    return im

frames, durs = [], []
for i, (p, chip, col, cap, ev, ms) in enumerate(STEPS):
    frames.append(draw(p, chip, col, cap, ev, final=(i == len(STEPS) - 1)))
    durs.append(ms)
pal = [f.convert("P", palette=Image.ADAPTIVE, colors=64) for f in frames]
pal[0].save("context_guardian_dsh_demo.gif", save_all=True, append_images=pal[1:], duration=durs, loop=0, optimize=True)
print(len(frames), "frames")
