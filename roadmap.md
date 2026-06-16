# Fanatics Tennis Tracker — Improvement Roadmap

> Repo: https://github.com/dradamholmes-cell/fanatics-tennis-tracker
> Dashboard: https://dradamholmes-cell.github.io/fanatics-tennis-tracker/
> Local script: tracker.py (runs on Windows 10, 8GB RAM)

---

## Current State (2026-06-16)

- `tracker.py` — Python OCR scanner on scrcpy ZB30 window
- pytesseract OCR with CLAHE contrast + 2x upscale
- Baseline anchored on first sighting per match
- 12pp implied probability drop = beep alert
- pyautogui mouse-wheel scroll through ~112 live matches
- tkinter dashboard with Pause/Resume scroll button
- `index.html` — GitHub Pages web dashboard pulling The Odds API

**Known issues:**
- Names garbled by flag icons (e.g. `1mAlexdeMinaur`, `ISIGabrielDiallo`)
- Baseline anchors on first sighting — can be mid-match garbage
- No velocity tracking — slow 12pp drift treated same as instant 12pp crash
- OCR runs on whole frame — slow (~1s/frame on this machine)

---

## Priority 1 — OCR Accuracy (Do First)

### 1a. Dedicated Monitor Capture
**Status:** [ ] Not started

Switch from window capture to full Monitor 2 capture using `mss` (faster, no letterbox, consistent coords).

```python
pip install mss

import mss
import numpy as np

with mss.mss() as sct:
    monitor = sct.monitors[2]   # Monitor 2 = tablet display
    img = np.array(sct.grab(monitor))
```

Setup:
- Put scrcpy fullscreen on Monitor 2
- Lock tablet to portrait orientation
- Disable Android animations (Developer Options → set all animation scales to 0)

**Expected gain:** Consistent pixel coords every scan, no crop guesswork.

---

### 1b. Fix Garbled Player Names
**Status:** [x] Done - 2026-06-16

**Root cause:** Flag emoji (🇦🇺) OCRs as random chars like `1m`, `ISI`, `SF`.

**Fix A — Regex strip (quick win):**
```python
name = re.sub(r'^[A-Z0-9]{1,4}', '', name)
# ISIGabrielDiallo → GabrielDiallo
# 1mAlexdeMinaur  → AlexdeMinaur
```

**Fix B — Crop flag column (better):**
After switching to Monitor 2 capture, skip the left ~12% of each row before OCR. The flag column is fixed position.

**Fix C — Fuzzy match against player list (best):**
```python
pip install rapidfuzz

from rapidfuzz import process
# Match raw OCR output against ATP/WTA player name list
result = process.extractOne("1mAlexdeMinaur", known_players, score_cutoff=80)
# → ("Alex de Minaur", 96)
```

Player list source: ATP/WTA rankings pages (~500 active players covers 95%+ of matches).

**Expected gain:** ~90% of name garbage eliminated.

---

## Priority 2 — Smarter Signals

### 2a. Velocity Tracking
**Status:** [ ] Not started

Current: alert fires when cumulative drop from baseline > 12pp. No sense of speed.

Add per-match history: store (timestamp, prob) pairs. Compute pp/minute.

```python
# In baselines[key], add:
"history": [(time.time(), prob)]

# On each update, append and compute:
if len(history) >= 2:
    dt_min = (history[-1][0] - history[-2][0]) / 60
    dp     = history[-2][1] - history[-1][1]
    velocity = dp / dt_min   # pp per minute
```

Alert threshold ideas:
- Cumulative drop > 12pp → standard alert
- Velocity > 8pp/min → fast move alert (sharps reacting)
- Velocity > 15pp/min → panic alert

---

### 2b. Symmetry Check (Reduce False Alerts)
**Status:** [ ] Not started

If p1 drops 12pp but p2 doesn't rise ~12pp, it's probably an OCR misread, not a real move.
Only fire alert when both sides of the book agree.

```python
# Both must move in consistent direction
if d1 >= DELTA_THRESHOLD and (prob1 + prob2) > 95:   # total still makes sense
    fire_alert(...)
```

---

### 2c. Momentum Acceleration
**Status:** [ ] Not started

Boring drift: 60 → 58 → 56 → 54 (linear, likely score-driven)
Panic move:   60 → 57 → 52 → 44 (accelerating, sharp money)

Track second derivative of implied probability. Flag when acceleration > threshold.

---

### 2d. Reversal Detection
**Status:** [ ] Not started

Pattern: 60% → 45% → 52%

Often means: overreaction, injury scare resolved, break point survived.
These can be faded (bet back the recovering player).

Flag when prob recovers > 5pp after a drop.

---

### 2e. Break Point Suppression
**Status:** [ ] Not started

Odds overreact at 0-40 / 15-40 / 30-40 and then snap back when the game resolves.
If score field shows a break-point score, delay alerts by one scan.

