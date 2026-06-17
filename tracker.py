# =============================================================================
#  TENNIS LIVE ODDS SCANNER v3  --  ZB30 / Scrcpy Edition
#  Scroll   : pyautogui mouse-wheel (no ADB/USB required) with ADB fallback
#  UI       : tkinter overlay — plain-English match table + alert panel
# =============================================================================

import re
import os
import json
import time
import threading
import subprocess
import winsound
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    import mss
    MSS_AVAILABLE = True
except ImportError:
    MSS_AVAILABLE = False

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

try:
    import keyboard as kb
    KB_AVAILABLE = True
except ImportError:
    KB_AVAILABLE = False


# =============================================================================
#  USER CONFIGURATION
# =============================================================================

SIMULATION_MODE      = False
WINDOW_TITLE         = "ZB30"
CAPTURE_MONITOR      = 2      # mss monitor index: 1=primary, 2=second monitor, 0=all screens

SCAN_INTERVAL_SEC    = 0
OCR_ROW_HEIGHT_PX    = 55     # strip height in the 2× preprocessed image; tune if lines get split
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

# ── Cross-book arbitrage (The Odds API) ───────────────────────────────────────
ODDS_API_KEY    = ""     # Paste your key here — same one used in index.html
ARB_GAP_PP      = 3.0   # Minimum pp gap between Fanatics and another book to flag
ARB_CACHE_TTL   = 60    # Seconds before the API response cache expires

TENNIS_SPORT_KEYS = [
    "tennis_atp_halle_open", "tennis_atp_wimbledon", "tennis_atp_queens_club_champ",
    "tennis_atp_french_open", "tennis_atp_us_open", "tennis_atp_aus_open_singles",
    "tennis_atp_canadian_open", "tennis_atp_cincinnati_open", "tennis_atp_indian_wells",
    "tennis_atp_italian_open", "tennis_atp_madrid_open", "tennis_atp_monte_carlo_masters",
    "tennis_atp_shanghai_masters", "tennis_atp_paris_masters", "tennis_atp_hamburg_open",
    "tennis_atp_munich", "tennis_atp_barcelona_open", "tennis_atp_dubai", "tennis_atp_qatar_open",
    "tennis_atp_china_open",
    "tennis_wta_german_open", "tennis_wta_wimbledon", "tennis_wta_french_open",
    "tennis_wta_us_open", "tennis_wta_aus_open_singles", "tennis_wta_canadian_open",
    "tennis_wta_cincinnati_open", "tennis_wta_indian_wells", "tennis_wta_italian_open",
    "tennis_wta_madrid_open", "tennis_wta_strasbourg", "tennis_wta_charleston_open",
    "tennis_wta_stuttgart_open", "tennis_wta_dubai", "tennis_wta_miami_open",
]


# =============================================================================
#  GLOBAL STATE
# =============================================================================

baselines        = {}
last_alert_at    = defaultdict(float)

BASELINES_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baselines.json")
WARMUP_SCANS     = 3      # number of consistent readings before baseline locks in
WARMUP_VARIANCE  = 5.0    # max pp spread across warmup readings to be considered stable
scroll_step       = 0        # kept for compatibility — no longer drives scroll logic
_scroll_dir       = 1        # 1 = scrolling down, -1 = scrolling up
_prev_keys        = frozenset()   # match keys from last frame (resistance detector)
_stall_count      = 0             # consecutive frames with same/empty content
STALL_LIMIT       = 2             # frames before declaring resistance and flipping
_adb_ok           = None
scrolling_enabled = True

_arb_cache = {"ts": 0.0, "events": []}
_arb_lock  = threading.Lock()

# =============================================================================
#  BASELINE PERSISTENCE
# =============================================================================

def save_baselines():
    try:
        with open(BASELINES_FILE, "w") as f:
            json.dump(baselines, f)
    except Exception as e:
        print(f"{YELLOW}[SAVE] Could not save baselines: {e}{RESET}")


