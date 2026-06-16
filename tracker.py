# =============================================================================
#  TENNIS LIVE ODDS SCANNER v3  --  ZB30 / Scrcpy Edition
#  Scroll   : pyautogui mouse-wheel (no ADB/USB required) with ADB fallback
#  UI       : tkinter overlay — plain-English match table + alert panel
# =============================================================================

import re
import os
import time
import threading
import subprocess
import winsound
from datetime import datetime
from collections import defaultdict

try:
    from rapidfuzz import process as fuzz_process
    FUZZ_AVAILABLE = True
except ImportError:
    FUZZ_AVAILABLE = False

import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

try:
    import pygetwindow as gw
    import pyautogui
    pyautogui.FAILSAFE = False   # don't abort if mouse hits corner
    CAPTURE_AVAILABLE = True
except ImportError:
    CAPTURE_AVAILABLE = False

try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

try:
    import tkinter as tk
    from tkinter import font as tkfont
    TK_AVAILABLE = True
except ImportError:
    TK_AVAILABLE = False


# =============================================================================
#  USER CONFIGURATION
# =============================================================================

SIMULATION_MODE      = False
WINDOW_TITLE         = "ZB30"

SCAN_INTERVAL_SEC    = 0
DELTA_THRESHOLD_PCT  = 12.0
ALERT_BEEP_FREQ      = 1200
ALERT_BEEP_DURATION  = 600
ALERT_COOLDOWN_SEC   = 20
BASELINE_MAX_IMPLIED = 85.0   # reject baseline if either player is above this %

# ── Mouse-wheel scroll (primary — no ADB needed) ──────────────────────────────
SCROLL_CLICKS        = -10     # negative = scroll down; tune if list moves too much/little
SCROLL_SETTLE_SEC    = 0.2

# ── ADB scroll (fallback if USB debugging works) ──────────────────────────────
USE_ADB_SCROLL       = False   # set True if you want to try ADB instead
SWIPE_X              = 500
SWIPE_FROM_Y         = 900
SWIPE_TO_Y           = 300
SWIPE_DURATION_MS    = 220
LIVE_TAB_X           = 500
LIVE_TAB_Y           = 960