---

## Priority 3 — Baseline Quality

### 3a. Reject Bad Baselines
**Status:** [ ] Not started

Don't anchor baseline if either player is above 85% or below 15% implied.
That's a match that's already decided.

```python
if prob1 > 85 or prob2 > 85 or prob1 < 15 or prob2 < 15:
    baselines[key]["quality"] = "MID_MATCH"
    # store but don't alert from it
```

---

### 3b. Warm-Up Baseline
**Status:** [ ] Not started

Don't anchor on first sighting. Wait for 3 consecutive scans with variance < 4pp, then lock in.

```python
"candidate_history": [prob1, prob1b, prob1c]
# if max - min < 4: anchor as baseline
```

---

### 3c. Persist Baselines to Disk
**Status:** [ ] Not started

Script restart currently wipes all baselines. Save to JSON on each update.

```python
import json

def save_baselines():
    with open("baselines.json", "w") as f:
        json.dump(baselines, f)

def load_baselines():
    global baselines
    try:
        with open("baselines.json") as f:
            baselines = json.load(f)
    except FileNotFoundError:
        pass
```

---

## Priority 4 — Alert Actionability

### 4a. Single-Glance Alert Card
**Status:** [ ] Not started

Replace current alert output with one card:

```
╔══════════════════════════════════╗
║  🔥 LIVE EDGE — BET NOW          ║
║                                  ║
║  BET:      Muchova               ║
║  Odds:     -405                  ║
║                                  ║
║  Why:  Opponent dropped 18pp     ║
║        Was: 67%  Now: 48%        ║
║        Speed: 9pp/min (FAST)     ║
╚══════════════════════════════════╝
```

Rule: the bet is always on the OPPONENT of the player whose odds drifted.

---

### 4b. Priority Ranking
**Status:** [ ] Not started

Not all 12pp alerts are equal. Score each alert:

```python
score = delta_pp * velocity_multiplier
# velocity_multiplier: 1.0 normal, 1.5 fast, 2.0 panic
```

Show ranked alert queue in dashboard.

---

## Priority 5 — Performance

### 5a. Row-Strip OCR
**Status:** [ ] Not started

Instead of OCR-ing the full frame with `--psm 6`:
1. Detect row boundaries (horizontal bands of consistent color)
2. Slice each row (~40px tall strip)
3. Run `--psm 7` (single line) on each strip in parallel threads

Estimated speedup: 3-4x per frame.

---

### 5b. Odds-Only OCR for Known Matches
**Status:** [ ] Not started

Once a match is in baselines, skip re-reading the name.
Only OCR the rightmost ~15% of the row (odds column).
Names don't change mid-match.

---

### 5c. PaddleOCR (If Needed)
**Status:** [ ] Not started

If Tesseract accuracy remains a problem after image preprocessing fixes:
PaddleOCR is faster and more accurate on sportsbook fonts than Tesseract.
RAM footprint is acceptable on 8GB.

```
pip install paddlepaddle paddleocr
```

Test before committing — may not be needed after Priority 1 fixes.

---

## Priority 6 — Cross-Book Arbitrage

### 6a. Fanatics vs DraftKings Gap Alerts
**Status:** [ ] Not started

Dashboard already pulls DraftKings + FanDuel via The Odds API.
If Fanatics live odds diverge > 3% implied from DraftKings on same player, that's a real arb gap.

This is the highest-value signal in the whole system.

Wire: when tracker.py fires a 12pp alert, also query The Odds API for that player and show DraftKings/FanDuel current line.

---

## Priority 7 — Scan Frequency Ranking

### 7a. Hot/Cold Match Tiers
**Status:** [ ] Not started

Instead of spending equal time scanning all 112 matches:

- **Hot tier** (largest recent move or velocity): scan every pass
- **Warm tier** (some movement): scan every 2nd pass  
- **Cold tier** (stable for 5+ passes): scan every 5th pass

This effectively multiplies useful signal rate without any hardware changes.

---

## File Structure

```
fanatics-tennis-tracker/          ← GitHub repo
  index.html                      ← Web dashboard (GitHub Pages)
  tracker.py                      ← Main OCR scanner (run locally)
  baselines.json                  ← Persisted baselines (gitignored)
  players/
    atp_players.txt               ← Known ATP player names for fuzzy match
    wta_players.txt               ← Known WTA player names for fuzzy match
  roadmap.md                      ← This file
  .gitignore
```

---

## Change Log

| Date | Change |
|---|---|
| 2026-06-16 | Initial working version: OCR scan, scroll, beep alerts, tkinter dashboard |
| 2026-06-16 | Switched scroll from ADB to pyautogui mouse-wheel (WM_MOUSEWHEEL via ctypes) |
| 2026-06-16 | Added Pause/Resume scroll button to dashboard |
| 2026-06-16 | Roadmap created, tracker.py pushed to GitHub |