def save_baselines_async():
    """Fire-and-forget baseline save — don't stall the scan loop on HDD write."""
    threading.Thread(target=save_baselines, daemon=True).start()


def load_baselines():
    try:
        import json
        with open(BASELINES_FILE) as f:
            data = json.load(f)
        baselines.update(data)
        print(f"{GREEN}[LOAD] Restored {len(baselines)} baselines from disk.{RESET}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"{YELLOW}[LOAD] Could not load baselines: {e}{RESET}")


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

    # ── Keyboard shortcuts (global — work even when scrcpy has focus) ────────

    def _bind_keys(self):
        if not KB_AVAILABLE:
            return
        kb.add_hotkey("space",  self._toggle_scroll,   suppress=False)
        kb.add_hotkey("escape", self._dismiss_alert,   suppress=False)
        kb.add_hotkey("r",      self._reset_baselines, suppress=False)
        kb.add_hotkey("s",      lambda: self.root.after(0, self._open_settings), suppress=False)

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
            ("Match",        30, "w"),
            ("Score",         7, "center"),
            ("Live odds",    12, "center"),
            ("Win %",         9, "center"),
            ("Drop",          8, "center"),
            ("Speed",         9, "center"),
            ("Tier",         10, "center"),
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
        tk.Label(w, text="🚨  SHARP MONEY DETECTED  🚨",
                 bg="#8b0000", fg="white",
                 font=("Segoe UI", 36, "bold")).pack(pady=(50, 4))

        # ── BET line — big and yellow, zero ambiguity ─────────────────────────
        tk.Label(w, text="BET ON:",
                 bg="#8b0000", fg="#fca5a5",
                 font=("Segoe UI", 22)).pack(pady=(14, 0))

        tk.Label(w, text=opponent,
                 bg="#8b0000", fg="#ffff00",
                 font=("Segoe UI", 72, "bold")).pack(pady=0)

        opp_odds_str = f"+{opp_odds}" if opp_odds > 0 else str(opp_odds)
        tk.Label(w, text=f"Fanatics odds:  {opp_odds_str}",
                 bg="#8b0000", fg="white",
                 font=("Segoe UI", 32)).pack(pady=4)

        tk.Frame(w, bg="#cc0000", height=3).pack(fill="x", pady=14, padx=80)

        # ── Why section — explicit cause → effect logic ───────────────────────
        cause = (f"{player}  collapsed:   {base_prob:.0f}%  →  {live_prob:.0f}%  "
                 f"(−{delta:.0f} pp implied)")
        tk.Label(w, text=cause,
                 bg="#8b0000", fg="#fca5a5",
                 font=("Segoe UI", 18),
                 justify="center").pack(pady=2)

        tk.Label(w,
                 text=f"↓  market moved against {player}  →  {opponent} is VALUE  ↓",
                 bg="#8b0000", fg="#fdba74",
                 font=("Segoe UI", 16, "italic")).pack(pady=2)

        tk.Label(w, text=f"Match:  {match_key.replace('|', ' vs ')}",
                 bg="#8b0000", fg="#7f1d1d",
                 font=("Segoe UI", 14)).pack(pady=(6, 0))

        # ── Arb placeholder — filled in async by show_arb_info ────────────────
        self._arb_frame = tk.Frame(w, bg="#8b0000")
        self._arb_frame.pack(fill="x", padx=80, pady=8)

        tk.Label(w, text="Press any key or click to dismiss",
                 bg="#8b0000", fg="#7f1d1d",
                 font=("Segoe UI", 13)).pack(pady=(8, 0))

        # Auto-dismiss after 60s
        w.after(60000, self._dismiss_alert)

    def _dismiss_alert(self):
        if self._alert_window:
            try:
                self._alert_window.destroy()
            except Exception:
                pass
            self._alert_window = None
        self._arb_frame = None

    def show_arb_info(self, arb: dict, target_player: str):
        """Populate the arb section of the active alert window. Called from main thread."""
        if not self._alert_window or not hasattr(self, "_arb_frame") or not self._arb_frame:
            return

        BOOK_LABELS = {"fanatics": "Fanatics", "draftkings": "DraftKings", "fanduel": "FanDuel"}

        # Determine which side of the API match corresponds to target_player
        p1_api = arb["p1"]
        p2_api = arb["p2"]
        is_p1 = True
        if FUZZ_AVAILABLE:
            r = fuzz_process.extractOne(target_player, [p1_api, p2_api], score_cutoff=50)
            is_p1 = bool(r and r[0] == p1_api)

        fan_data  = arb["books"].get("fanatics", {})
        fan_odds  = fan_data.get("p1_odds" if is_p1 else "p2_odds")
        fan_impl  = american_to_implied(fan_odds) if fan_odds is not None else None

        lines = []
        best_book  = None
        best_odds  = None

        for book, data in arb["books"].items():
            odds = data.get("p1_odds" if is_p1 else "p2_odds")
            if odds is None:
                continue
            impl      = american_to_implied(odds)
            odds_str  = f"+{odds}" if odds > 0 else str(odds)
            label     = BOOK_LABELS.get(book, book)
            gap_tag   = ""
            if fan_impl is not None and book != "fanatics":
                gap = fan_impl - impl   # positive = Fanatics already moved, DK/FD haven't
                if gap >= ARB_GAP_PP:
                    gap_tag = f"  ← {gap:.1f}pp gap — BET HERE"
                elif gap <= -ARB_GAP_PP:
                    gap_tag = "  ← Fanatics is behind other books"
            lines.append(f"  {label:<13}{odds_str:>7}   ({impl:.1f}%){gap_tag}")
            if best_odds is None or odds > best_odds:
                best_odds = odds
                best_book = label

        if not lines:
            return

        try:
            f = self._arb_frame
            tk.Frame(f, bg="#660000", height=2).pack(fill="x", pady=(0, 6))
            tk.Label(f,
                     text=f"Cross-book check — {target_player}",
                     bg="#8b0000", fg="#fcd34d",
                     font=("Segoe UI", 15, "bold")).pack()
            tk.Label(f,
                     text="\n".join(lines),
                     bg="#8b0000", fg="#fde68a",
                     font=("Courier New", 13),
                     justify="left").pack(pady=2)
            if best_book and best_book != "Fanatics":
                tk.Label(f,
                         text=f"Best odds:  {best_book}  →  consider betting there",
                         bg="#8b0000", fg="#4ade80",
                         font=("Segoe UI", 13, "bold")).pack(pady=2)
            elif best_book == "Fanatics":
                tk.Label(f,
                         text="Fanatics has the best line — books agree, signal confirmed.",
                         bg="#8b0000", fg="#86efac",
                         font=("Segoe UI", 12)).pack(pady=2)
        except Exception:
            pass

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

        self._sv_watch_vel = tk.DoubleVar(value=WATCH_VEL_PPM)
        slider_row(sw, "WATCH velocity (pp/min)",
                   self._sv_watch_vel, 1, 15, lambda v: f"{v:.1f}",
                   lambda v: self._set("WATCH_VEL_PPM", v))

        self._sv_attack_vel = tk.DoubleVar(value=ATTACK_VEL_PPM)
        slider_row(sw, "ATTACK velocity (pp/min)",
                   self._sv_attack_vel, 3, 20, lambda v: f"{v:.1f}",
                   lambda v: self._set("ATTACK_VEL_PPM", v))

        self._sv_lock = tk.DoubleVar(value=ATTACK_LOCK_SEC)
        slider_row(sw, "Lock-on duration (seconds)",
                   self._sv_lock, 10, 60, lambda v: f"{v:.0f}s",
                   lambda v: self._set("ATTACK_LOCK_SEC", v))

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
        try:
            import os as _os
            if _os.path.exists(BASELINES_FILE):
                _os.remove(BASELINES_FILE)
        except Exception:
            pass
        self.lbl_status.config(text="Baselines RESET")
        print(f"{YELLOW}[RESET] All baselines cleared and file deleted.{RESET}")

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

            tier = m.get("tier", "NORMAL")
            mid  = m.get("mid_match", False)

            if tier == "ATTACK":
                row_bg, tier_text, tier_color = "#2d0a0a", "🔴 ATTACK", self.RED_C
            elif tier == "WATCH":
                row_bg, tier_text, tier_color = "#1f1a00", "🟡 WATCH",  self.YELLOW_C
            elif mid:
                row_bg, tier_text, tier_color = "#111111", "⚫ Mid",    self.GREY_C
            else:
                row_bg = self.PANEL if idx % 2 == 0 else self.BG
                tier_text, tier_color = "🟢", self.GREEN_C

            def fmt_odds(o): return f"+{o}" if o > 0 else str(o)

            worst = max(m["d1"], m["d2"])
            vel   = m.get("velocity", 0.0)

            drop_str  = f"▼{worst:.1f}pp" if worst > 0.5 else "—"
            drop_col  = (self.RED_C    if worst >= DELTA_THRESHOLD_PCT
                         else self.YELLOW_C if worst >= 6
                         else self.WHITE)
            vel_str   = f"{vel:+.1f}/m" if abs(vel) > 0.2 else "—"
            vel_col   = (self.RED_C    if vel >= ATTACK_VEL_PPM
                         else self.YELLOW_C if vel >= WATCH_VEL_PPM
                         else self.WHITE)
            prob_str  = f"{m['p1_prob']:.0f}/{m['p2_prob']:.0f}%"

            cells = [
                (f"{m['p1']} vs {m['p2']}", 30, "w",      self.WHITE),
                (m.get("sets", "?-?"),        7, "center", self.GREY_C),
                (f"{fmt_odds(m['p1_odds'])}/{fmt_odds(m['p2_odds'])}",
                                             12, "center", self.WHITE),
                (prob_str,                    9, "center", self.WHITE),
                (drop_str,                    8, "center", drop_col),
                (vel_str,                     9, "center", vel_col),
                (tier_text,                  10, "center", tier_color),
            ]

            if key in self._row_widgets:
                widgets  = self._row_widgets[key]
                frame    = widgets[0]
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


def _scroll_one_step():
    """Scroll one step in the current direction (_scroll_dir: 1=down, -1=up)."""
    if not scrolling_enabled:
        return
    if USE_ADB_SCROLL and _adb_available():
        y1, y2 = (SWIPE_FROM_Y, SWIPE_TO_Y) if _scroll_dir == 1 else (SWIPE_TO_Y, SWIPE_FROM_Y)
        subprocess.run(
            f'adb shell input swipe {SWIPE_X} {y1} {SWIPE_X} {y2} {SWIPE_DURATION_MS}',
            shell=True, capture_output=True, timeout=5)
    else:
        win = _get_window()
        if win:
            _post_scroll(win._hWnd, SCROLL_CLICKS * _scroll_dir)
    time.sleep(SCROLL_SETTLE_SEC)


def reset_to_top():
    """Slam to the top of the list — used once at startup."""
    if USE_ADB_SCROLL and _adb_available():
        subprocess.run(
            f'adb shell input tap {LIVE_TAB_X} {LIVE_TAB_Y}',
            shell=True, capture_output=True, timeout=5)
        time.sleep(1.2)
    else:
        win = _get_window()
        if win:
            for _ in range(30):
                _post_scroll(win._hWnd, 10)
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
#  CAPTURE  (mss monitor → pyautogui window fallback)
# =============================================================================

BLACK_THRESH = 18


def capture_frame() -> Image.Image | None:
    """Grab the source image.

    Primary: mss full-screen grab of CAPTURE_MONITOR (fast, no focus steal).
    Fallback: pyautogui window grab of WINDOW_TITLE (used if mss unavailable
              or the requested monitor index doesn't exist).
    """
    if MSS_AVAILABLE:
        try:
            with mss.mss() as sct:
                monitors = sct.monitors   # [0]=all, [1]=primary, [2]=second, …
                if CAPTURE_MONITOR < len(monitors):
                    shot = sct.grab(monitors[CAPTURE_MONITOR])
                    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                print(f"{YELLOW}[WARN] Monitor {CAPTURE_MONITOR} not found "
                      f"({len(monitors)-1} monitor(s) detected) — falling back to window capture.{RESET}")
        except Exception as e:
            print(f"{YELLOW}[WARN] mss capture failed ({e}) — falling back to window capture.{RESET}")

    return _capture_window_fallback()


def _capture_window_fallback() -> Image.Image | None:
    if not CAPTURE_AVAILABLE:
        return None
    wins = [w for w in gw.getAllWindows()
            if WINDOW_TITLE.lower() in w.title.lower() and w.width > 20]
    if not wins:
        print(f"{YELLOW}[WARN] Window '{WINDOW_TITLE}' not found.{RESET}")
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
    """Parallel row-strip OCR — ~3-4× faster than single-pass psm 6."""
    if not OCR_AVAILABLE:
        return []

    processed = preprocess_for_ocr(pil_img)
    h, _      = processed.shape
    config    = (
        "--psm 7 --oem 3 "
        "-c tessedit_char_whitelist="
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-. "
    )

    def _ocr_strip(y0: int) -> str:
        strip = processed[y0 : min(y0 + OCR_ROW_HEIGHT_PX, h), :]
        if strip.mean() < 8:      # skip nearly-black strips
            return ""
        return pytesseract.image_to_string(Image.fromarray(strip), config=config)

    ys = list(range(0, h, OCR_ROW_HEIGHT_PX))
    with ThreadPoolExecutor(max_workers=min(len(ys), 8)) as pool:
        raw_strips = list(pool.map(_ocr_strip, ys))

    out = []
    for raw in raw_strips:
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
    print(f"{RED}#  SHARP MONEY DETECTED  [{ts}]{'':<31}#{RESET}")
    print(f"{RED}#  BET ON  : {BOLD}{opponent:<50}{RESET}{RED}#{RESET}")
    print(f"{RED}#  ODDS    : {opp_odds_str:<52}#{RESET}")
    print(f"{RED}#  BECAUSE : {player} collapsed  {base_prob:.0f}% → {live_prob:.0f}%  (−{delta:.0f}pp){'':<10}#{RESET}")
    print(f"{RED}{'#' * 64}{RESET}\n")

    if _dash:
        _dash.show_alert(player, match_key, delta, live_prob, base_prob,
                         live_odds, opponent, opp_odds)

    if ODDS_API_KEY:
        p1, p2 = match_key.split("|", 1)
        threading.Thread(
            target=_arb_check_and_update, args=(p1, p2, opponent),
            daemon=True
        ).start()


# =============================================================================
#  CROSS-BOOK ARBITRAGE
# =============================================================================

def _fetch_sport_events(sport: str) -> list:
    url = (
        f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
        f"?apiKey={ODDS_API_KEY}&regions=us&markets=h2h"
        f"&oddsFormat=american&bookmakers=fanatics,draftkings,fanduel"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tennis-tracker/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()) if r.status == 200 else []
    except Exception:
        return []


def _refresh_arb_cache():
    """Fetch all active tennis events in parallel. No-op if cache is still fresh."""
    if not ODDS_API_KEY:
        return
    with _arb_lock:
        if time.time() - _arb_cache["ts"] < ARB_CACHE_TTL:
            return
    print(f"{CYAN}[ARB] Fetching cross-book odds…{RESET}")
    all_events = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_sport_events, s): s for s in TENNIS_SPORT_KEYS}
        for fut in as_completed(futures):
            all_events.extend(fut.result())
    with _arb_lock:
        _arb_cache["ts"] = time.time()
        _arb_cache["events"] = all_events
    print(f"{CYAN}[ARB] Cached {len(all_events)} events from The Odds API.{RESET}")


