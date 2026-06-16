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
    """Plain-English live overlay.  Runs on the Tk thread."""

    # Colours
    BG       = "#0e1423"
    PANEL    = "#111827"
    HEADER   = "#1e2d4a"
    GREEN_C  = "#22c55e"
    YELLOW_C = "#facc15"
    RED_C    = "#ef4444"
    WHITE    = "#f1f5f9"
    GREY_C   = "#64748b"
    NAVY     = "#1e3a5f"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Tennis Odds Tracker")
        self.root.configure(bg=self.BG)
        self.root.attributes("-topmost", True)
        self.root.geometry("780x560+20+20")
        self.root.resizable(True, True)

        self._build_ui()
        self._match_data   = {}   # key -> dict with display info
        self._pending_alert = None

    def _build_ui(self):
        r = self.root

        # ── Title bar ────────────────────────────────────────────────────────
        title_bar = tk.Frame(r, bg=self.HEADER, pady=6)
        title_bar.pack(fill="x")
        tk.Label(title_bar, text="🎾  Tennis Live Odds Tracker",
                 bg=self.HEADER, fg=self.WHITE,
                 font=("Segoe UI", 14, "bold")).pack(side="left", padx=12)

        self.btn_scroll = tk.Button(
            title_bar, text="⏸ Pause Scroll",
            bg=self.YELLOW_C, fg="#000000",
            font=("Segoe UI", 10, "bold"),
            relief="flat", padx=10, pady=2,
            command=self._toggle_scroll)
        self.btn_scroll.pack(side="right", padx=12, pady=4)

        self.lbl_status = tk.Label(title_bar, text="Starting…",
                                   bg=self.HEADER, fg=self.GREY_C,
                                   font=("Segoe UI", 10))
        self.lbl_status.pack(side="right", padx=12)

        # ── Alert banner (hidden until alert fires) ──────────────────────────
        self.alert_frame = tk.Frame(r, bg=self.RED_C, pady=8)
        self.lbl_alert_head = tk.Label(self.alert_frame,
                                       text="", bg=self.RED_C, fg="white",
                                       font=("Segoe UI", 13, "bold"))
        self.lbl_alert_head.pack(padx=14, anchor="w")
        self.lbl_alert_body = tk.Label(self.alert_frame,
                                       text="", bg=self.RED_C, fg="white",
                                       font=("Segoe UI", 11),
                                       wraplength=740, justify="left")
        self.lbl_alert_body.pack(padx=14, anchor="w")
        # alert_frame packed dynamically when alert fires

        # ── Column headers ───────────────────────────────────────────────────
        hdr = tk.Frame(r, bg=self.PANEL, pady=4)
        hdr.pack(fill="x", padx=6, pady=(6, 0))
        for text, w, anchor in [
            ("Match",       34, "w"),
            ("Score",        9, "center"),
            ("Live odds",   12, "center"),
            ("Win chance",  12, "center"),
            ("vs Baseline", 14, "center"),
            ("Signal",      10, "center"),
        ]:
            tk.Label(hdr, text=text, bg=self.PANEL, fg=self.GREY_C,
                     font=("Segoe UI", 9, "bold"),
                     width=w, anchor=anchor).pack(side="left")

        # ── Scrollable match list ─────────────────────────────────────────────
        container = tk.Frame(r, bg=self.BG)
        container.pack(fill="both", expand=True, padx=6, pady=4)

        canvas = tk.Canvas(container, bg=self.BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.match_frame = tk.Frame(canvas, bg=self.BG)
        self.match_frame.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.match_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ── Footer ────────────────────────────────────────────────────────────
        foot = tk.Frame(r, bg=self.HEADER, pady=4)
        foot.pack(fill="x", side="bottom")
        tk.Label(foot,
                 text="🟢 Normal  🟡 Watch (>6pp drop)  🔴 BET ALERT (>12pp drop)",
                 bg=self.HEADER, fg=self.GREY_C,
                 font=("Segoe UI", 9)).pack(side="left", padx=12)
        self.lbl_count = tk.Label(foot, text="", bg=self.HEADER, fg=self.WHITE,
                                  font=("Segoe UI", 9))
        self.lbl_count.pack(side="right", padx=12)

        # Row widget cache: key -> list of Label widgets
        self._row_widgets = {}

    # ── Public API (called from scan thread via after()) ─────────────────────

    def update_matches(self, match_rows: list[dict], status: str):
        """match_rows = [{key, p1, p2, sets, p1_odds, p2_odds,
                          p1_prob, p2_prob, base_p1, base_p2,
                          d1, d2}]"""
        self.root.after(0, self._do_update, match_rows, status)

    def show_alert(self, player: str, match_key: str,
                   delta: float, live_prob: float, base_prob: float,
                   live_odds: int):
        self.root.after(0, self._do_alert, player, match_key,
                        delta, live_prob, base_prob, live_odds)

    def _do_update(self, match_rows, status):
        ts = datetime.now().strftime("%H:%M:%S")
        self.lbl_status.config(text=f"Last scan: {ts}  |  {status}")
        self.lbl_count.config(text=f"{len(baselines)} baselines  |  {len(match_rows)} on screen")

        # Destroy rows that are no longer present
        current_keys = {r["key"] for r in match_rows}
        for k in list(self._row_widgets.keys()):
            if k not in current_keys:
                for w in self._row_widgets.pop(k):
                    w.destroy()

        for idx, m in enumerate(match_rows):
            key   = m["key"]
            d1    = m["d1"]
            d2    = m["d2"]
            worst = max(d1, d2)

            if worst >= DELTA_THRESHOLD_PCT:
                row_bg = "#2d0a0a"
                sig_text  = "🔴 BET"
                sig_color = self.RED_C
            elif worst >= DELTA_THRESHOLD_PCT * 0.55:
                row_bg = "#1f1a00"
                sig_text  = "🟡 Watch"
                sig_color = self.YELLOW_C
            else:
                row_bg = self.PANEL if idx % 2 == 0 else self.BG
                sig_text  = "🟢"
                sig_color = self.GREEN_C

            # Format odds: show + for underdog, - for favourite
            def fmt_odds(o):
                return f"+{o}" if o > 0 else str(o)

            # Win chance string: "P1 65% / P2 35%"
            prob_str   = f"{m['p1_prob']:.0f}% / {m['p2_prob']:.0f}%"
            delta_str  = f"▼{worst:.1f}pp" if worst > 0.5 else "stable"
            delta_col  = (self.RED_C if worst >= DELTA_THRESHOLD_PCT
                          else self.YELLOW_C if worst >= 6
                          else self.GREEN_C)

            match_str = f"{m['p1']} vs {m['p2']}"
            odds_str  = f"{fmt_odds(m['p1_odds'])} / {fmt_odds(m['p2_odds'])}"
            score_str = m.get("sets", "?-?")

            cells = [
                (match_str,  34, "w",      self.WHITE),
                (score_str,   9, "center", self.GREY_C),
                (odds_str,   12, "center", self.WHITE),
                (prob_str,   12, "center", self.WHITE),
                (delta_str,  14, "center", delta_col),
                (sig_text,   10, "center", sig_color),
            ]

            if key in self._row_widgets:
                for label, (text, _, _, fg) in zip(self._row_widgets[key], cells):
                    label.config(text=text, fg=fg, bg=row_bg)
            else:
                widgets = []
                row_frame = tk.Frame(self.match_frame, bg=row_bg, pady=3)
                row_frame.pack(fill="x", padx=2, pady=1)
                for text, w, anchor, fg in cells:
                    lbl = tk.Label(row_frame, text=text, bg=row_bg, fg=fg,
                                   font=("Segoe UI", 10),
                                   width=w, anchor=anchor)
                    lbl.pack(side="left")
                    widgets.append(lbl)
                # store frame + labels so we can update/destroy
                self._row_widgets[key] = widgets
                # keep frame reference via label parent
                widgets.insert(0, row_frame)   # index 0 = frame

    def _do_alert(self, player, match_key, delta, live_prob, base_prob, live_odds):
        opponents = match_key.replace("|", " vs ")
        head = f"🚨  BET ALERT — {player}"
        body = (
            f"Match: {opponents}\n"
            f"{player} was {base_prob:.0f}% likely to win at the start.  "
            f"They are now {live_prob:.0f}% (odds: {'+' if live_odds>0 else ''}{live_odds}).  "
            f"That's a {delta:.0f}pp drop — the market thinks they're in more trouble. "
            f"This could be a value bet on their opponent."
        )
        self.lbl_alert_head.config(text=head)
        self.lbl_alert_body.config(text=body)
        self.alert_frame.pack(fill="x", padx=6, pady=(4, 0), before=self.match_frame.master)

        # Auto-hide after 30s
        self.root.after(30000, self.alert_frame.pack_forget)

    def _toggle_scroll(self):
        global scrolling_enabled
        scrolling_enabled = not scrolling_enabled
        if scrolling_enabled:
            self.btn_scroll.config(text="⏸ Pause Scroll", bg=self.YELLOW_C)
        else:
            self.btn_scroll.config(text="▶ Resume Scroll", bg=self.GREEN_C)

    def run(self):
        self.root.mainloop()


# Singleton dashboard (None if TK unavailable)
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
               live_prob: float, base_prob: float, live_odds: int):
    now = time.time()
    if now - last_alert_at[match_key] < ALERT_COOLDOWN_SEC:
        return
    last_alert_at[match_key] = now
    threading.Thread(target=_beep, daemon=True).start()

    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n{RED}{'=' * 64}{RESET}")
    print(f"{RED}  ***  VALUE ALERT  [{ts}]{RESET}")
    print(f"{RED}  Match  : {match_key.replace('|', ' vs ')}{RESET}")
    print(f"{RED}  Player : {player}{RESET}")
    print(f"{RED}  Base   : {base_prob:.1f}%   ->   Live: {live_prob:.1f}%{RESET}")
    print(f"{RED}  Delta  : {BOLD}-{delta:.1f} pp  (threshold {DELTA_THRESHOLD_PCT} pp){RESET}")
    print(f"{RED}{'=' * 64}{RESET}\n")

    # Big terminal banner so you can't miss it
    print(f"\n{RED}{'#' * 64}{RESET}")
    print(f"{RED}#{'BET ALERT':^62}#{RESET}")
    print(f"{RED}#  Player : {player:<50}#{RESET}")
    print(f"{RED}#  Was    : {base_prob:.0f}% chance to win{'':<38}#{RESET}")
    print(f"{RED}#  Now    : {live_prob:.0f}% — dropped {delta:.0f}pp{'':<36}#{RESET}")
    print(f"{RED}#  Bet    : OPPONENT of {player:<38}#{RESET}")
    print(f"{RED}{'#' * 64}{RESET}\n")

    if _dash:
        _dash.show_alert(player, match_key, delta, live_prob, base_prob, live_odds)
        _dash.root.after(0, _dash.root.lift)
        _dash.root.after(0, lambda: _dash.root.attributes("-topmost", True))


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

        new_flag = ""
        if key not in baselines:
            baselines[key] = {
                "p1_prob": prob1, "p2_prob": prob2,
                "p1_odds": o1,    "p2_odds": o2,
                "seen_at": ts,
            }
            new_flag = f"  {GREEN}[NEW BASELINE]{RESET}"

        base = baselines[key]
        d1   = base["p1_prob"] - prob1
        d2   = base["p2_prob"] - prob2

        def delta_col(d):
            if d >= DELTA_THRESHOLD_PCT:          return RED
            if d >= DELTA_THRESHOLD_PCT * 0.55:   return YELLOW
            return GREEN

        print(f"\n  {BOLD}{p1}{RESET} vs {BOLD}{p2}{RESET}"
              f"  Sets:{m.get('sets','?')}  Vig:{vig:.2f}%{new_flag}")
        print(f"  {'Player':<22} {'Odds':>7}  {'Impl%':>7}  {'Base%':>7}  {'Delta':>9}")
        print(f"  {'─'*22}  {'─'*7}  {'─'*7}  {'─'*7}  {'─'*9}")
        print(f"  {p1:<22} {o1:>+7d}  {prob1:>6.1f}%  {base['p1_prob']:>6.1f}%  "
              f"{delta_col(d1)}{d1:>+8.1f}pp{RESET}")
        print(f"  {p2:<22} {o2:>+7d}  {prob2:>6.1f}%  {base['p2_prob']:>6.1f}%  "
              f"{delta_col(d2)}{d2:>+8.1f}pp{RESET}")

        if d1 >= DELTA_THRESHOLD_PCT:
            fire_alert(key, p1, d1, prob1, base["p1_prob"], o1)
        if d2 >= DELTA_THRESHOLD_PCT:
            fire_alert(key, p2, d2, prob2, base["p2_prob"], o2)

        dashboard_rows.append({
            "key":      key,
            "p1":       p1, "p2": p2,
            "sets":     m.get("sets", "?-?"),
            "p1_odds":  o1, "p2_odds": o2,
            "p1_prob":  prob1, "p2_prob": prob2,
            "base_p1":  base["p1_prob"], "base_p2": base["p2_prob"],
            "d1": d1,  "d2": d2,
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