MATCHES_PER_VIEW     = 7
TOTAL_MATCHES_EST    = 112
SWIPES_PER_PASS      = (TOTAL_MATCHES_EST // MATCHES_PER_VIEW) + 4


# =============================================================================
#  GLOBAL STATE
# =============================================================================

baselines        = {}
last_alert_at    = defaultdict(float)
scroll_step      = 0
_adb_ok          = None
scrolling_enabled = True      # toggled by the UI button

RESET  = "\033[0m"
BOLD   = "\033[1m"
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
GREY   = "\033[90m"


# =============================================================================
#  PLAYER LIST  (for fuzzy name matching)
# =============================================================================

def _load_players() -> list[str]:
    candidates = [
        os.path.join(os.path.dirname(__file__), "players", "players.txt"),
        r"C:\Users\Admin\AppData\Local\Temp\fanatics-repo\players\players.txt",
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return [l.strip() for l in f if l.strip()]
    return []

KNOWN_PLAYERS = _load_players()
if KNOWN_PLAYERS:
    print(f"{GREEN}[NAMES] Loaded {len(KNOWN_PLAYERS)} known players for fuzzy match.{RESET}")
else:
    print(f"{YELLOW}[NAMES] No players.txt found — fuzzy match disabled.{RESET}")


def clean_name(raw: str) -> str:
    """Strip OCR flag-icon garbage then fuzzy-match against known player list."""
    # Remove leading junk: 1-4 chars of digits/uppercase that aren't a real name prefix
    name = re.sub(r'^[\d]+[a-z]*\s*', '', raw)       # e.g. "1m" prefix
    name = re.sub(r'^[A-Z]{2,4}(?=[A-Z][a-z])', '', name)  # e.g. "ISI" prefix
    name = name.strip()

    if not FUZZ_AVAILABLE or not KNOWN_PLAYERS or len(name) < 3:
        return name

    # Insert spaces before capital letters to help matching (AlexdeMinaur → Alex de Minaur)
    spaced = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)

    match, score, _ = fuzz_process.extractOne(
        spaced, KNOWN_PLAYERS,
        score_cutoff=72   # minimum confidence to accept a match
    ) or (None, 0, None)

    if match:
        return match
    return spaced   # return space-inserted version even if no match found


# =============================================================================
#  TKINTER DASHBOARD
# =============================================================================

class Dashboard:
    BG       = "#0e1423"
    PANEL    = "#111827"
    HEADER   = "#1e2d4a"
    GREEN_C  = "#22c55e"
    YELLOW_C = "#facc15"
    RED_C    = "#ef4444"
    WHITE    = "#f1f5f9"
    GREY_C   = "#64748b"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Tennis Odds Tracker")
        self.root.configure(bg=self.BG)
        self.root.attributes("-topmost", True)
        self.root.geometry("860x600+20+20")
        self.root.resizable(True, True)
        self._row_widgets  = {}
        self._alert_window = None
        self._build_ui()
        self._bind_keys()

    # ── Keyboard shortcuts ───────────────────────────────────────────────────

    def _bind_keys(self):
        self.root.bind("<space>",          lambda e: self._toggle_scroll())
        self.root.bind("<Escape>",         lambda e: self._dismiss_alert())
        self.root.bind("<r>",              lambda e: self._reset_baselines())
        self.root.bind("<R>",              lambda e: self._reset_baselines())
        self.root.bind("<s>",              lambda e: self._open_settings())
        self.root.bind("<S>",              lambda e: self._open_settings())

    # ── UI build ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        r = self.root

        # Title bar
        bar = tk.Frame(r, bg=self.HEADER, pady=6)
        bar.pack(fill="x")
        tk.Label(bar, text="🎾  Tennis Live Odds Tracker",
                 bg=self.HEADER, fg=self.WHITE,
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=12)

        tk.Button(bar, text="⚙ Settings  [S]",
                  bg="#2d4a6e", fg=self.WHITE,
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  padx=8, command=self._open_settings
                  ).pack(side="right", padx=6, pady=4)

        tk.Button(bar, text="🔄 Reset Baselines  [R]",
                  bg="#374151", fg=self.WHITE,
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  padx=8, command=self._reset_baselines
                  ).pack(side="right", padx=6, pady=4)

        self.btn_scroll = tk.Button(
            bar, text="⏸ Pause  [Space]",
            bg=self.YELLOW_C, fg="#000",
            font=("Segoe UI", 9, "bold"), relief="flat",
            padx=8, command=self._toggle_scroll)
        self.btn_scroll.pack(side="right", padx=6, pady=4)

        self.lbl_status = tk.Label(bar, text="Starting…",
                                   bg=self.HEADER, fg=self.GREY_C,
                                   font=("Segoe UI", 9))
        self.lbl_status.pack(side="right", padx=10)

        # Shortcut hint bar
        hints = tk.Frame(r, bg="#0a0f1e", pady=2)
        hints.pack(fill="x")
        tk.Label(hints,
                 text="  Space = pause/resume   |   R = reset baselines   |   S = settings   |   Esc = dismiss alert",
                 bg="#0a0f1e", fg="#334155",
                 font=("Segoe UI", 8)).pack(side="left")

        # Column headers
        hdr = tk.Frame(r, bg=self.PANEL, pady=4)
        hdr.pack(fill="x", padx=6, pady=(6, 0))
        for text, w, anchor in [
            ("Match",        36, "w"),
            ("Score",         8, "center"),
            ("Live odds",    13, "center"),
            ("Win chance",   12, "center"),
            ("vs Baseline",  14, "center"),
            ("Signal",       10, "center"),
        ]:
            tk.Label(hdr, text=text, bg=self.PANEL, fg=self.GREY_C,
                     font=("Segoe UI", 9, "bold"),
                     width=w, anchor=anchor).pack(side="left")

        # Scrollable match list
        container = tk.Frame(r, bg=self.BG)
        container.pack(fill="both", expand=True, padx=6, pady=4)
        canvas = tk.Canvas(container, bg=self.BG, highlightthickness=0)
        sb = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.match_frame = tk.Frame(canvas, bg=self.BG)
        self.match_frame.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.match_frame, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # Footer
        foot = tk.Frame(r, bg=self.HEADER, pady=4)
        foot.pack(fill="x", side="bottom")
        tk.Label(foot,
                 text="🟢 Normal   🟡 Watch (>6pp)   🔴 BET ALERT   ⚫ Mid-match / bad baseline",
                 bg=self.HEADER, fg=self.GREY_C,
                 font=("Segoe UI", 9)).pack(side="left", padx=12)
        self.lbl_count = tk.Label(foot, text="", bg=self.HEADER, fg=self.WHITE,
                                  font=("Segoe UI", 9, "bold"))
        self.lbl_count.pack(side="right", padx=12)

    # ── Fullscreen alert overlay ─────────────────────────────────────────────

    def _do_alert(self, player, match_key, delta, live_prob, base_prob,
                  live_odds, opponent, opp_odds):
        self._dismiss_alert()   # close any existing alert first

        w = tk.Toplevel(self.root)
        w.attributes("-fullscreen", True)
        w.attributes("-topmost", True)
        w.configure(bg="#8b0000")
        self._alert_window = w

        # Dismiss on any key or click
        w.bind("<Key>",      lambda e: self._dismiss_alert())
        w.bind("<Button-1>", lambda e: self._dismiss_alert())
        w.focus_set()

        # Layout
        tk.Label(w, text="🚨  BET ALERT  🚨",
                 bg="#8b0000", fg="white",
                 font=("Segoe UI", 48, "bold")).pack(pady=(80, 20))

        tk.Label(w, text=f"BET ON:   {opponent}",
                 bg="#8b0000", fg="#ffff00",
                 font=("Segoe UI", 56, "bold")).pack(pady=10)

        tk.Label(w, text=f"CURRENT ODDS:   {'+' if opp_odds > 0 else ''}{opp_odds}",
                 bg="#8b0000", fg="white",
                 font=("Segoe UI", 40)).pack(pady=6)

        tk.Frame(w, bg="#cc0000", height=3).pack(fill="x", pady=20, padx=80)

        detail = (f"Why:  {player} dropped {delta:.0f}pp\n"
                  f"Was {base_prob:.0f}% to win  →  Now {live_prob:.0f}%\n"
                  f"Match:  {match_key.replace('|', ' vs ')}")
        tk.Label(w, text=detail,
                 bg="#8b0000", fg="#fca5a5",
                 font=("Segoe UI", 22),
                 justify="center").pack(pady=10)

        tk.Label(w, text="Press any key or click to dismiss",
                 bg="#8b0000", fg="#7f1d1d",
                 font=("Segoe UI", 14)).pack(pady=(40, 0))

        # Auto-dismiss after 45s
        w.after(45000, self._dismiss_alert)

    def _dismiss_alert(self):
        if self._alert_window:
            try:
                self._alert_window.destroy()
            except Exception:
                pass
            self._alert_window = None

    # ── Settings panel ───────────────────────────────────────────────────────

    def _open_settings(self):
        # Only open one settings window at a time
        if hasattr(self, "_settings_win") and self._settings_win.winfo_exists():
            self._settings_win.lift()
            return

        sw = tk.Toplevel(self.root)
        sw.title("Settings")
        sw.configure(bg=self.BG)
        sw.geometry("460x420+860+20")
        sw.attributes("-topmost", True)
        sw.resizable(False, False)
        self._settings_win = sw

        def section(text):
            tk.Label(sw, text=text, bg=self.BG, fg=self.GREY_C,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=20, pady=(14, 2))

        def slider_row(parent, label, var, from_, to_, fmt, on_change):
            row = tk.Frame(parent, bg=self.BG)
            row.pack(fill="x", padx=20, pady=3)
            tk.Label(row, text=label, bg=self.BG, fg=self.WHITE,
                     font=("Segoe UI", 10), width=26, anchor="w").pack(side="left")
            val_lbl = tk.Label(row, text=fmt(var.get()), bg=self.BG,
                               fg=self.YELLOW_C, font=("Segoe UI", 10, "bold"), width=8)
            val_lbl.pack(side="right")
            def _update(v):
                val_lbl.config(text=fmt(float(v)))
                on_change(float(v))
            tk.Scale(row, variable=var, from_=from_, to=to_,
                     orient="horizontal", bg=self.BG, fg=self.WHITE,
                     highlightthickness=0, troughcolor="#1e3a5f",
                     activebackground=self.YELLOW_C, showvalue=False,
                     command=_update, length=180).pack(side="right", padx=6)

        tk.Label(sw, text="⚙  Settings", bg=self.BG, fg=self.WHITE,
                 font=("Segoe UI", 14, "bold")).pack(pady=(16, 4))

        # ── Alert thresholds ──────────────────────────────────────────────────
        section("ALERT THRESHOLDS")

        self._sv_threshold = tk.DoubleVar(value=DELTA_THRESHOLD_PCT)
        slider_row(sw, "Alert threshold (pp drop)",
                   self._sv_threshold, 5, 30, lambda v: f"{v:.0f} pp",
                   lambda v: self._set("DELTA_THRESHOLD_PCT", v))

        self._sv_baseline_max = tk.DoubleVar(value=BASELINE_MAX_IMPLIED)
        slider_row(sw, "Reject baseline if prob >",
                   self._sv_baseline_max, 60, 95, lambda v: f"{v:.0f}%",
                   lambda v: self._set("BASELINE_MAX_IMPLIED", v))

        self._sv_cooldown = tk.DoubleVar(value=ALERT_COOLDOWN_SEC)
        slider_row(sw, "Alert cooldown (seconds)",
                   self._sv_cooldown, 10, 120, lambda v: f"{v:.0f}s",
                   lambda v: self._set("ALERT_COOLDOWN_SEC", v))

        # ── Scroll ────────────────────────────────────────────────────────────
        section("SCROLL")

        self._sv_scroll = tk.DoubleVar(value=abs(SCROLL_CLICKS))
        slider_row(sw, "Scroll distance per step",
                   self._sv_scroll, 2, 20, lambda v: f"{v:.0f} clicks",
                   lambda v: self._set("SCROLL_CLICKS", -int(v)))

        self._sv_settle = tk.DoubleVar(value=SCROLL_SETTLE_SEC * 1000)
        slider_row(sw, "Scroll settle delay (ms)",
                   self._sv_settle, 100, 800, lambda v: f"{v:.0f} ms",
                   lambda v: self._set("SCROLL_SETTLE_SEC", v / 1000))

        # ── Info ──────────────────────────────────────────────────────────────
        section("KEYBOARD SHORTCUTS")
        shortcuts = [
            ("Space",  "Pause / Resume scroll"),
            ("R",      "Reset all baselines"),
            ("S",      "Open this settings panel"),
            ("Esc",    "Dismiss alert overlay"),
        ]
        for key, desc in shortcuts:
            row = tk.Frame(sw, bg=self.BG)
            row.pack(fill="x", padx=20, pady=1)
            tk.Label(row, text=key, bg="#1e3a5f", fg=self.YELLOW_C,
                     font=("Courier", 10, "bold"), width=7,
                     relief="flat", pady=2).pack(side="left", padx=(0, 10))
            tk.Label(row, text=desc, bg=self.BG, fg=self.WHITE,
                     font=("Segoe UI", 10)).pack(side="left")

    def _set(self, name, value):
        """Update a global config variable live."""
        globals()[name] = value

    # ── Scroll toggle ─────────────────────────────────────────────────────────

    def _toggle_scroll(self):
        global scrolling_enabled
        scrolling_enabled = not scrolling_enabled
        if scrolling_enabled:
            self.btn_scroll.config(text="⏸ Pause  [Space]", bg=self.YELLOW_C, fg="#000")
        else:
            self.btn_scroll.config(text="▶ Resume  [Space]", bg=self.GREEN_C, fg="#000")

    # ── Reset baselines ───────────────────────────────────────────────────────

    def _reset_baselines(self):
        baselines.clear()
        last_alert_at.clear()
        self.lbl_status.config(text="Baselines RESET")
        print(f"{YELLOW}[RESET] All baselines cleared.{RESET}")

    # ── Match table update ────────────────────────────────────────────────────

    def update_matches(self, match_rows: list[dict], status: str):
        self.root.after(0, self._do_update, match_rows, status)

    def show_alert(self, player: str, match_key: str,
                   delta: float, live_prob: float, base_prob: float,
                   live_odds: int, opponent: str, opp_odds: int):
        self.root.after(0, self._do_alert, player, match_key,
                        delta, live_prob, base_prob, live_odds, opponent, opp_odds)

    def _do_update(self, match_rows, status):
        ts = datetime.now().strftime("%H:%M:%S")
        self.lbl_status.config(text=f"Last scan: {ts}  |  {status}")
        self.lbl_count.config(
            text=f"{len(baselines)} baselines  |  {len(match_rows)} on screen")

        current_keys = {r["key"] for r in match_rows}
        for k in list(self._row_widgets.keys()):
            if k not in current_keys:
                for w in self._row_widgets.pop(k):
                    try: w.destroy()
                    except Exception: pass

        for idx, m in enumerate(match_rows):
            key   = m["key"]
            d1, d2 = m["d1"], m["d2"]
            worst  = max(d1, d2)
            mid    = m.get("mid_match", False)

            if mid:
                row_bg, sig_text, sig_color = "#1a1a1a", "⚫ Mid", self.GREY_C
            elif worst >= DELTA_THRESHOLD_PCT:
                row_bg, sig_text, sig_color = "#2d0a0a", "🔴 BET", self.RED_C
            elif worst >= DELTA_THRESHOLD_PCT * 0.5:
                row_bg, sig_text, sig_color = "#1f1a00", "🟡 Watch", self.YELLOW_C
            else:
                row_bg = self.PANEL if idx % 2 == 0 else self.BG
                sig_text, sig_color = "🟢", self.GREEN_C

            def fmt_odds(o): return f"+{o}" if o > 0 else str(o)

            prob_str  = f"{m['p1_prob']:.0f}% / {m['p2_prob']:.0f}%"
            delta_str = f"▼{worst:.1f}pp" if worst > 0.5 else "stable"
            delta_col = (self.RED_C if worst >= DELTA_THRESHOLD_PCT
                         else self.YELLOW_C if worst >= 6
                         else self.GREEN_C)
            cells = [
                (f"{m['p1']} vs {m['p2']}", 36, "w",      self.WHITE),
                (m.get("sets", "?-?"),        8, "center", self.GREY_C),
                (f"{fmt_odds(m['p1_odds'])} / {fmt_odds(m['p2_odds'])}",
                                             13, "center", self.WHITE),
                (prob_str,                   12, "center", self.WHITE),
                (delta_str,                  14, "center", delta_col),
                (sig_text,                   10, "center", sig_color),
            ]

            if key in self._row_widgets:
                widgets = self._row_widgets[key]
                frame = widgets[0]
                frame.configure(bg=row_bg)
                for label, (text, _, _, fg) in zip(widgets[1:], cells):
                    label.config(text=text, fg=fg, bg=row_bg)
            else:
                frame = tk.Frame(self.match_frame, bg=row_bg, pady=3)
                frame.pack(fill="x", padx=2, pady=1)
                widgets = [frame]
                for text, w, anchor, fg in cells:
                    lbl = tk.Label(frame, text=text, bg=row_bg, fg=fg,
                                   font=("Segoe UI", 10),
                                   width=w, anchor=anchor)
                    lbl.pack(side="left")
                    widgets.append(lbl)
                self._row_widgets[key] = widgets

    def run(self):
        self.root.mainloop()


# Singleton
_dash: Dashboard | None = None


def start_dashboard():
    global _dash
    if not TK_AVAILABLE:
        return
    _dash = Dashboard()


# =============================================================================
#  SCROLL HELPERS
# =============================================================================

def _get_window():
    if not CAPTURE_AVAILABLE:
        return None
    wins = [w for w in gw.getAllWindows()
            if WINDOW_TITLE.lower() in w.title.lower() and w.width > 20]
    return wins[0] if wins else None


def _adb_available() -> bool:
    global _adb_ok
    if _adb_ok is not None:
        return _adb_ok
    result = subprocess.run("adb devices", shell=True, capture_output=True, text=True)
    _adb_ok = "\tdevice" in result.stdout
    return _adb_ok


import ctypes

WM_MOUSEWHEEL = 0x020A
_user32 = ctypes.windll.user32


def _post_scroll(hwnd: int, notches: int):
    """Send WM_MOUSEWHEEL directly to a window handle — no focus needed.
    notches > 0 = scroll up, notches < 0 = scroll down."""
    delta = notches * 120          # Windows WHEEL_DELTA = 120 per notch
    wparam = (delta & 0xFFFF) << 16
    _user32.PostMessageW(hwnd, WM_MOUSEWHEEL, wparam, 0)


def scroll_down():
    if not scrolling_enabled:
        return
    if USE_ADB_SCROLL and _adb_available():
        subprocess.run(
            f'adb shell input swipe {SWIPE_X} {SWIPE_FROM_Y} {SWIPE_X} {SWIPE_TO_Y} {SWIPE_DURATION_MS}',
            shell=True, capture_output=True, timeout=5)
    else:
        win = _get_window()
        if win:
            cx = win.left + win.width  // 2
            cy = win.top  + win.height // 2
            pyautogui.moveTo(cx, cy, duration=0)
            _post_scroll(win._hWnd, SCROLL_CLICKS)
    time.sleep(SCROLL_SETTLE_SEC)


def reset_to_top():
    if not scrolling_enabled:
        return
    print(f"{CYAN}[SCROLL] Pass complete — resetting to top…{RESET}")
    if USE_ADB_SCROLL and _adb_available():
        subprocess.run(
            f'adb shell input tap {LIVE_TAB_X} {LIVE_TAB_Y}',
            shell=True, capture_output=True, timeout=5)
        time.sleep(1.2)
    else:
        win = _get_window()
        if win:
            for _ in range(30):
                _post_scroll(win._hWnd, 10)   # scroll up
        time.sleep(0.8)


# =============================================================================
#  ODDS MATH
# =============================================================================

def american_to_implied(odds: int) -> float:
    if odds < 0:
        return (-odds) / (-odds + 100) * 100
    return 100 / (odds + 100) * 100


def implied_to_american(prob: float) -> str:
    if prob <= 0 or prob >= 100:
        return "N/A"
    if prob >= 50:
        return str(round(-prob / (100 - prob) * 100))
    return f"+{round((100 - prob) / prob * 100)}"


def vig_pct(p1: float, p2: float) -> float:
    return p1 + p2 - 100.0


# =============================================================================
#  SIMULATION
# =============================================================================

SIM_MATCHES = [
    ("C. Alcaraz",  "N. Djokovic",  1, 0, 4, 1,  -320,  +260),
    ("J. Sinner",   "A. Zverev",    0, 1, 2, 5,  +180,  -215),
    ("T. Fritz",    "C. Ruud",      1, 1, 3, 3,  -140,  +120),
    ("L. Musetti",  "F. Tiafoe",    1, 0, 5, 4,  -580,  +440),
]
SIM_DRIFT = {}


def _init_drift():
    for m in SIM_MATCHES:
        k = f"{m[0]}|{m[1]}"
        if k not in SIM_DRIFT:
            SIM_DRIFT[k] = 0


def generate_sim_image() -> Image.Image:
    _init_drift()
    W, H = 520, 480
    img  = Image.new("RGB", (W, H), color=(14, 20, 35))
    draw = ImageDraw.Draw(img)
    try:
        fnt   = ImageFont.truetype("consola.ttf", 14)
        fnt_s = ImageFont.truetype("consola.ttf", 11)
    except OSError:
        fnt = fnt_s = ImageFont.load_default()

    draw.rectangle([0, 0, W, 34], fill=(20, 30, 52))
    draw.text((12, 9), "Live  Tennis  Main", fill=(232, 49, 15), font=fnt)
    y = 44
    for i, m in enumerate(SIM_MATCHES):
        p1, p2, sp1, sp2, gp1, gp2, o1, o2 = m
        k  = f"{p1}|{p2}"
        d  = SIM_DRIFT.get(k, 0)
        lo1, lo2 = o1 + d, o2 - d
        bg = (22, 33, 58) if i % 2 == 0 else (18, 27, 48)
        draw.rectangle([6, y, W-6, y+84], fill=bg)
        draw.text((14, y+4),  "ATP Halle", fill=(70, 90, 130), font=fnt_s)
        draw.text((14, y+20), f"{p1:<18}", fill=(240, 240, 245), font=fnt)
        draw.text((14, y+40), f"{p2:<18}", fill=(200, 205, 215), font=fnt)
        draw.text((200, y+20), f"{sp1} {sp2} {gp1} 0", fill=(160, 175, 200), font=fnt)
        draw.text((200, y+40), f"{sp2} {sp1} {gp2} 0", fill=(130, 145, 165), font=fnt)
        c1 = (100, 210, 140) if lo1 > 0 else (230, 90, 90)
        c2 = (100, 210, 140) if lo2 > 0 else (230, 90, 90)
        draw.text((380, y+20), f"{lo1:+d}", fill=c1, font=fnt)
        draw.text((380, y+40), f"{lo2:+d}", fill=c2, font=fnt)
        y += 94
    return img


def drift_sim_state():
    import random
    _init_drift()
    for m in SIM_MATCHES:
        k    = f"{m[0]}|{m[1]}"
        big  = random.random() < 0.15
        step = random.randint(15, 40) if big else random.randint(2, 10)
        SIM_DRIFT[k] = max(-200, min(200, SIM_DRIFT[k] + random.choice([-1, 1]) * step))


def parse_sim_matches() -> list[dict]:
    _init_drift()
    out = []
    for m in SIM_MATCHES:
        p1, p2, sp1, sp2, gp1, gp2, o1, o2 = m
        k = f"{m[0]}|{m[1]}"
        d = SIM_DRIFT.get(k, 0)
        out.append({
            "p1": p1, "p2": p2,
            "sets": f"{sp1}-{sp2}", "games": f"{gp1}-{gp2}",
            "p1_odds": o1 + d, "p2_odds": o2 - d,
        })
    return out


# =============================================================================
#  WINDOW CAPTURE
# =============================================================================

BLACK_THRESH = 18


def capture_window(title: str) -> Image.Image | None:
    if not CAPTURE_AVAILABLE:
        return None
    wins = [w for w in gw.getAllWindows()
            if title.lower() in w.title.lower() and w.width > 20]
    if not wins:
        print(f"{YELLOW}[WARN] Window '{title}' not found.{RESET}")
        return None
    win = wins[0]
    try:
        win.activate()
        time.sleep(0.1)
    except Exception:
        pass
    return pyautogui.screenshot(region=(win.left, win.top, win.width, win.height))


def crop_content(pil_img: Image.Image) -> Image.Image:
    arr       = np.array(pil_img.convert("L"))
    col_means = arr.mean(axis=0)
    active    = np.where(col_means > BLACK_THRESH)[0]
    if len(active) < 20:
        return pil_img
    x0, x1 = int(active[0]), int(active[-1])
    h  = pil_img.height
    y0 = max(0, int(h * 0.06))
    y1 = int(h * 0.91)
    return pil_img.crop((x0, y0, x1, y1))


# =============================================================================
#  IMAGE PRE-PROCESSING + OCR
# =============================================================================

def preprocess_for_ocr(pil_img: Image.Image) -> np.ndarray:
    img_np   = np.array(pil_img.convert("RGB"))
    gray     = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
    clahe    = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    kernel   = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
    sharp    = cv2.filter2D(enhanced, -1, kernel)
    h, w     = sharp.shape
    return cv2.resize(sharp, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)


def ocr_image(pil_img: Image.Image) -> list[str]:
    if not OCR_AVAILABLE:
        return []
    processed     = preprocess_for_ocr(pil_img)
    pil_processed = Image.fromarray(processed)
    config = (
        "--psm 6 --oem 3 "
        "-c tessedit_char_whitelist="
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-. "
    )
    raw = pytesseract.image_to_string(pil_processed, config=config)
    out = []
    for line in raw.splitlines():
        line = line.strip()
        line = re.sub(r"[^\w\s\.\+\-]", " ", line)
        line = re.sub(r"\s{2,}", " ", line).strip()
        if line:
            out.append(line)
    return out


# =============================================================================
#  PARSER
# =============================================================================

BANNER_RE = re.compile(
    r"\b(ATP|WTA)\b.*\b(Halle|London|Berlin|Nottingham|Paris|Madrid|Rome|"
    r"Cincinnati|Toronto|Montreal|Shanghai|Dubai|Doha|Miami|Indian Wells|"
    r"Melbourne|Roland|Wimbledon|Flushing|Strasbourg|Stuttgart|Charleston|"
    r"Wuhan|Beijing|Vienna|Basel|Stockholm|Metz|Lyon|Munich|Hamburg|Barcelona|"
    r"Queens|Eastbourne|Birmingham|Hertogenbosch|Rosmalen|Mallorca|Newport|"
    r"Washington|Los Angeles|Winston|Kitzbuhel|Gstaad|Bastad|Umag|Bucharest|"
    r"Atlanta|Prague|Budapest|Lausanne|Palermo|Iasi|Granby|Cleveland|"
    r"Guadalajara|Tenerife|Ostrava|Seoul|Ningbo|Tokyo|Tianjin|Linz|Tallinn|"
    r"Parma|Nur-Sultan|Astana|Pune|Adelaide|Auckland|Hobart|Delray|Dallas|"
    r"Acapulco|Santiago|Buenos Aires|Rio|Marseille|Rotterdam|Montpellier|"
    r"Cordoba|Singapore|Zhengzhou|Chengdu|Zhuhai)\b",
    re.IGNORECASE
)
CHROME_RE = re.compile(
    r"^(More Bets|Sets \d[\--]\d|First set|Second set|Third set|Home|Live|"
    r"My Bets|Rewards|Search|Tennis|Soccer|Darts|Cricket|Snooker|"
    r"Game Winner|Set Winner|Point Winner|Set Betting|Total Games|"
    r"Set Spread|Total Bets|Main|\d{1,2}:\d{2})$",
    re.IGNORECASE
)

ODDS_TAIL_RE    = re.compile(r"([+\-]\d{3,5})\s*$")
SERVING_DOT_RE  = re.compile(r"^[\.\·\•\*·•●]\s*")


def _clean_line(line: str) -> str:
    line = SERVING_DOT_RE.sub("", line.strip())
    return re.sub(r"\s+", " ", line).strip()


def _extract_name(tokens: list[str]) -> str:
    POINT_SCORES = {"0", "15", "30", "40"}
    name_parts = []
    for t in tokens:
        if t in POINT_SCORES or re.fullmatch(r"\d+", t):
            break
        name_parts.append(t)
    name = " ".join(name_parts).strip()
    if len(name) >= 3 and re.search(r"[A-Za-z]", name):
        return name
    return ""


def parse_matches(text_lines: list[str]) -> list[dict]:
    candidate_rows = []
    for raw in text_lines:
        line = _clean_line(raw)
        if not line or BANNER_RE.search(line) or CHROME_RE.match(line):
            continue
        m = ODDS_TAIL_RE.search(line)
        if not m:
            continue
        odds_val = int(m.group(1))
        if abs(odds_val) < 100 or abs(odds_val) > 50000:
            continue
        prefix = line[:m.start()].strip()
        tokens = prefix.split()
        if len(tokens) < 2:
            continue
        candidate_rows.append({"line": line, "tokens": tokens, "odds": odds_val})

    matches = []
    i = 0
    while i < len(candidate_rows) - 1:
        r1, r2 = candidate_rows[i], candidate_rows[i + 1]
        p1 = clean_name(_extract_name(r1["tokens"]))
        p2 = clean_name(_extract_name(r2["tokens"]))
        if not p1 or not p2:
            i += 1
            continue
        o1, o2 = r1["odds"], r2["odds"]
        if o1 < -10000 or o2 < -10000:
            i += 1
            continue

        def score_digits(row):
            POINT_SCORES = {"0", "15", "30", "40"}
            past_name = False
            digits = []
            for t in row["tokens"]:
                if not past_name:
                    if re.fullmatch(r"\d+", t) or t in POINT_SCORES:
                        past_name = True
                    else:
                        continue
                if re.fullmatch(r"\d+", t):
                    digits.append(int(t))
            return digits

        d1 = score_digits(r1)
        sets_str = f"{d1[0]}-{d1[1]}" if len(d1) >= 2 else "?-?"

        matches.append({
            "p1": p1, "p2": p2,
            "sets": sets_str,
            "p1_odds": o1, "p2_odds": o2,
        })
        i += 2

    return matches


# =============================================================================
#  ALERT ENGINE
# =============================================================================

def _beep():
    for _ in range(3):
        winsound.Beep(ALERT_BEEP_FREQ, ALERT_BEEP_DURATION)
        time.sleep(0.15)


def fire_alert(match_key: str, player: str, delta: float,
               live_prob: float, base_prob: float, live_odds: int,
               opponent: str, opp_odds: int):
    now = time.time()
    if now - last_alert_at[match_key] < ALERT_COOLDOWN_SEC:
        return
    last_alert_at[match_key] = now
    threading.Thread(target=_beep, daemon=True).start()

    ts = datetime.now().strftime("%H:%M:%S")
    opp_odds_str = f"+{opp_odds}" if opp_odds > 0 else str(opp_odds)
    print(f"\n{RED}{'#' * 64}{RESET}")
    print(f"{RED}#  BET ALERT  [{ts}]{'':<42}#{RESET}")
    print(f"{RED}#  BET ON : {BOLD}{opponent:<51}{RESET}{RED}#{RESET}")
    print(f"{RED}#  ODDS   : {opp_odds_str:<52}#{RESET}")
    print(f"{RED}#  Why    : {player} dropped {delta:.0f}pp ({base_prob:.0f}% → {live_prob:.0f}%){'':<18}#{RESET}")
    print(f"{RED}{'#' * 64}{RESET}\n")

    if _dash:
        _dash.show_alert(player, match_key, delta, live_prob, base_prob,
                         live_odds, opponent, opp_odds)


# =============================================================================
#  CORE PROCESSING
# =============================================================================

def process_matches(match_list: list[dict]):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n{GREY}{'─' * 64}{RESET}")
    print(f"{CYAN}[{ts}]  Matches on this frame: {len(match_list)}{RESET}")

    dashboard_rows = []

    for m in match_list:
        p1, p2 = m["p1"], m["p2"]
        o1, o2 = m["p1_odds"], m["p2_odds"]
        key    = f"{p1}|{p2}"

        prob1  = american_to_implied(o1)
        prob2  = american_to_implied(o2)
        vig    = vig_pct(prob1, prob2)

        # Anchor baseline — reject mid-match entries where odds are already extreme
        mid_match = False
        new_flag  = ""
        if key not in baselines:
            if prob1 > BASELINE_MAX_IMPLIED or prob2 > BASELINE_MAX_IMPLIED:
                baselines[key] = {
                    "p1_prob": prob1, "p2_prob": prob2,
                    "p1_odds": o1,    "p2_odds": o2,
                    "seen_at": ts,    "quality": "MID_MATCH",
                }
                new_flag  = f"  {GREY}[MID-MATCH]{RESET}"
                mid_match = True
            else:
                baselines[key] = {
                    "p1_prob": prob1, "p2_prob": prob2,
                    "p1_odds": o1,    "p2_odds": o2,
                    "seen_at": ts,    "quality": "OK",
                }
                new_flag = f"  {GREEN}[NEW BASELINE]{RESET}"

        base      = baselines[key]
        mid_match = base.get("quality") == "MID_MATCH"
        d1        = base["p1_prob"] - prob1
        d2        = base["p2_prob"] - prob2

        def delta_col(d):
            if d >= DELTA_THRESHOLD_PCT:          return RED
            if d >= DELTA_THRESHOLD_PCT * 0.55:   return YELLOW
            return GREEN

        print(f"\n  {BOLD}{p1}{RESET} vs {BOLD}{p2}{RESET}"
              f"  Sets:{m.get('sets','?')}  Vig:{vig:.2f}%{new_flag}")
        print(f"  {p1:<22} {o1:>+7d}  {prob1:>6.1f}%  {base['p1_prob']:>6.1f}%  "
              f"{delta_col(d1)}{d1:>+8.1f}pp{RESET}")
        print(f"  {p2:<22} {o2:>+7d}  {prob2:>6.1f}%  {base['p2_prob']:>6.1f}%  "
              f"{delta_col(d2)}{d2:>+8.1f}pp{RESET}")

        if not mid_match:
            if d1 >= DELTA_THRESHOLD_PCT:
                fire_alert(key, p1, d1, prob1, base["p1_prob"], o1, p2, o2)
            if d2 >= DELTA_THRESHOLD_PCT:
                fire_alert(key, p2, d2, prob2, base["p2_prob"], o2, p1, o1)

        dashboard_rows.append({
            "key":       key,
            "p1":        p1,    "p2":      p2,
            "sets":      m.get("sets", "?-?"),
            "p1_odds":   o1,    "p2_odds": o2,
            "p1_prob":   prob1, "p2_prob": prob2,
            "base_p1":   base["p1_prob"], "base_p2": base["p2_prob"],
            "d1":        d1,    "d2":      d2,
            "mid_match": mid_match,
        })

    if _dash:
        scroll_pct = int(scroll_step / max(SWIPES_PER_PASS, 1) * 100)
        _dash.update_matches(dashboard_rows,
                             f"scanning list {scroll_pct}%")


# =============================================================================
#  MAIN SCAN LOOP
# =============================================================================

def scan_once():
    global scroll_step

    if SIMULATION_MODE:
        drift_sim_state()
        match_list = parse_sim_matches()
        process_matches(match_list)
        return

    img = capture_window(WINDOW_TITLE)
    if img is None:
        return

    img_cropped = crop_content(img)
    text_lines  = ocr_image(img_cropped)
    match_list  = parse_matches(text_lines)
    process_matches(match_list)

    scroll_step += 1
    if scroll_step >= SWIPES_PER_PASS:
        reset_to_top()
        scroll_step = 0
        print(f"{CYAN}[SCROLL] Pass reset.  Baselines: {len(baselines)}{RESET}")
    else:
        scroll_down()
        pct = int(scroll_step / SWIPES_PER_PASS * 100)
        print(f"{GREY}[SCROLL] Step {scroll_step}/{SWIPES_PER_PASS}  ({pct}%){RESET}")


def _scan_loop():
    while True:
        try:
            scan_once()
        except Exception as e:
            print(f"{RED}[ERROR] {e}{RESET}")
        time.sleep(SCAN_INTERVAL_SEC)


def main():
    if not SIMULATION_MODE:
        if not CAPTURE_AVAILABLE:
            print(f"{RED}[ERROR] Install pygetwindow + pyautogui.{RESET}")
            return
        if not OCR_AVAILABLE:
            print(f"{RED}[ERROR] Install pytesseract.{RESET}")
            return

    # Start scan loop on background thread
    t = threading.Thread(target=_scan_loop, daemon=True)
    t.start()

    # Run dashboard on main thread (required for tkinter on Windows)
    start_dashboard()
    if _dash:
        _dash.run()   # blocks until window is closed


if __name__ == "__main__":
    main()