def _find_arb(player1: str, player2: str) -> dict | None:
    """Fuzzy-match a match against the arb cache and return structured odds by book."""
    with _arb_lock:
        events = list(_arb_cache["events"])
    if not events or not FUZZ_AVAILABLE:
        return None

    best_ev    = None
    best_score = 0
    for ev in events:
        h = ev.get("home_team", "")
        a = ev.get("away_team", "")
        r1 = fuzz_process.extractOne(player1, [h, a], score_cutoff=55)
        r2 = fuzz_process.extractOne(player2, [h, a], score_cutoff=55)
        score = (r1[1] if r1 else 0) + (r2[1] if r2 else 0)
        if score > best_score:
            best_score = score
            best_ev    = ev

    if not best_ev or best_score < 90:
        return None

    h_name = best_ev["home_team"]
    a_name = best_ev["away_team"]
    by_book: dict = {}
    for bm in best_ev.get("bookmakers", []):
        h2h = next((mk for mk in bm.get("markets", []) if mk["key"] == "h2h"), None)
        if h2h:
            by_book[bm["key"]] = {o["name"]: o["price"] for o in h2h["outcomes"]}

    result: dict = {"p1": h_name, "p2": a_name, "books": {}}
    for book in ("fanatics", "draftkings", "fanduel"):
        bdata = by_book.get(book)
        if bdata:
            result["books"][book] = {
                "p1_odds": bdata.get(h_name),
                "p2_odds": bdata.get(a_name),
            }
    return result if result["books"] else None


