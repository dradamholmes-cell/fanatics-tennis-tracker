# Tennis Live Odds Tracker — Implementation Status

## Overview
Python script that monitors live tennis odds on Fanatics Sportsbook via scrcpy (tablet mirroring on Monitor 2). Uses OCR, fuzzy name matching, velocity tracking, and cross-book arbitrage to detect sharp money and alert on betting edges.

**Repo:** https://github.com/dradamholmes-cell/fanatics-tennis-tracker  
**Branch:** `claude/tennis-betting-tracker-yzsmqv` (active development)  
**Local copy:** `C:\Users\Admin\Desktop\300 LEONIDAS\tracker.py`

---

## What's Implemented ✅

### Core Loop
- **mss fullscreen capture** of Monitor #2 (no window borders, consistent coords)
- **Parallel row-strip OCR** with --psm 7, ThreadPoolExecutor (8 workers) — 3–4× faster than single-pass
- **Name cleanup** via regex (strip "1m", "ISI" prefixes) + rapidfuzz matching against ATP/WTA player list
- **Resistance detection** (ping-pong scroll) — flips direction when 2 consecutive frames match

### Baseline & Warmup
- **3-reading warm-up** before baseline locks in (requires ≤5pp variance)
- **Mid-match rejection** (implied > 85% or < 15%)
- **Persistence** to `baselines.json`

### Signal Detection
- **Velocity tracking** (pp/min, first derivative) — detects fast moves vs slow drift
- **NORMAL / WATCH / ATTACK tiers** based on delta + velocity thresholds
- **ATTACK lock-on** — pauses scroll for 30s when velocity > `ATTACK_VEL_PPM` (default 8.0)

### Alerts & Actions
- **Fullscreen red alert** when either player drops >= `DELTA_THRESHOLD_PCT` (default 12pp)
- **Alert clarity** — "BET ON: [opponent]" + cause/effect breakdown
- **Cross-book arbitrage** — checks The Odds API across 35 ATP/WTA sport keys in parallel
- **Cooldown** — prevents alert spam (20s default)

### Dashboard & Settings
- **Live match table** — shows score, odds, win%, drop, velocity, tier for all visible matches
- **Live sliders** — tune thresholds (delta, velocity, cooldown) during runtime
- **Keyboard shortcuts** — space (pause/resume), R (reset baselines), S (settings), Esc (dismiss alert)

### Hot-Match Fast Rescan
- **Dedicated background thread** rescans WATCH/ATTACK matches every 5s
- **Independent of main scroll loop** — finds developing moves faster
- **Skips during lock-on** — avoids double-OCR when main loop already rescans at ~1s
- **Dashboard indicator** — status bar shows "🔥 N hot" when matches are hot-tracked

---

## What's NOT Yet Implemented ❌

### Higher-Order Signals
- **Acceleration (2nd derivative)** — detect *increasing* velocity (panic vs steady drift)
- **Reversal detection** — 60→45→52 patterns (injury scare resolved, overreaction fade)
- **Score context** — "dropped 12pp while at 40-15" vs "dropped 12pp at 0-40" (very different alerts)
- **Confidence scoring** — composite of delta + velocity + score context + history length

### Advanced Features
- **Scan frequency prioritization** — top movers re-scanned more often than bottom 100
- **Break point panic detection** — OCR score → 0-40/15-40/30-40 patterns
- **PaddleOCR** — faster than Tesseract on sportsbook fonts (would need `pip install paddlepaddle paddleocr`)
- **Template matching for odds** — skip OCR on digit-only columns (fastest path)

### Market Features
- **Set Winner vs Match Winner divergence** — compare both markets
- **Injury timeout detection** — sudden multi-second pauses in play
- **Liquidity weighting** — shock score = (delta) × (speed) × (volume factor)

---

## User Configuration

### Startup
```bash
pip install -r requirements.txt
python tracker.py
```

Or double-click `run_tracker.bat` from Desktop.

### Key Settings (in tracker.py)
| Setting | Default | Purpose |
|---|---|---|
| `CAPTURE_MONITOR` | 2 | Which monitor to grab (1=primary, 2=second) |
| `DELTA_THRESHOLD_PCT` | 12.0 | pp drop needed for ALERT tier |
| `WATCH_VEL_PPM` | 3.0 | pp/min to enter WATCH tier |
| `ATTACK_VEL_PPM` | 8.0 | pp/min to enter ATTACK tier |
| `FAST_SCAN_INTERVAL_SEC` | 5.0 | Hot-scan thread rescans every N seconds |
| `ALERT_COOLDOWN_SEC` | 20 | Minimum seconds between alerts on same match |
| `OCR_ROW_HEIGHT_PX` | 55 | Strip height in 2× preprocessed image (tune if lines split) |
| `ODDS_API_KEY` | "" | Paste your The Odds API key for cross-book arb |

All can be tuned live via **Settings panel (S key)**.

---

## Known Limitations & Next Steps

### Immediate Wins
1. **Add acceleration detection** — track 2nd derivative of velocity to catch panic moves
2. **Score-based alert filtering** — don't alert on 40-15 drops the same way as 0-40 drops
3. **PaddleOCR** — faster, more accurate on sportsbook fonts than Tesseract

### Medium-Term
4. **Adaptive scan frequency** — top 10 movers every 5s, rest every 60s
5. **Reversal detection** — fade moves that quickly recover
6. **Liquidity weighting** — not all 12pp moves are equal (volume context matters)

### Testing
- Run with `SIMULATION_MODE = True` to test on synthetic data (no capture needed)
- Use Settings sliders to find your preferred velocity thresholds
- Baseline warm-up takes ~30s per new match — wait for "Locked" message before judging

---

## File Structure
```
fanatics-tennis-tracker/
├── tracker.py          (main script — all-in-one, 1500+ lines)
├── baselines.json      (auto-created, match baseline odds)
├── run_tracker.bat     (double-click to launch)
├── requirements.txt    (pip dependencies)
├── players/
│   └── players.txt     (ATP/WTA names for fuzzy matching)
├── roadmap.md          (feature wishlist)
└── CLAUDE.md           (this file)
```

---

## Development Notes

### Threading Model
- **Main thread:** tkinter dashboard (blocks on `mainloop()`)
- **Scan loop thread:** infinite loop calling `scan_once()` — captures, OCRs, parses, processes matches, scrolls
- **Hot scan thread:** checks WATCH/ATTACK matches every 5s (independent viewport)
- **Arb thread:** (per alert) fetches cross-book odds in background

### Global State
- `baselines` — dict of all match anchors (never cleared except manual reset)
- `_hot_matches` — set of match keys currently in WATCH or ATTACK tier (thread-safe)
- `last_alert_at` — cooldown tracking per match key
- `_scroll_dir` — 1 (down) or -1 (up), flips on resistance

### Key Functions
- `capture_frame()` — mss grab of Monitor #2 or pyautogui window fallback
- `ocr_image()` — parallel row-strip OCR with ThreadPoolExecutor
- `parse_matches()` — regex-based odds extraction from OCR text
- `process_matches()` — baseline logic, velocity calc, tier assignment, alert firing
- `_hot_scan_loop()` — background thread that rescans hot matches
- `scan_once()` — single iteration: capture → OCR → parse → process → scroll

---

## Last Updated
**June 2026** — Added hot-scan thread, fixed velocity bug (p2 collapses), 3–4× OCR speedup via parallel row strips.