def _arb_check_and_update(p1: str, p2: str, target_player: str):
    """Background thread: refresh cache, find match, push arb info to alert window."""
    _refresh_arb_cache()
    arb = _find_arb(p1, p2)
    if arb and _dash:
        _dash.root.after(0, _dash.show_arb_info, arb, target_player)
    elif _dash and ODDS_API_KEY:
        print(f"{YELLOW}[ARB] Match not found on The Odds API: {p1} / {p2}{RESET}")


# =============================================================================
#  VELOCITY + TIER LOGIC
# =============================================================================

WATCH_DELTA_PP   = 6.0    # pp drop to enter WATCH tier
WATCH_VEL_PPM    = 3.0    # pp/min velocity to enter WATCH tier
ATTACK_VEL_PPM   = 8.0    # pp/min velocity to enter ATTACK tier
ATTACK_LOCK_SEC  = 30     # seconds to hold lock-on before auto-resuming scroll

_attack_resume_timer = None   # threading.Timer handle


def _update_history(base: dict, prob1: float, prob2: float):
    """Append current reading to match history, keep last 20."""
    hist = base.setdefault("history", [])
    hist.append((time.time(), prob1, prob2))
    if len(hist) > 20:
        hist.pop(0)


def _velocity(base: dict) -> float:
    """Return pp/min drop rate for the worse player. Positive = dropping."""
    h = base.get("history", [])
    if len(h) < 2:
        return 0.0
    dt_min = (h[-1][0] - h[0][0]) / 60.0
    if dt_min < 0.05:
        return 0.0
    if len(h[-1]) == 3:                         # (ts, prob1, prob2)
        drop1 = base["p1_prob"] - h[-1][1]
        drop2 = base["p2_prob"] - h[-1][2]
        return max(drop1, drop2) / dt_min
    return (base["p1_prob"] - h[-1][1]) / dt_min   # legacy single-prob entries


def _tier(delta: float, velocity: float, mid_match: bool) -> str:
    if mid_match:
        return "NORMAL"
    if delta >= DELTA_THRESHOLD_PCT or velocity >= ATTACK_VEL_PPM:
        return "ATTACK"
    if delta >= WATCH_DELTA_PP or velocity >= WATCH_VEL_PPM:
        return "WATCH"
    return "NORMAL"


def _auto_resume_scroll():
    global scrolling_enabled, _attack_resume_timer
    _attack_resume_timer = None
    if not scrolling_enabled:
        scrolling_enabled = True
        print(f"{CYAN}[LOCK-ON] 30s elapsed — resuming scroll.{RESET}")
        if _dash:
            _dash.root.after(0, lambda: _dash.btn_scroll.config(
                text="⏸ Pause  [Space]", bg=Dashboard.YELLOW_C, fg="#000"))


def _engage_lock_on(match_key: str):
    """Pause scroll and schedule auto-resume after ATTACK_LOCK_SEC."""
    global scrolling_enabled, _attack_resume_timer
    if scrolling_enabled:
        scrolling_enabled = False
        print(f"{RED}[LOCK-ON] Scroll paused — watching {match_key}{RESET}")
        if _dash:
            _dash.root.after(0, lambda: _dash.btn_scroll.config(
                text="🔒 LOCKED ON  [Space]", bg=Dashboard.RED_C, fg="white"))
    # Reset timer each time the same match keeps escalating
    if _attack_resume_timer:
        _attack_resume_timer.cancel()
    _attack_resume_timer = threading.Timer(ATTACK_LOCK_SEC, _auto_resume_scroll)
    _attack_resume_timer.daemon = True
    _attack_resume_timer.start()


# =============================================================================
#  CORE PROCESSING
# =============================================================================

def process_matches(match_list: list[dict]):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n{GREY}{'─' * 64}{RESET}")
    print(f"{CYAN}[{ts}]  on screen: {len(match_list)}{RESET}")

    dashboard_rows = []

    for m in match_list:
        p1, p2 = m["p1"], m["p2"]
        o1, o2 = m["p1_odds"], m["p2_odds"]
        key    = f"{p1}|{p2}"

        prob1 = american_to_implied(o1)
        prob2 = american_to_implied(o2)

        # ── Baseline anchor with warm-up ──────────────────────────────────────
        if key not in baselines:
            # Immediately reject mid-match entries
            if prob1 > BASELINE_MAX_IMPLIED or prob2 > BASELINE_MAX_IMPLIED:
                baselines[key] = {
                    "p1_prob": prob1, "p2_prob": prob2,
                    "p1_odds": o1,    "p2_odds": o2,
                    "seen_at": ts,    "quality": "MID_MATCH",
                    "history": [],    "warmup":  [],
                }
            else:
                baselines[key] = {
                    "p1_prob": prob1, "p2_prob": prob2,
                    "p1_odds": o1,    "p2_odds": o2,
                    "seen_at": ts,    "quality": "WARMUP",
                    "history": [],    "warmup":  [prob1],
                }

        base = baselines[key]

        # Accumulate warmup readings until stable
        if base["quality"] == "WARMUP":
            wu = base["warmup"]
            if prob1 not in wu:          # avoid double-counting same frame
                wu.append(prob1)
            if len(wu) >= WARMUP_SCANS:
                spread = max(wu) - min(wu)
                if spread <= WARMUP_VARIANCE:
                    # Stable — lock in baseline as average of warmup readings
                    avg1 = sum(wu) / len(wu)
                    avg2 = 100 - avg1    # approximate; will refine on next frame
                    base["p1_prob"] = avg1
                    base["p2_prob"] = prob2
                    base["quality"] = "OK"
                    print(f"{GREEN}[BASELINE] Locked: {key}  ({avg1:.1f}% / {prob2:.1f}%  "
                          f"after {len(wu)} readings){RESET}")
                    save_baselines()
                else:
                    # Unstable — reset and try again
                    base["warmup"] = [prob1]

        mid_match = base["quality"] in ("MID_MATCH", "WARMUP")

        d1  = base["p1_prob"] - prob1
        d2  = base["p2_prob"] - prob2
        _update_history(base, prob1, prob2)

        worst_delta = max(d1, d2)
        vel = _velocity(base)
        tier = _tier(worst_delta, vel, mid_match)

        # ── Terminal print (compact) ──────────────────────────────────────────
        tier_tag = f"{RED}[ATTACK]{RESET}" if tier=="ATTACK" else \
                   f"{YELLOW}[WATCH]{RESET}"  if tier=="WATCH"  else ""
        if tier != "NORMAL" or worst_delta > 2:
            print(f"  {p1} vs {p2}  Δ{worst_delta:.1f}pp  {vel:+.1f}pp/min  {tier_tag}")

        # ── Fire alert + lock-on ──────────────────────────────────────────────
        if not mid_match:
            if tier == "ATTACK":
                _engage_lock_on(key)
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
            "d1":        d1,    "d2":      d2,
            "velocity":  vel,
            "tier":      tier,
            "mid_match": mid_match,
        })

    if _dash:
        # Sort: ATTACK first, then WATCH, then NORMAL, then MID_MATCH
        tier_order = {"ATTACK": 0, "WATCH": 1, "NORMAL": 2}
        dashboard_rows.sort(key=lambda r: (
            tier_order.get(r["tier"], 3),
            -max(r["d1"], r["d2"])
        ))
        attack_n  = sum(1 for r in dashboard_rows if r["tier"] == "ATTACK")
        watch_n   = sum(1 for r in dashboard_rows if r["tier"] == "WATCH")
        dir_label = "↓ DOWN" if _scroll_dir == 1 else "↑ UP"
        status    = (f"🔴 {attack_n} ATTACK  🟡 {watch_n} WATCH  |  "
                     f"{dir_label}  |  {len(baselines)} baselines")
        _dash.update_matches(dashboard_rows, status)


# =============================================================================
#  MAIN SCAN LOOP
# =============================================================================

def scan_once():
    global _scroll_dir, _prev_keys, _stall_count

    if SIMULATION_MODE:
        drift_sim_state()
        match_list = parse_sim_matches()
        process_matches(match_list)
        return

    img = capture_frame()
    if img is None:
        return

    img_cropped  = crop_content(img)
    text_lines   = ocr_image(img_cropped)
    match_list   = parse_matches(text_lines)
    current_keys = frozenset(f"{m['p1']}|{m['p2']}" for m in match_list)

    # ── Resistance detection ──────────────────────────────────────────────────
    # Same content as the previous frame (or empty screen) means the scroll hit
    # an end — flip direction immediately. No fixed step counter needed.
    if current_keys == _prev_keys or not current_keys:
        _stall_count += 1
        if _stall_count >= STALL_LIMIT:
            _scroll_dir *= -1
            _stall_count  = 0
            _prev_keys    = frozenset()
            label = "↓ DOWN" if _scroll_dir == 1 else "↑ UP"
            print(f"{CYAN}[SCROLL] Resistance — switching to {label}{RESET}")
            save_baselines_async()
    else:
        _stall_count = 0
        _prev_keys   = current_keys

    if match_list:
        process_matches(match_list)

    _scroll_one_step()


def _scan_loop():
    load_baselines()
    reset_to_top()   # always start from the top of the list
    while True:
        try:
            scan_once()
        except Exception as e:
            print(f"{RED}[ERROR] {e}{RESET}")
        time.sleep(SCAN_INTERVAL_SEC)


def _print_startup_info():
    print(f"{CYAN}{'=' * 56}{RESET}")
    print(f"{CYAN}  Tennis Odds Tracker — startup diagnostics{RESET}")
    if MSS_AVAILABLE:
        with mss.mss() as sct:
            for i, m in enumerate(sct.monitors):
                tag = f"  ← CAPTURE_MONITOR = {i}" if i == CAPTURE_MONITOR else ""
                label = "all screens" if i == 0 else f"monitor {i}"
                print(f"{CYAN}  mss[{i}]  {label:14}  {m['width']}×{m['height']}  "
                      f"at ({m['left']},{m['top']}){tag}{RESET}")
    else:
        print(f"{YELLOW}  mss not installed — run: pip install mss{RESET}")
        print(f"{YELLOW}  Falling back to window capture ('{WINDOW_TITLE}'){RESET}")
    print(f"{CYAN}{'=' * 56}{RESET}")


def main():
    _print_startup_info()

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
