"""
Author: Paul R. Charovkine <krypdoh@gmail.com>
Program: NFL-TCKR.py
Date: 2026.0927
Copyright: 2026 Paul R. Charovkine

Description:
NFL ticker application that displays live football game data in a scrolling
ticker bar — logos, colored names, scores, down & distance, last play, QB
stats, ball-on, and possession. Data via ESPN public site API. Integrates with
Windows AppBar for docked desktop reservation (same model as MLB-TCKR).
"""

VERSION = "0.1.23"

import ctypes
from ctypes import wintypes
import json
import math
import os
import re
import sys
import time
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from PyQt5 import QtWidgets, QtCore, QtGui

# ---------------------------------------------------------------------------
# Windows AppBar (desktop reservation) — mirrored from MLB-TCKR
# ---------------------------------------------------------------------------
ABM_NEW = 0x00000000
ABM_REMOVE = 0x00000001
ABM_QUERYPOS = 0x00000002
ABM_SETPOS = 0x00000003
ABM_ACTIVATE = 0x00000006
ABM_WINDOWPOSCHANGED = 0x00000009
ABE_TOP = 1
WM_APPBAR = 0x0405  # AppBar notification (WM_USER + 5)
ABN_POSCHANGED = 0x00000001


class APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uCallbackMessage", wintypes.UINT),
        ("uEdge", wintypes.UINT),
        ("rc", wintypes.RECT),
        ("lParam", wintypes.LPARAM),
    ]

# GUI wake events — VBlank driver posts these (MLB pattern; no QueuedConnection
# from a run()-only QThread, which can freeze scrolling via startTimer).
_GUI_WAKE_EVENT_TYPE = QtCore.QEvent.Type(QtCore.QEvent.registerEventType())
_GUI_WAKE_SCROLL = 6
_GUI_WAKE_VBLANK_FAIL = 7


class _GuiWakeEvent(QtCore.QEvent):
    def __init__(self, code):
        super().__init__(_GUI_WAKE_EVENT_TYPE)
        self.code = code


class _VBlankDriver(QtCore.QThread):
    """Wake the GUI once per hardware VBlank via DwmFlush() (Windows DWM).

    True vsync without OpenGL. On failure (non-Windows / RDP / compositor
    down), posts a fail wake so the ticker starts the QTimer fallback.
    """

    def __init__(self, notify_target=None):
        super().__init__()
        self._running = True
        self._notify_target = notify_target

    def stop(self):
        self._running = False

    def _wake(self, code):
        t = self._notify_target
        if t is None:
            return
        if code == _GUI_WAKE_SCROLL:
            if getattr(t, "_vblank_scroll_pending", False):
                return
            t._vblank_scroll_pending = True
        prio = (
            QtCore.Qt.HighEventPriority
            if code == _GUI_WAKE_SCROLL
            else QtCore.Qt.NormalEventPriority
        )
        QtCore.QCoreApplication.postEvent(t, _GuiWakeEvent(code), prio)

    def run(self):
        exit_reason = None
        try:
            dwmapi = ctypes.WinDLL("dwmapi")
            fail_streak = 0
            while self._running:
                result = dwmapi.DwmFlush()
                if result != 0:
                    fail_streak += 1
                    if fail_streak >= 5:
                        exit_reason = f"DwmFlush HRESULT 0x{result & 0xFFFFFFFF:08X}"
                        break
                    time.sleep(0.016)
                    continue
                fail_streak = 0
                if self._running:
                    self._wake(_GUI_WAKE_SCROLL)
        except Exception as exc:
            exit_reason = f"{type(exc).__name__}: {exc}"
        if exit_reason and self._running:
            t = self._notify_target
            if t is not None:
                t._vblank_fail_reason = exit_reason
                self._wake(_GUI_WAKE_VBLANK_FAIL)

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    APP_DIR = sys._MEIPASS
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(APP_DIR)
APPDATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "NFL-TCKR")
SETTINGS_FILE = os.path.join(APPDATA_DIR, "NFL-TCKR.Settings.json")
LOGO_DIR = os.path.join(APP_DIR, "logos")
IMAGES_DIR = os.path.join(APP_DIR, "images")
FOOTBALL_ICON_PATH = os.path.join(IMAGES_DIR, "football-icon.jpg")
NFL_COM_LOGO_PATH = os.path.join(LOGO_DIR, "nfl-com-logo.png")

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"

QB_ROTATE_MS = 5000
QB_MIN_ATTEMPTS = 4  # proxy for "snaps" — ESPN boxscore has no snap count here
INTRO_HOLD_MS = 3000  # centered yellow title before the first scroll
SCORE_ALERT_HOLD_MS = 6000  # hold phase duration for scoring flash
SCORE_ALERT_IN_MS = 600
SCORE_ALERT_OUT_MS = 400
REQUEST_TIMEOUT = 20
USER_AGENT = "NFL-TCKR/0.1 (+https://github.com/krypdoh/MLB-TCKR)"

# Console fetch diagnostics. Default ON; set NFL_TCKR_DEBUG=0 to silence.
# Mirrors MLB-TCKR's MLB_TCKR_VERBOSE / MLB_TCKR_DEBUG env style.
_NFL_DEBUG = os.environ.get("NFL_TCKR_DEBUG", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

# Scroll judder profiler. Default OFF; enable with:  python NFL-TCKR.py --debug
# Or NFL_TCKR_SCROLL_DEBUG=1. NFL_TCKR_SCROLL_DEBUG=0 forces off.
SCROLL_DEBUG = False
_cli_debug = "--debug" in sys.argv
_cli_faststart = "--faststart" in sys.argv
sys.argv = [a for a in sys.argv if a not in ("--debug", "--faststart")]
_env_scroll = os.environ.get("NFL_TCKR_SCROLL_DEBUG", "").strip().lower()
if _env_scroll in ("0", "false", "no", "off"):
    _NFL_SCROLL_DEBUG = False
elif _cli_debug or _env_scroll in ("1", "true", "yes", "on") or SCROLL_DEBUG:
    _NFL_SCROLL_DEBUG = True
else:
    _NFL_SCROLL_DEBUG = False


def _dbg(msg):
    if not _NFL_DEBUG:
        return
    try:
        print(f"[DEBUG] {msg}", flush=True)
    except UnicodeEncodeError:
        print(
            f"[DEBUG] {msg}".encode("ascii", "replace").decode("ascii"),
            flush=True,
        )


def _scroll_dbg(msg):
    if not _NFL_SCROLL_DEBUG:
        return
    try:
        print(f"[SCROLL] {msg}", flush=True)
    except UnicodeEncodeError:
        print(
            f"[SCROLL] {msg}".encode("ascii", "replace").decode("ascii"),
            flush=True,
        )


def _scroll_dbg_stats(vals):
    """Return (min, avg, max, jitter, stdev) or Nones if empty."""
    if not vals:
        return None, None, None, None, None
    n = len(vals)
    mn = min(vals)
    mx = max(vals)
    avg = sum(vals) / n
    jitter = mx - mn
    if n < 2:
        return mn, avg, mx, jitter, 0.0
    var = sum((v - avg) ** 2 for v in vals) / (n - 1)
    return mn, avg, mx, jitter, math.sqrt(var)

# Nickname → primary / secondary / tertiary hex (NFL team color guide)
# Two-color teams get tertiary padded with #FFFFFF for the Tertiary slot.
NFL_TEAM_COLORS = {
    "Cardinals":   ["#97233F", "#000000", "#FFFFFF"],
    "Falcons":     ["#A71930", "#000000", "#A5ACAF"],
    "Ravens":      ["#241773", "#000000", "#9E7C0C"],
    "Bills":       ["#00338D", "#C60C30", "#FFFFFF"],
    "Panthers":    ["#0085CA", "#101820", "#BFC0BF"],
    "Bears":       ["#0B162A", "#C83803", "#FFFFFF"],
    "Bengals":     ["#FB4F14", "#000000", "#FFFFFF"],
    "Browns":      ["#311D00", "#FF3C00", "#FFFFFF"],
    "Cowboys":     ["#003594", "#869397", "#FFFFFF"],
    "Broncos":     ["#FB4F14", "#002244", "#FFFFFF"],
    "Lions":       ["#0076B6", "#B0B7BC", "#000000"],
    "Packers":     ["#203731", "#FFB612", "#FFFFFF"],
    "Texans":      ["#03202F", "#A71930", "#FFFFFF"],
    "Colts":       ["#002C5F", "#FFFFFF", "#FFFFFF"],
    "Jaguars":     ["#006778", "#101820", "#D7A22A"],
    "Chiefs":      ["#E31837", "#FFB81C", "#FFFFFF"],
    "Raiders":     ["#A5ACAF", "#000000", "#FFFFFF"],
    "Chargers":    ["#0080C6", "#FFC20E", "#FFFFFF"],
    "Rams":        ["#003594", "#FFA300", "#FFFFFF"],
    "Dolphins":    ["#008E97", "#FC4C02", "#FFFFFF"],
    "Vikings":     ["#4F2683", "#FFC62F", "#FFFFFF"],
    "Patriots":    ["#002244", "#C60C30", "#B0B7BC"],
    "Saints":      ["#D3BC8D", "#101820", "#FFFFFF"],
    "Giants":      ["#0B2265", "#A71930", "#FFFFFF"],
    "Jets":        ["#125740", "#FFFFFF", "#000000"],
    "Eagles":      ["#004C54", "#A5ACAF", "#000000"],
    "Steelers":    ["#101820", "#FFB612", "#FFFFFF"],
    "49ers":       ["#AA0000", "#B3995D", "#FFFFFF"],
    "Seahawks":    ["#002244", "#69BE28", "#A5ACAF"],
    "Buccaneers":  ["#D50A0A", "#34302B", "#FF7900"],
    "Titans":      ["#0C2340", "#4B92DB", "#C8102E"],
    "Commanders":  ["#5A1414", "#FFB612", "#FFFFFF"],
}

# Local logo filenames under logos/
NFL_LOGO_FILES = {
    "Cardinals":  "arizona-cardinals-logo.png",
    "Falcons":    "atlanta-falcons-logo.png",
    "Ravens":     "baltimore-ravens-logo.png",
    "Bills":      "buffalo-bills-logo.png",
    "Panthers":   "carolina-panthers-logo.png",
    "Bears":      "chicago-bears-logo.png",
    "Bengals":    "cincinnati-bengals-logo.png",
    "Browns":     "cleveland-browns-logo.png",
    "Cowboys":    "dallas-cowboys-logo.png",
    "Broncos":    "denver-broncos-logo.png",
    "Lions":      "detroit-lions-logo.png",
    "Packers":    "green-bay-packers-logo.png",
    "Texans":     "houston-texans-logo.png",
    "Colts":      "indianapolis-colts-logo.png",
    "Jaguars":    "jacksonville-jaguars-logo.png",
    "Chiefs":     "kansas-city-chiefs-logo.png",
    "Raiders":    "oakland-raiders-logo.png",
    "Chargers":   "los-angeles-chargers-logo.png",
    "Rams":       "los-angeles-rams-logo.png",
    "Dolphins":   "miami-dolphins-logo.png",
    "Vikings":    "minnesota-vikings-logo.png",
    "Patriots":   "new-england-patriots-logo.png",
    "Saints":     "new-orleans-saints-logo.png",
    "Giants":     "new-york-giants-logo.png",
    "Jets":       "new-york-jets-logo-2024.png",
    "Eagles":     "philadelphia-eagles-logo.png",
    "Steelers":   "pittsburgh-steelers-logo.png",
    "49ers":      "san-francisco-49ers-logo.png",
    "Seahawks":   "seattle-seahawks-logo.png",
    "Buccaneers": "tampa-bay-buccaneers-logo.png",
    "Titans":     "tennessee-titans-logo.png",
    "Commanders": "washington-commanders-logo.png",
}

NFL_CITY_ABBR = {
    "Cardinals": "ARI", "Falcons": "ATL", "Ravens": "BAL", "Bills": "BUF",
    "Panthers": "CAR", "Bears": "CHI", "Bengals": "CIN", "Browns": "CLE",
    "Cowboys": "DAL", "Broncos": "DEN", "Lions": "DET", "Packers": "GB",
    "Texans": "HOU", "Colts": "IND", "Jaguars": "JAX", "Chiefs": "KC",
    "Raiders": "LV", "Chargers": "LAC", "Rams": "LAR", "Dolphins": "MIA",
    "Vikings": "MIN", "Patriots": "NE", "Saints": "NO", "Giants": "NYG",
    "Jets": "NYJ", "Eagles": "PHI", "Steelers": "PIT", "49ers": "SF",
    "Seahawks": "SEA", "Buccaneers": "TB", "Titans": "TEN", "Commanders": "WSH",
}

_LOGO_CACHE = {}
_LOGO_INK_CACHE = {}
_FOOTBALL_CACHE = {}
_IMAGE_CACHE_LOCK = threading.Lock()
_FINAL_SUMMARY_CACHE = {}
_FINAL_SUMMARY_LOCK = threading.Lock()
# No live games: poll the scoreboard slowly. Near kickoff, keep the normal interval.
_IDLE_POLL_SECONDS = 60
_KICKOFF_SOON_SECONDS = 10 * 60
_SETTINGS_CACHE = None
_SETTINGS_LOCK = threading.Lock()


def _ensure_appdata():
    if not os.path.isdir(APPDATA_DIR):
        os.makedirs(APPDATA_DIR, exist_ok=True)


def get_settings():
    global _SETTINGS_CACHE
    with _SETTINGS_LOCK:
        if _SETTINGS_CACHE is not None:
            return _SETTINGS_CACHE
    defaults = {
        "speed": 5,
        "update_interval": 15,
        "ticker_height": 72,
        "game_spacing_percent": 100,  # 100% ≈ 1.8× ticker height between cards
        "font": "Ozone",
        "font_scale_percent": 160,
        "player_info_font": "Gotham Black",  # Match MLB-TCKR player/stat text
        "player_font_scale_percent": 75,  # Match MLB-TCKR default
        "show_team_cities": False,
        "show_city_only": False,
        "use_city_abbreviations": False,
        "include_final_games": True,
        "include_scheduled_games": True,
        "live_games_only": False,
        "show_last_play": True,
        "show_qb_stats": True,
        "show_ball_on": True,
        "show_possession": True,
        "led_background": True,
        "background_opacity": 230,
        "content_opacity": 255,  # scores / logos / strip (MLB-TCKR pattern)
        "glow_team_names": False,  # soft glow behind team names (team color)
        "glow_all": False,  # glow behind all content; logos use faint white
        "monitor_index": 0,
        "team_name_color_slot": 0,
        "team_colors": {},
        "docked": True,  # AppBar desktop reservation (False = floating always-on-top)
        "fullscreen_override_exes": [],  # EXE basenames that never hide the ticker
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            defaults.update(saved)
        except Exception:
            pass
    with _SETTINGS_LOCK:
        _SETTINGS_CACHE = defaults
        return _SETTINGS_CACHE


def save_settings(settings):
    global _SETTINGS_CACHE
    _ensure_appdata()
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        print(f"[SETTINGS] save failed: {e}")
    with _SETTINGS_LOCK:
        _SETTINGS_CACHE = settings


# ---------------------------------------------------------------------------
# Team helpers
# ---------------------------------------------------------------------------
def get_team_nickname(full_name):
    if not full_name:
        return ""
    for nick in NFL_TEAM_COLORS:
        if full_name.endswith(nick):
            return nick
    return full_name.split()[-1]


def get_team_city(full_name):
    nick = get_team_nickname(full_name)
    if full_name.endswith(nick):
        city = full_name[: -len(nick)].strip()
        return city or full_name
    parts = full_name.split()
    return " ".join(parts[:-1]) if len(parts) > 1 else full_name


def get_team_color(full_name, settings=None):
    """Return display color for a team name.

    Priority (matches MLB-TCKR):
      1. Per-team override from Team Colors tab (hex string or slot int 0/1/2)
      2. Global team_name_color_slot (0=primary, 1=secondary, 2=tertiary)
    """
    settings = settings or get_settings()
    nick = get_team_nickname(full_name)
    custom_colors = settings.get("team_colors", {})
    palette = NFL_TEAM_COLORS.get(nick, ["#FFFFFF", "#CCCCCC", "#888888"])

    if nick in custom_colors:
        val = custom_colors[nick]
        if isinstance(val, int):
            if 0 <= val < len(palette):
                return palette[val]
        elif isinstance(val, str) and val.startswith("#"):
            return val
        elif isinstance(val, str) and val:
            return val if val.startswith("#") else f"#{val}"

    slot = int(settings.get("team_name_color_slot", 0) or 0)
    if 0 <= slot < len(palette):
        return palette[slot]
    return palette[0]


def display_team_name(full_name, settings):
    nick = get_team_nickname(full_name)
    if settings.get("use_city_abbreviations", False):
        return NFL_CITY_ABBR.get(nick, nick)
    if settings.get("show_city_only", False):
        return get_team_city(full_name)
    if settings.get("show_team_cities", False):
        return full_name
    return nick


def _blank_image(w, h):
    img = QtGui.QImage(
        max(1, int(w)), max(1, int(h)), QtGui.QImage.Format_ARGB32_Premultiplied,
    )
    img.fill(0)
    return img


def get_team_logo(full_name, size=40):
    key = (full_name, size)
    with _IMAGE_CACHE_LOCK:
        cached = _LOGO_CACHE.get(key)
        if cached is not None:
            return cached
        nick = get_team_nickname(full_name)
        fname = NFL_LOGO_FILES.get(nick)
        path = os.path.join(LOGO_DIR, fname) if fname else None
        img = QtGui.QImage()
        if path and os.path.isfile(path):
            img = QtGui.QImage(path)
            if not img.isNull():
                img = img.scaled(size, size, QtCore.Qt.KeepAspectRatio,
                                 QtCore.Qt.SmoothTransformation)
        if img.isNull():
            img = _blank_image(size, size)
            p = QtGui.QPainter(img)
            color = QtGui.QColor(get_team_color(full_name))
            p.fillRect(0, 0, size, size, color)
            p.setPen(QtGui.QColor("#FFFFFF"))
            p.drawText(img.rect(), QtCore.Qt.AlignCenter, (nick or "?")[:3].upper())
            p.end()
        _LOGO_CACHE[key] = img
        _LOGO_INK_CACHE[key] = _opaque_bounds(img)
        return img


def _opaque_bounds(img, alpha_min=24):
    """Inclusive pixel box of visible ink. Full image if nothing is opaque."""
    if img is None or img.isNull():
        return 0, 0, 0, 0
    w, h = img.width(), img.height()
    min_x, min_y = w, h
    max_x, max_y = -1, -1
    for y in range(h):
        for x in range(w):
            if img.pixelColor(x, y).alpha() >= alpha_min:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
    if max_x < 0:
        return 0, 0, max(0, w - 1), max(0, h - 1)
    return min_x, min_y, max_x, max_y


def _logo_visual(img, cache_key=None):
    """left pad, right pad, top pad, bottom pad, ink width, ink height."""
    w = max(1, img.width()) if img is not None and not img.isNull() else 1
    hgt = max(1, img.height()) if img is not None and not img.isNull() else 1
    box = None
    if cache_key is not None:
        with _IMAGE_CACHE_LOCK:
            box = _LOGO_INK_CACHE.get(cache_key)
    if box is None:
        box = _opaque_bounds(img)
        if cache_key is not None:
            with _IMAGE_CACHE_LOCK:
                _LOGO_INK_CACHE[cache_key] = box
    left, top, right, bottom = box
    return (
        left,
        w - 1 - right,
        top,
        hgt - 1 - bottom,
        max(1, right - left + 1),
        max(1, bottom - top + 1),
    )


def _text_right_slack(fm, text, advance):
    """Empty px between the last glyph and the advance width."""
    br = fm.tightBoundingRect(text or "0")
    used = max(0, br.x() + br.width())
    return max(0, int(advance) - used)


def _text_left_slack(fm, text):
    """Empty px before the first glyph."""
    return max(0, fm.tightBoundingRect(text or "0").x())


def get_nfl_com_logo(height):
    """NFL shield from logos/nfl-com-logo.png, scaled to the ticker height."""
    key = ("nfl-com", int(height))
    with _IMAGE_CACHE_LOCK:
        cached = _LOGO_CACHE.get(key)
        if cached is not None:
            return cached
        img = QtGui.QImage()
        if os.path.isfile(NFL_COM_LOGO_PATH):
            img = QtGui.QImage(NFL_COM_LOGO_PATH)
            if not img.isNull():
                img = img.scaled(
                    int(height), int(height),
                    QtCore.Qt.KeepAspectRatio,
                    QtCore.Qt.SmoothTransformation,
                )
        if img.isNull():
            img = _blank_image(height, height)
            p = QtGui.QPainter(img)
            p.setPen(QtGui.QColor("#FFFFFF"))
            p.drawText(img.rect(), QtCore.Qt.AlignCenter, "NFL")
            p.end()
        _LOGO_CACHE[key] = img
        return img


def build_loop_marker_card(host):
    """Card at the start of each scroll loop: the NFL.com shield."""
    h = int(host.ticker_height)
    dpr = float(host.dpr)
    logo = get_nfl_com_logo(max(24, int(h * 0.88)))
    pad_x = max(12, int(h * 0.28))
    logo_w = logo.width()
    logo_h = logo.height()
    total_w = pad_x * 2 + logo_w
    image = QtGui.QImage(
        max(1, int(total_w * dpr)),
        max(1, int(h * dpr)),
        QtGui.QImage.Format_ARGB32_Premultiplied,
    )
    image.setDevicePixelRatio(dpr)
    image.fill(0)
    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
    painter.drawImage(pad_x, (h - logo_h) // 2, logo)
    painter.end()
    return image


def get_football_icon(size=14):
    key = size
    with _IMAGE_CACHE_LOCK:
        cached = _FOOTBALL_CACHE.get(key)
        if cached is not None:
            return cached
        path = FOOTBALL_ICON_PATH
        if not os.path.isfile(path):
            alt = os.path.join(LOGO_DIR, "football-icon.jpg")
            path = alt if os.path.isfile(alt) else path
        img = QtGui.QImage()
        if os.path.isfile(path):
            img = QtGui.QImage(path)
            if not img.isNull():
                img = img.scaled(size, size, QtCore.Qt.KeepAspectRatio,
                                 QtCore.Qt.SmoothTransformation)
        if img.isNull():
            img = _blank_image(size, size)
            p = QtGui.QPainter(img)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            p.setBrush(QtGui.QColor("#8B4513"))
            p.setPen(QtGui.QColor("#D2B48C"))
            p.drawEllipse(1, 1, size - 2, size - 2)
            p.end()
        _FOOTBALL_CACHE[key] = img
        return img


_RESOLVED_FONTS = {}
_SYSTEM_FONT_FAMILIES = None


def _bundled_file_hint(requested):
    """Filename in fonts/ whose stem matches requested, if any."""
    best = None
    best_score = 0
    for path in _bundled_font_files():
        stem = os.path.splitext(os.path.basename(path))[0].replace("-", " ").replace("_", " ")
        score = _font_name_score(requested, stem)
        if score > best_score:
            best_score = score
            best = os.path.basename(path)
            if score == 2:
                break
    return best


def _bundled_font_files():
    """ttf/otf files shipped in the project fonts folder."""
    seen = set()
    folders = (
        os.path.join(APP_DIR, "fonts"),
        os.path.join(REPO_ROOT, "fonts"),
        os.path.join(REPO_ROOT, "docs"),
    )
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith((".ttf", ".otf", ".ttc")):
                continue
            path = os.path.join(folder, name)
            key = os.path.normcase(os.path.abspath(path))
            if key in seen or not os.path.isfile(path):
                continue
            seen.add(key)
            yield path


def _font_name_score(requested, family):
    """2 = exact family, 1 = same face with a weight suffix, 0 = no match."""
    req = " ".join((requested or "").split()).lower()
    fam = " ".join((family or "").split()).lower()
    if not req or not fam:
        return 0
    if fam == req:
        return 2
    if req.startswith(fam + " ") or fam.startswith(req + " "):
        return 1
    return 0


def _load_bundled_family(requested):
    """Register a fonts-folder file whose family matches requested. None if none."""
    best_fid = -1
    best_family = None
    best_score = (0, 0)
    req_tokens = (requested or "").lower().split()
    db = QtGui.QFontDatabase
    for path in _bundled_font_files():
        fid = db.addApplicationFont(path)
        if fid < 0:
            continue
        score = (0, 0)
        family = None
        stem = os.path.splitext(os.path.basename(path))[0].lower().replace("-", " ").replace("_", " ")
        for fam in db.applicationFontFamilies(fid) or []:
            name_score = _font_name_score(requested, fam)
            if name_score <= 0:
                continue
            extra = [t for t in req_tokens if t not in fam.lower().split()]
            bonus = sum(1 for t in extra if t in stem)
            cand = (name_score, bonus)
            if cand > score:
                score = cand
                family = fam
        if family and score > best_score:
            if best_fid >= 0:
                db.removeApplicationFont(best_fid)
            best_fid = fid
            best_family = family
            best_score = score
            if score[0] == 2:
                break
        else:
            db.removeApplicationFont(fid)
    return best_family


def _load_bundled_font_file(filename):
    """Register one fonts-folder file by basename. Returns (family, source)."""
    want = os.path.basename(filename or "").lower()
    if not want:
        return None, None
    db = QtGui.QFontDatabase
    for path in _bundled_font_files():
        if os.path.basename(path).lower() != want:
            continue
        fid = db.addApplicationFont(path)
        if fid < 0:
            return None, None
        families = db.applicationFontFamilies(fid) or []
        if not families:
            return None, None
        return families[0], f"fonts/{os.path.basename(path)}"
    return None, None


def resolve_font_family(requested, default="Arial Black"):
    """Installed family, then a match in fonts/, then default.

    default is used as-is when it is already available, otherwise it is
    resolved the same way (installed, then fonts/).
    """
    requested = " ".join(str(requested or "").split())
    default = " ".join(str(default or "Arial Black").split()) or "Arial Black"
    key = (requested.lower(), default.lower())
    cached = _RESOLVED_FONTS.get(key)
    if cached:
        return cached[0]

    global _SYSTEM_FONT_FAMILIES
    if _SYSTEM_FONT_FAMILIES is None:
        # Captured once, before any fonts-folder file is registered.
        _SYSTEM_FONT_FAMILIES = set(QtGui.QFontDatabase().families())
    families = set(QtGui.QFontDatabase().families())

    def _pick(name, via_default):
        if not name:
            return None, None
        if name in _SYSTEM_FONT_FAMILIES or name in families:
            # families() also sees faces this process already loaded from fonts/.
            if name in _SYSTEM_FONT_FAMILIES:
                source = "default, installed" if via_default else "installed"
            else:
                source = "default, fonts folder" if via_default else "fonts folder"
            hint = _bundled_file_hint(name)
            if source.endswith("installed") and hint:
                source = f"{source}; fonts/{hint} also present, not used"
            return name, source
        loaded = _load_bundled_family(name)
        if loaded:
            families.add(loaded)
            source = "default, fonts folder" if via_default else "fonts folder"
            return loaded, source
        return None, None

    family, source = _pick(requested, False)
    if not family:
        family, source = _pick(default, True)
    if not family:
        family, source = "Arial", "fallback"
    _RESOLVED_FONTS[key] = (family, source)
    return family


def font_resolve_source(requested, default="Arial Black"):
    """How resolve_font_family found the face: installed, fonts folder, or default."""
    resolve_font_family(requested, default)
    requested = " ".join(str(requested or "").split())
    default = " ".join(str(default or "Arial Black").split()) or "Arial Black"
    return _RESOLVED_FONTS[(requested.lower(), default.lower())][1]


def _font_debug_desc(font):
    info = QtGui.QFontInfo(font)
    style = (info.styleName() or "").strip()
    face = info.family()
    if style and style.lower() not in face.lower():
        face = f"{face} {style}"
    weight = "bold" if font.bold() else "regular"
    return f"{face} {font.pixelSize()}px {weight} (draws {info.family()})"


def load_ticker_font():
    return resolve_font_family("Ozone", "Arial Black")


def load_player_font_family(fallback):
    """Player-stats face: installed Gotham Black, then fonts/, then fallback."""
    return resolve_font_family("Gotham Black", fallback)


def load_center_sans_family(player_family, ticker_family):
    """Heavy sans for the live down and play lines.

    Gotham Black when that family is installed (same resolve as the QB line).
    Otherwise the bundled Roboto Black face — the LED ticker font is not a
    stand-in for this block.
    """
    families = set(QtGui.QFontDatabase().families())
    gotham_ready = (
        "Gotham" in families
        or "Gotham Black" in families
        or (player_family and "Gotham" in player_family and player_family in families)
    )
    if gotham_ready:
        return _player_mixed_weight_family(player_family, ticker_family)
    for path in (
        os.path.join(REPO_ROOT, "fonts", "Roboto-Black.ttf"),
        os.path.join(APP_DIR, "fonts", "Roboto-Black.ttf"),
    ):
        if not os.path.isfile(path):
            continue
        fid = QtGui.QFontDatabase.addApplicationFont(path)
        if fid < 0:
            continue
        fams = QtGui.QFontDatabase.applicationFontFamilies(fid)
        if fams:
            return fams[0]
    return ticker_family


def _player_mixed_weight_family(player_family, fallback):
    """Base typeface for QB player-stats text (resolves to Gotham Black via heavy).

    Display-only families like 'Gotham Black' have no in-family style variants;
    map those to the base family so _qfont_weight_variant(..., 'heavy') can pick
    the Black sibling display face for both full-size and label-size fonts.
    """
    families = set(QtGui.QFontDatabase().families())
    # Separate ultra-weight faces → parent family with real Bold/Book styles
    base_map = {
        "Gotham Black": "Gotham",
        "Gotham Ultra": "Gotham",
        "Gotham Thin": "Gotham",
        "Gotham XLight": "Gotham",
    }
    mapped = base_map.get(player_family or "")
    if mapped and mapped in families:
        return mapped
    if player_family and player_family in families:
        return player_family
    return fallback


def _qfont_weight_variant(family, pixel_size, weight="regular"):
    """QFont at pixel_size: heavy / bold / regular faces.

    weight:
      - 'heavy' — Black/ExtraBold/Heavy (QB line: both full-size and labels);
        may use sibling display families like 'Gotham Black'
      - 'bold'  — SemiBold / Medium (legacy / non-QB); Bold only as fallback
      - 'regular' — Book/Regular (non-QB small text)

    Prefer QFontDatabase.font(family, style) — on Windows, QFont.setStyleName()
    often reports the style name but still renders the Book face. When a heavier
    face is missing, return the best available; callers may faux-bold via 1px
    double-draw if needed (QB line currently uses size, not weight, contrast).
    """
    db = QtGui.QFontDatabase()
    families = set(db.families())

    def _from_styles(fam, style_names):
        fam_styles = set(db.styles(fam) or [])
        for style in style_names:
            if style in fam_styles:
                font = db.font(fam, style, -1)
                font.setPixelSize(pixel_size)
                return font
        return None

    if weight == "heavy":
        # In-family heavy styles only — do not fall through to Bold here, or we
        # skip sibling display families like 'Gotham Black'.
        font = _from_styles(family, ("Black", "ExtraBold", "Heavy", "Ultra"))
        if font is not None:
            return font
        for suffix in (" Black", " ExtraBold", " Heavy", " Ultra"):
            cand = family + suffix
            if cand in families:
                font = _from_styles(cand, ("Regular", "Black", "Normal", "Book", "Bold"))
                if font is not None:
                    return font
        # No true heavy face — Bold then lighter; caller may faux-bold
        font = _from_styles(family, ("Bold", "Medium", "Book", "Regular", "Normal"))
        if font is not None:
            return font
    elif weight == "bold":
        # Labels: prefer SemiBold / Medium over full Bold when available
        font = _from_styles(
            family,
            ("SemiBold", "Semi Bold", "Medium", "Bold", "Book", "Regular", "Normal"),
        )
        if font is not None:
            return font
    else:
        font = _from_styles(family, ("Book", "Regular", "Normal", "Medium"))
        if font is not None:
            return font

    font = QtGui.QFont(family)
    font.setPixelSize(pixel_size)
    font.setBold(False)
    font.setWeight(QtGui.QFont.Normal)
    return font


def _needs_faux_bold(bold_font, regular_font):
    """True when bold_font is not actually heavier (single-weight / failed resolve).

    Sibling display families (e.g. Gotham Black vs Gotham Bold) often report
    misleading OS weight numbers; a different family name means a real heavy face.
    """
    bi = QtGui.QFontInfo(bold_font)
    ri = QtGui.QFontInfo(regular_font)
    if bi.family() != ri.family():
        return False
    return bi.weight() <= ri.weight()


# ---------------------------------------------------------------------------
# ESPN fetch
# ---------------------------------------------------------------------------
def _endpoint_label(url, params=None):
    """Short label for debug lines (not full URL spam)."""
    if "scoreboard" in url:
        label = "scoreboard"
    elif "summary" in url:
        label = "summary"
    else:
        label = url.rsplit("/", 1)[-1] or url
    if params:
        bits = [f"{k}={v}" for k, v in params.items()]
        label = f"{label}?{'&'.join(bits)}"
    return label


def _abbrev_scoreboard(data):
    events = data.get("events") or []
    lines = [f"events={len(events)}"]
    for e in events[:20]:
        st = ((e.get("status") or {}).get("type") or {})
        state = st.get("state") or "?"
        detail = st.get("shortDetail") or st.get("description") or ""
        comp = (e.get("competitions") or [{}])[0]
        away = home = "?"
        ascore = hscore = "-"
        for c in comp.get("competitors") or []:
            abbr = (c.get("team") or {}).get("abbreviation") or "?"
            if c.get("homeAway") == "home":
                home, hscore = abbr, c.get("score") or "0"
            else:
                away, ascore = abbr, c.get("score") or "0"
        sit = comp.get("situation") or {}
        dd = sit.get("downDistanceText") or sit.get("shortDownDistanceText") or ""
        poss = sit.get("possessionText") or sit.get("possession") or ""
        lp = ""
        last = sit.get("lastPlay")
        if isinstance(last, dict):
            lp = (last.get("text") or "")[:40]
        elif isinstance(last, str):
            lp = last[:40]
        extra = []
        if dd:
            extra.append(dd)
        if poss:
            extra.append(f"ball@{poss}")
        if lp:
            extra.append(f"lp={lp!r}")
        extra_s = (" | " + "; ".join(extra)) if extra else ""
        lines.append(
            f"  {e.get('id')} {away}@{home} {ascore}-{hscore} {state}"
            f"{(' ' + detail) if detail and state != 'post' else ''}{extra_s}"
        )
    if len(events) > 20:
        lines.append(f"  ... +{len(events) - 20} more")
    return "\n".join(lines)


def _abbrev_summary(data, event_id=""):
    header = data.get("header") or {}
    comps = header.get("competitions") or [{}]
    st = ((comps[0].get("status") or {}).get("type") or {}) if comps else {}
    state = st.get("state") or "?"
    sit = (comps[0].get("situation") or {}) if comps else {}
    dd = sit.get("downDistanceText") or ""
    poss = sit.get("possessionText") or sit.get("possession") or ""
    lp = ""
    last = sit.get("lastPlay")
    if isinstance(last, dict):
        lp = (last.get("text") or "")[:50]
    players = (data.get("boxscore") or {}).get("players") or []
    qb_bits = []
    for block in players:
        abbr = (block.get("team") or {}).get("abbreviation") or "?"
        n_pass = 0
        for cat in block.get("statistics") or []:
            if (cat.get("name") or "").lower() == "passing":
                n_pass = len(cat.get("athletes") or [])
        qb_bits.append(f"{abbr}:{n_pass}")
    return (
        f"event={event_id or header.get('id') or '?'} state={state} "
        f"down={dd or '-'} ball={poss or '-'} "
        f"lp={lp!r} box_passers=[{', '.join(qb_bits) or 'none'}]"
    )


def _abbrev_game(g):
    """One-line parsed-game digest for console."""
    aq = [q.get("line", "") for q in (g.get("away_qbs") or [])]
    hq = [q.get("line", "") for q in (g.get("home_qbs") or [])]
    scope = "game" if g.get("state") in ("in", "post") else "season"
    lp = (g.get("last_play") or "")[:40]
    return (
        f"{g.get('game_id')} {g.get('away_abbr')}@{g.get('home_abbr')} "
        f"{g.get('away_score')}-{g.get('home_score')} {g.get('state')} "
        f"dd={g.get('down_distance') or '-'} {g.get('ball_on') or '-'} "
        f"poss={g.get('possession_id') or '-'} "
        f"lp={lp!r} qb[{scope}] "
        f"away={aq or ['-']} home={hq or ['-']}"
    )


def _http_get(url, params=None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    label = _endpoint_label(url, params)
    t0 = time.monotonic()
    try:
        r = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        ms = (time.monotonic() - t0) * 1000.0
        r.raise_for_status()
        data = r.json()
    except Exception as ex:
        ms = (time.monotonic() - t0) * 1000.0
        _dbg(f"GET {label} FAILED {ms:.0f}ms: {ex}")
        raise
    if "scoreboard" in url:
        _dbg(f"GET {label} -> {r.status_code} {ms:.0f}ms\n{_abbrev_scoreboard(data)}")
    elif "summary" in url:
        eid = (params or {}).get("event", "")
        _dbg(f"GET {label} -> {r.status_code} {ms:.0f}ms {_abbrev_summary(data, eid)}")
    else:
        keys = list(data.keys())[:12] if isinstance(data, dict) else type(data).__name__
        _dbg(f"GET {label} -> {r.status_code} {ms:.0f}ms keys={keys}")
    return data


def _ordinal_down(down):
    """1st–4th only. ESPN sends down=-1 when no down is active (timeout, kickoff)."""
    try:
        d = int(down)
    except (TypeError, ValueError):
        return ""
    return {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(d, "")


def _competitor_lines(competitor):
    """Points by period from ESPN linescores, in order (Q1, Q2, …)."""
    vals = []
    for row in (competitor or {}).get("linescores") or []:
        if isinstance(row, dict):
            val = row.get("displayValue")
            if val is None or val == "":
                val = row.get("value")
        else:
            val = row
        vals.append("" if val is None else str(val))
    return vals


def _period_header(index):
    if index < 4:
        return str(index + 1)
    if index == 4:
        return "OT"
    return f"{index - 3}OT"


def _break_heading(game):
    """Center heading when the period is over. Empty during live play.

    End of 1st / halftime / end of 3rd / final → END 1ST, HALFTIME, END 3RD, FINAL.
    """
    state = (game.get("state") or "").strip().lower()
    blob = " ".join([
        str(game.get("status_detail") or ""),
        str(game.get("status") or ""),
    ]).lower().replace(".", " ")
    blob = re.sub(r"\s+", " ", blob).strip()
    if state == "post" or re.search(r"\bfinal\b", blob):
        return "FINAL"
    if "halftime" in blob or "half time" in blob:
        return "HALFTIME"
    match = re.search(r"end(?:\s+of)?(?:\s+the)?\s+(1st|2nd|3rd|4th)", blob)
    if not match:
        return ""
    quarter = match.group(1).upper()
    if quarter == "2ND":
        return "HALFTIME"
    if quarter == "4TH":
        return "FINAL"
    return f"END {quarter}"


def _clock_seconds(status_detail):
    """Remaining seconds from a clock like '5:46 - 2nd', or None."""
    text = (status_detail or "").strip()
    match = re.search(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _clock_under_two_minutes(status_detail):
    """True when the shown clock is 2:00 or less in the 2nd, 4th, or OT."""
    text = (status_detail or "").strip()
    match = re.search(
        r"(?:(1st|2nd|3rd|4th|ot)\s+)?"
        r"(\d{1,2}):(\d{2})"
        r"(?:\s*-\s*(1st|2nd|3rd|4th|ot))?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return False
    quarter = (match.group(1) or match.group(4) or "").lower()
    if quarter not in ("2nd", "4th", "ot"):
        return False
    seconds = int(match.group(2)) * 60 + int(match.group(3))
    return seconds <= 2 * 60


def _clock_over_two_minutes(status_detail):
    """True when a parseable game clock is strictly over 2:00."""
    seconds = _clock_seconds(status_detail)
    return seconds is not None and seconds > 2 * 60


def _split_clock_line(text):
    """Split '5:46 - 2ND' into (time, dash, quarter). Missing parts are ''."""
    match = re.match(
        r"^(\d{1,2}:\d{2})(\s*-\s*)(.*)$",
        (text or "").strip(),
    )
    if not match:
        return (text or "", "", "")
    return match.group(1), match.group(2), match.group(3)


def _sit_color_segments(text):
    """Split a down line so '&' and '@' can be drawn white."""
    parts = []
    buf = []
    for ch in text or "":
        if ch in "&@":
            if buf:
                parts.append(("".join(buf), False))
                buf = []
            parts.append((ch, True))
        else:
            buf.append(ch)
    if buf:
        parts.append(("".join(buf), False))
    return parts


def _glyph_line(fm, text, fallback="A"):
    """Visual line height and baseline offset from the top of the glyphs."""
    sample = text or fallback
    br = fm.tightBoundingRect(sample)
    if br.width() <= 0 or br.height() <= 0:
        br = fm.boundingRect(sample)
    if br.height() <= 0:
        return fm.ascent() + fm.descent(), fm.ascent()
    ascent = -br.top() if br.top() < 0 else br.height()
    return br.height(), ascent


def _live_center_rows(height, time_m, sit_m, play_m, clock_text, situation_line,
                      play_lines, gap=1):
    """Fixed vertical slots for live clock / down / two play lines.

    The clock row is always the same, even when down or last play is empty.
    The four slots are centered in the bar as a group so the time sits in
    the upper half (above the score midline) without hugging the top edge.
    """
    clock_h, clock_a = _glyph_line(time_m, clock_text or "0:00 - 2ND")
    sit_h, sit_a = _glyph_line(sit_m, situation_line or "1st & 10 @ AAA 00")
    play_sample = (play_lines[0] if play_lines else "LAST PLAY")
    play_h, play_a = _glyph_line(play_m, play_sample)
    clock_to_sit = max(4, gap + 3)
    sit_to_play = max(3, gap + 2)
    play_to_play = gap
    reserved = (
        clock_h + sit_h + 2 * play_h
        + clock_to_sit + sit_to_play + play_to_play
    )
    y0 = max(2, (int(height) - reserved) // 2)
    sit_y = y0 + clock_h + clock_to_sit
    play_y = sit_y + sit_h + sit_to_play
    play2_y = play_y + play_h + play_to_play
    return {
        "time": (y0, clock_a),
        "sit": (sit_y, sit_a),
        "play": (play_y, play_a),
        "play2": (play2_y, play_a),
        "reserved": reserved,
    }


def _prepare_linescore(font, game, height, heading="", heading_font=None):
    """Quarter table for a final: header, away, home. None if ESPN sent no lines."""
    away_q = list(game.get("away_lines") or [])
    home_q = list(game.get("home_lines") or [])
    if not away_q and not home_q:
        return None
    n = max(len(away_q), len(home_q), 4)

    def _pad(vals):
        vals = list(vals) + [""] * (n - len(vals))
        return [str(v) for v in vals[:n]]

    rows = [
        [""] + [_period_header(i) for i in range(n)] + ["T"],
        [str(game.get("away_abbr") or "").upper()] + _pad(away_q) + [str(game.get("away_score") or "0")],
        [str(game.get("home_abbr") or "").upper()] + _pad(home_q) + [str(game.get("home_score") or "0")],
    ]
    face = QtGui.QFont(font)
    # Smaller than the down/play faces; the heading stays the larger line.
    px = max(8, int(height * 0.125))
    head_h = 0
    head_w = 0
    if heading and heading_font is not None:
        head_fm = QtGui.QFontMetrics(heading_font)
        head_h = head_fm.ascent() + head_fm.descent() + 2
        head_w = head_fm.horizontalAdvance(heading)
    while px > 7:
        face.setPixelSize(px)
        fm = QtGui.QFontMetrics(face)
        if head_h + 3 * (fm.ascent() + fm.descent()) + 2 <= height:
            break
        px -= 1
    fm = QtGui.QFontMetrics(face)
    gap = max(5, px // 2 + 1)
    cols = len(rows[0])
    col_w = []
    for c in range(cols):
        w = max(fm.horizontalAdvance(row[c]) for row in rows)
        if c > 0:
            w = max(w, fm.horizontalAdvance("00"))
        col_w.append(int(w))
    table_w = int(sum(col_w) + gap * (cols - 1))
    return {
        "font": face,
        "fm": fm,
        "rows": rows,
        "col_w": col_w,
        "gap": gap,
        "width": max(table_w, int(head_w)),
        "heading": heading,
        "heading_font": heading_font,
        "head_h": head_h,
    }


def _draw_linescore(painter, left, width, height, table, away_color, home_color):
    """Draw the break heading, then the quarter table underneath it."""
    fm = table["fm"]
    rows = table["rows"]
    col_w = table["col_w"]
    gap = table["gap"]
    line_h = fm.ascent() + fm.descent()
    table_h = 3 * line_h
    head_h = int(table.get("head_h") or 0)
    stack_h = head_h + table_h
    y0 = (height - stack_h) // 2
    heading = table.get("heading") or ""
    heading_font = table.get("heading_font")
    if heading and heading_font is not None and head_h:
        head_fm = QtGui.QFontMetrics(heading_font)
        painter.setFont(heading_font)
        tw = head_fm.horizontalAdvance(heading)
        painter.setPen(QtGui.QColor("#FFFFFF"))
        painter.drawText(
            int(left + (width - tw) // 2),
            int(y0 + head_fm.ascent()),
            heading,
        )
        y0 += head_h
    x0 = left + max(0, (width - (sum(col_w) + gap * (len(col_w) - 1))) // 2)
    painter.setFont(table["font"])
    colors = ("#9A9A9A", away_color, home_color)
    for r, row in enumerate(rows):
        baseline = y0 + r * line_h + fm.ascent()
        x = x0
        for c, text in enumerate(row):
            if not text:
                x += col_w[c] + gap
                continue
            if r == 0:
                fill = "#9A9A9A"
            elif c == 0:
                fill = colors[r]
            else:
                fill = "#FFFFFF"
            tw = fm.horizontalAdvance(text)
            tx = x if c == 0 else x + (col_w[c] - tw) // 2
            painter.setPen(QtGui.QColor(fill))
            painter.drawText(int(tx), int(baseline), text)
            x += col_w[c] + gap


def _timeout_remaining(value):
    """Timeouts left this half, clamped to 0–3. None when ESPN omits the field."""
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(3, n))


def _clean_down_distance(text):
    """Drop placeholder down text such as '-1th & 0'."""
    text = (text or "").strip()
    if not text:
        return ""
    if text.startswith("-"):
        return ""
    if re.match(r"^-?\d+th\b", text, flags=re.IGNORECASE):
        return ""
    if re.search(r"&\s*0\s*$", text) and "goal" not in text.lower():
        return ""
    return text


# Generational / suffix tokens kept with the surname (Beckham Jr., Cook III, …)
_NAME_SUFFIXES = frozenset({
    "jr", "jr.", "sr", "sr.",
    "ii", "iii", "iv", "v",
})


def _is_name_suffix(token):
    t = (token or "").strip().lower()
    return t in _NAME_SUFFIXES or t.rstrip(".") in ("jr", "sr")


def _is_name_initial(token):
    """True for 'O.' / 'J' style leading initials from shortName."""
    t = (token or "").strip()
    if not t:
        return False
    if t.endswith(".") and len(t) <= 3:
        return True
    return len(t) == 1 and t.isalpha()


def format_player_lastname(athlete_or_name):
    """Display surname including multi-part / suffix (e.g. 'Beckham Jr.', 'Penix Jr.').

    Prefers ESPN athlete ``lastName`` when present; otherwise derives from
    display/full/short name without dropping Jr/Sr/II–V.
    """
    if isinstance(athlete_or_name, dict):
        ln = (athlete_or_name.get("lastName") or "").strip()
        if ln:
            return ln
        full = (
            athlete_or_name.get("displayName")
            or athlete_or_name.get("fullName")
            or athlete_or_name.get("shortName")
            or ""
        )
    else:
        full = str(athlete_or_name or "")
    full = full.strip()
    if not full:
        return "Player"
    parts = full.split()
    # Drop leading initials: "O. Beckham Jr." → ["Beckham", "Jr."]
    while len(parts) > 1 and _is_name_initial(parts[0]):
        parts = parts[1:]
    if not parts:
        return "Player"
    if len(parts) >= 2 and _is_name_suffix(parts[-1]):
        # Avoid returning bare "Jr." — keep surname + suffix
        return f"{parts[-2]} {parts[-1]}"
    # MLB-style: 3+ tokens without formal suffix → last two (covers some multi-word surnames)
    if len(parts) > 2:
        return " ".join(parts[-2:])
    return parts[-1]


def _qb_last_name(athlete):
    return format_player_lastname(athlete if isinstance(athlete, dict) else {"displayName": athlete})


def _athlete_jersey(athlete):
    """Jersey number string from ESPN athlete dict, or ''."""
    if not isinstance(athlete, dict):
        return ""
    j = athlete.get("jersey") or athlete.get("jerseyNumber") or ""
    j = str(j).strip()
    return j


def _qb_line_segments(name, yds, td, inter, jersey=""):
    """Mixed-size parts for under-name QB stats (same face; labels 1px smaller).

    Format: #1 Ward 323 YDS, 1 TD, 0 INT
    Full size (True): #jersey, last name, yards, TD count, INT count.
    Label size (False): spaces and YDS/TD/INT labels (+ punctuation). No 'QB'.
    """
    yds = str(yds).replace(",", "")
    segs = []
    if jersey:
        segs.append((f"#{jersey}", True))
        segs.append((" ", False))
    segs.extend([
        (str(name), True),
        (" ", False),
        (str(yds), True),
        (" YDS, ", False),
        (str(td), True),
        (" TD, ", False),
        (str(inter), True),
        (" INT", False),
    ])
    return segs


def _format_qb_line(name, yds, td, inter, jersey=""):
    """Plain-text QB stats line (debug / visual-key); see _qb_line_segments for size."""
    return "".join(t for t, _ in _qb_line_segments(name, yds, td, inter, jersey))


def _qb_segments_width(segments, full_font, label_font, faux_bold=False):
    """Width of QB segments using full-size vs label (size-1) fonts."""
    if not segments:
        return 0
    full_m = QtGui.QFontMetrics(full_font)
    label_m = QtGui.QFontMetrics(label_font)
    total = 0
    for text, is_full in segments:
        if not text:
            continue
        total += (full_m if is_full else label_m).horizontalAdvance(text)
        if is_full and faux_bold:
            total += 1  # 1px double-draw expands the glyph
    return total


def _draw_mixed_text(painter, x, y, segments, full_font, label_font, fill_color,
                     glow=False, glow_color="#FFFFFF", faux_bold=False):
    """Draw sequential text segments with full-size vs label (smaller) fonts.

    Both fonts are expected to be the same face/weight (e.g. Gotham Black);
    label_font is typically 1px smaller. drawText uses baseline y so smaller
    labels sit on the same baseline as full-size glyphs.

    When faux_bold is True (legacy single-weight faces), full-size segments are
    drawn twice with a 1px horizontal offset so they read heavier.
    """
    cx = float(x)
    for text, is_full in segments:
        if not text:
            continue
        font = full_font if is_full else label_font
        painter.setFont(font)
        ix = int(round(cx))
        do_faux = bool(is_full and faux_bold)
        if glow:
            _draw_text_glow(painter, ix, y, text, fill_color, glow_color)
            if do_faux:
                _draw_text_glow(painter, ix + 1, y, text, fill_color, glow_color)
        else:
            painter.setPen(QtGui.QColor(fill_color))
            painter.drawText(ix, y, text)
            if do_faux:
                painter.drawText(ix + 1, y, text)
        cx += QtGui.QFontMetrics(font).horizontalAdvance(text)
        if do_faux:
            cx += 1


def _parse_catt(catt):
    """Parse '14/20' or '14-20' → (display '14-20', attempts int)."""
    if not catt:
        return "", 0
    sep = "/" if "/" in catt else ("-" if "-" in catt else None)
    if not sep:
        return str(catt), 0
    parts = catt.split(sep, 1)
    try:
        attempts = int(parts[1].replace(",", "").strip())
        return f"{parts[0].strip()}-{parts[1].strip()}", attempts
    except (ValueError, IndexError):
        return str(catt), 0


def _parse_passing_athletes(boxscore_players, team_id):
    """Game boxscore QBs with attempts >= QB_MIN_ATTEMPTS (live / final)."""
    qbs = []
    if not boxscore_players:
        return qbs
    for block in boxscore_players:
        tid = str((block.get("team") or {}).get("id") or "")
        if team_id and tid != str(team_id):
            continue
        for cat in block.get("statistics") or []:
            if (cat.get("name") or "").lower() != "passing":
                continue
            labels = cat.get("labels") or []

            def _idx(label):
                try:
                    return labels.index(label)
                except ValueError:
                    return -1

            for ath in cat.get("athletes") or []:
                stats = ath.get("stats") or []
                catt = stats[_idx("C/ATT")] if 0 <= _idx("C/ATT") < len(stats) else ""
                yds = stats[_idx("YDS")] if 0 <= _idx("YDS") < len(stats) else "0"
                td = stats[_idx("TD")] if 0 <= _idx("TD") < len(stats) else "0"
                inter = stats[_idx("INT")] if 0 <= _idx("INT") < len(stats) else "0"
                completes, attempts = _parse_catt(catt)
                if attempts < QB_MIN_ATTEMPTS:
                    continue
                athlete = ath.get("athlete") or {}
                last = _qb_last_name(athlete)
                jersey = _athlete_jersey(athlete)
                segs = _qb_line_segments(last, yds, td, inter, jersey)
                qbs.append({
                    "name": last,
                    "jersey": jersey,
                    "line": _format_qb_line(last, yds, td, inter, jersey),
                    "segments": segs,
                    "attempts": attempts,
                    "scope": "game",
                })
    return qbs


def _parse_season_leader_display(display_value):
    """Parse ESPN season leader text like '40/60, 582 YDS, 5 TD, 3 INT'."""
    text = (display_value or "").strip()
    completes, attempts, yds, td, inter = "", 0, "0", "0", "0"
    if not text:
        return completes, attempts, yds, td, inter
    # C/ATT at start
    m = re.match(r"^(\d+)\s*[/-]\s*(\d+)\s*,?\s*(.*)$", text)
    rest = text
    if m:
        completes = f"{m.group(1)}-{m.group(2)}"
        attempts = int(m.group(2))
        rest = m.group(3)
    y = re.search(r"([\d,]+)\s*YDS", rest, re.I)
    if y:
        yds = y.group(1).replace(",", "")
    t = re.search(r"(\d+)\s*TD", rest, re.I)
    if t:
        td = t.group(1)
    i = re.search(r"(\d+)\s*INT", rest, re.I)
    if i:
        inter = i.group(1)
    return completes, attempts, yds, td, inter


def _season_qbs_from_competitors(comp, team_id):
    """Pregame: season passing line from each competitor's passingLeader."""
    qbs = []
    for c in comp.get("competitors") or []:
        tid = str((c.get("team") or {}).get("id") or c.get("id") or "")
        if team_id and tid != str(team_id):
            continue
        for cat in c.get("leaders") or []:
            name = (cat.get("name") or "").lower()
            if name not in ("passingleader", "passingyards", "passing"):
                continue
            for lead in cat.get("leaders") or []:
                completes, attempts, yds, td, inter = _parse_season_leader_display(
                    lead.get("displayValue") or ""
                )
                # Season threshold: prefer real attempts; if missing C/ATT but has yards, keep
                if attempts and attempts < QB_MIN_ATTEMPTS:
                    continue
                if not attempts and not yds:
                    continue
                athlete = lead.get("athlete") or {}
                last = _qb_last_name(athlete)
                jersey = _athlete_jersey(athlete)
                segs = _qb_line_segments(last, yds, td, inter, jersey)
                qbs.append({
                    "name": last,
                    "jersey": jersey,
                    "line": _format_qb_line(last, yds, td, inter, jersey),
                    "segments": segs,
                    "attempts": attempts or QB_MIN_ATTEMPTS,
                    "scope": "season",
                })
    return qbs


def _situation_fields(situation, status_type):
    """Extract down/distance, ball-on, last play, possession from ESPN situation."""
    out = {
        "down_distance": "",
        "ball_on": "",
        "last_play": "",
        "possession_id": "",
        "clock": "",
        "period": "",
        "away_timeouts": None,
        "home_timeouts": None,
    }
    if not situation:
        return out
    dd = (situation.get("downDistanceText")
          or situation.get("shortDownDistanceText")
          or "")
    if not _clean_down_distance(dd):
        down = situation.get("down")
        dist = situation.get("distance")
        ordinal = _ordinal_down(down)
        try:
            dist_n = int(dist)
        except (TypeError, ValueError):
            dist_n = 0
        dd = f"{ordinal} & {dist_n}" if ordinal and dist_n > 0 else ""
    out["down_distance"] = _clean_down_distance(dd)

    # Raw yard spot ("NYG 45"); the card composes "2nd & 22 @ NYG 45".
    out["ball_on"] = (situation.get("possessionText") or "").strip()
    out["possession_id"] = str(situation.get("possession") or "")
    out["away_timeouts"] = _timeout_remaining(situation.get("awayTimeouts"))
    out["home_timeouts"] = _timeout_remaining(situation.get("homeTimeouts"))

    last = situation.get("lastPlay") or {}
    if isinstance(last, dict):
        out["last_play"] = (last.get("text") or last.get("description") or "").strip()
    elif isinstance(last, str):
        out["last_play"] = last.strip()

    # Clock / period from status when available
    detail = (status_type or {}).get("shortDetail") or (status_type or {}).get("detail") or ""
    out["clock"] = detail
    return out


def _live_situation_line(down, ball_on):
    """Down and spot for the live center, e.g. '2nd & 22 @ NYG 45'.

    ESPN downDistanceText often already ends with 'at NYG 47'. Drop that
    clause and keep the '@ NYG 47' spot.
    """
    down = (down or "").strip()
    spot = (ball_on or "").strip()
    if spot.lower().startswith("ball on "):
        spot = spot[8:].strip()
    if down and spot:
        down = re.sub(r"\s+at\s+.+$", "", down, count=1, flags=re.IGNORECASE).strip()
        return f"{down} @ {spot}" if down else f"@ {spot}"
    if down:
        return down
    if spot:
        return f"@ {spot}"
    return ""


def _last_play_from_drives(summary):
    drives = (summary or {}).get("drives") or {}
    current = drives.get("current")
    previous = drives.get("previous") or []
    for drive in ([current] if current else []) + list(reversed(previous)):
        if not drive:
            continue
        plays = drive.get("plays") or []
        for play in reversed(plays):
            text = (play.get("text") or "").strip()
            if text and text.upper() not in ("END GAME", "END OF GAME", "END QUARTER", "END OF QUARTER"):
                return text
    return ""


def _short_player_name(full):
    """Surname (+ suffix) from a free-text player name in play descriptions."""
    return format_player_lastname(full)


def _extract_scoring_plays(summary):
    """Lightweight list from ESPN summary.scoringPlays for alert detection."""
    out = []
    for sp in (summary or {}).get("scoringPlays") or []:
        pid = str(sp.get("id") or "").strip()
        if not pid:
            continue
        team = sp.get("team") or {}
        out.append({
            "id": pid,
            "text": (sp.get("text") or "").strip(),
            "type_text": ((sp.get("type") or {}).get("text") or "").strip(),
            "scoring_name": ((sp.get("scoringType") or {}).get("name") or "").strip(),
            "team_id": str(team.get("id") or ""),
            "team_full": team.get("displayName") or "",
            "team_abbr": team.get("abbreviation") or "",
        })
    return out


def format_scoring_alert_message(play, team_label):
    """Build MLB-style flash text, e.g. 'Giants Score: PASS Beckham Jr. 44yd Touchdown!'."""
    label = (team_label or "TEAM").strip()
    if label:
        label = label[0].upper() + label[1:] if len(label) > 1 else label.upper()
    # Prefer nickname casing like examples (Giants, not GIANTS) — title-case nickname
    nick = get_team_nickname(play.get("team_full") or "") or label
    label = nick

    raw = play.get("text") or ""
    type_text = (play.get("type_text") or "").lower()
    scoring = (play.get("scoring_name") or "").lower()
    main = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip() or raw

    if "field goal" in type_text or scoring in ("field-goal", "fieldgoal"):
        m = re.match(r"^(.+?)\s+(\d+)\s*Yds?\s+Field Goal", main, re.I)
        if m:
            return f"{label} Score: FIELD GOAL {_short_player_name(m.group(1))} {m.group(2)} yd"
        return f"{label} Score: FIELD GOAL {main}"

    if "pass" in type_text or re.search(r"\bpass from\b", main, re.I):
        m = re.match(r"^(.+?)\s+(\d+)\s*Yds?\s+pass\s+from\s+(.+)$", main, re.I)
        if m:
            return (
                f"{label} Score: PASS {_short_player_name(m.group(1))} "
                f"{m.group(2)}yd Touchdown!"
            )
        return f"{label} Score: PASS {main} Touchdown!"

    if "rush" in type_text or re.search(r"\bRush\b", main):
        m = re.match(r"^(.+?)\s+(\d+)\s*Yds?\s+Rush", main, re.I)
        if m:
            return (
                f"{label} Score: RUN {_short_player_name(m.group(1))} "
                f"{m.group(2)}yd Touchdown!"
            )
        return f"{label} Score: RUN {main} Touchdown!"

    if "safety" in type_text or scoring == "safety":
        return f"{label} Score: SAFETY"

    if "two-point" in type_text or "two point" in main.lower() or scoring in (
        "two-point-conversion", "2pt",
    ):
        return f"{label} Score: 2PT {main}"

    if "extra point" in type_text or scoring in ("extra-point", "pat"):
        m = re.match(r"^(.+?)\s+Kick$", main, re.I)
        if m:
            return f"{label} Score: XP {_short_player_name(m.group(1))}"
        return f"{label} Score: XP {main}"

    if scoring == "touchdown" or "touchdown" in type_text:
        return f"{label} Score: {main} Touchdown!"

    return f"{label} Score: {main}"


def _parse_event(event, summary=None):
    comp = (event.get("competitions") or [{}])[0]
    status = event.get("status") or comp.get("status") or {}
    stype = status.get("type") or {}
    state = stype.get("state") or "pre"  # pre / in / post
    status_name = stype.get("description") or stype.get("name") or state

    away = home = None
    for c in comp.get("competitors") or []:
        if c.get("homeAway") == "home":
            home = c
        else:
            away = c
    if not away or not home:
        return None

    def _team_bits(c):
        t = c.get("team") or {}
        loc = t.get("location") or ""
        name = t.get("name") or t.get("shortDisplayName") or ""
        full = t.get("displayName") or f"{loc} {name}".strip()
        records = c.get("records") or []
        rec = ""
        for r in records:
            if r.get("type") == "total" or r.get("name") == "overall":
                rec = r.get("summary") or ""
                break
        if not rec and records:
            rec = records[0].get("summary") or ""
        return {
            "id": str(t.get("id") or c.get("id") or ""),
            "abbr": t.get("abbreviation") or "",
            "full": full,
            "nick": name or get_team_nickname(full),
            "city": loc or get_team_city(full),
            "score": c.get("score") or "0",
            "record": rec,
            "lines": _competitor_lines(c),
        }

    a = _team_bits(away)
    h = _team_bits(home)
    board_situation = comp.get("situation") or {}
    situation = board_situation
    if summary:
        header_comps = ((summary.get("header") or {}).get("competitions") or [])
        if header_comps and header_comps[0].get("situation"):
            situation = dict(header_comps[0].get("situation") or {})
            for key in ("homeTimeouts", "awayTimeouts"):
                if situation.get(key) is None and board_situation.get(key) is not None:
                    situation[key] = board_situation.get(key)

    sit = _situation_fields(situation, stype)
    if not sit["last_play"] and summary:
        sit["last_play"] = _last_play_from_drives(summary)

    # For finals: center "F", never show last-play text under the score
    if state == "post":
        sit["down_distance"] = "F"
        sit["last_play"] = ""
        sit["ball_on"] = ""
        sit["possession_id"] = ""
        sit["away_timeouts"] = None
        sit["home_timeouts"] = None

    # Kickoff time for scheduled
    start = comp.get("date") or event.get("date") or ""
    kickoff_local = ""
    if start:
        try:
            # ESPN dates are ISO UTC
            dt = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
            local = dt.astimezone()
            kickoff_local = local.strftime("%I:%M %p").lstrip("0")
        except Exception:
            kickoff_local = stype.get("shortDetail") or ""

    # QB stats:
    #   live / final  → this game's boxscore passing (summary)
    #   pregame only  → season passingLeader on each competitor
    away_qbs = home_qbs = []
    if state in ("in", "post") and summary:
        players = (summary.get("boxscore") or {}).get("players") or []
        away_qbs = _parse_passing_athletes(players, a["id"])
        home_qbs = _parse_passing_athletes(players, h["id"])
    elif state == "pre":
        away_qbs = _season_qbs_from_competitors(comp, a["id"])
        home_qbs = _season_qbs_from_competitors(comp, h["id"])
        # Fallback: competition-level leaders if a side had none
        if not away_qbs or not home_qbs:
            for leader_cat in comp.get("leaders") or []:
                if (leader_cat.get("name") or "") != "passingYards":
                    continue
                for lead in leader_cat.get("leaders") or []:
                    ath = lead.get("athlete") or {}
                    team = lead.get("team") or {}
                    tid = str(team.get("id") or (ath.get("team") or {}).get("id") or "")
                    completes, attempts, yds, td, inter = _parse_season_leader_display(
                        lead.get("displayValue") or ""
                    )
                    if attempts and attempts < QB_MIN_ATTEMPTS:
                        continue
                    last = _qb_last_name(ath)
                    jersey = _athlete_jersey(ath)
                    segs = _qb_line_segments(last, yds, td, inter, jersey)
                    entry = {
                        "name": last,
                        "jersey": jersey,
                        "line": _format_qb_line(last, yds, td, inter, jersey),
                        "segments": segs,
                        "attempts": attempts or QB_MIN_ATTEMPTS,
                        "scope": "season",
                    }
                    if tid == a["id"] and not away_qbs:
                        away_qbs.append(entry)
                    elif tid == h["id"] and not home_qbs:
                        home_qbs.append(entry)

    scoring_plays = _extract_scoring_plays(summary) if summary else []

    return {
        "game_id": str(event.get("id") or comp.get("id") or ""),
        "state": state,  # pre / in / post
        "status": status_name,
        "status_detail": stype.get("shortDetail") or stype.get("detail") or "",
        "away_name": a["full"],
        "home_name": h["full"],
        "away_id": a["id"],
        "home_id": h["id"],
        "away_abbr": a["abbr"],
        "home_abbr": h["abbr"],
        "away_score": a["score"],
        "home_score": h["score"],
        "away_lines": a["lines"],
        "home_lines": h["lines"],
        "away_record": a["record"],
        "home_record": h["record"],
        "away_qbs": away_qbs,
        "home_qbs": home_qbs,
        "down_distance": sit["down_distance"],
        "ball_on": sit["ball_on"],
        "last_play": sit["last_play"],
        "possession_id": sit["possession_id"],
        "away_timeouts": sit["away_timeouts"],
        "home_timeouts": sit["home_timeouts"],
        "kickoff": kickoff_local,
        "start": start,
        "scoring_plays": scoring_plays,
    }


def fetch_nfl_games(settings=None):
    """Fetch scoreboard; enrich in-progress (and finals) with summary for QB/last play."""
    settings = settings or get_settings()
    data = _http_get(ESPN_SCOREBOARD)
    events = data.get("events") or []
    if not events:
        return []

    # Live games need a fresh summary every poll (QB line, last play).
    # Finals are fetched once and reused. Scheduled games stay on the scoreboard.
    need_summary = []
    for e in events:
        st = ((e.get("status") or {}).get("type") or {}).get("state")
        eid = str(e.get("id"))
        if st == "in":
            need_summary.append(eid)
        elif st == "post":
            with _FINAL_SUMMARY_LOCK:
                if eid not in _FINAL_SUMMARY_CACHE:
                    need_summary.append(eid)

    summaries = {}
    if need_summary:
        def _one(eid):
            try:
                return eid, _http_get(ESPN_SUMMARY, params={"event": eid})
            except Exception as ex:
                print(f"[ESPN] summary {eid} failed: {ex}")
                return eid, None

        with ThreadPoolExecutor(max_workers=min(8, len(need_summary))) as pool:
            futs = [pool.submit(_one, eid) for eid in need_summary]
            for fut in as_completed(futs):
                eid, sm = fut.result()
                if sm:
                    summaries[eid] = sm

    for e in events:
        eid = str(e.get("id"))
        st = ((e.get("status") or {}).get("type") or {}).get("state")
        sm = summaries.get(eid)
        if st == "post" and sm is not None:
            with _FINAL_SUMMARY_LOCK:
                _FINAL_SUMMARY_CACHE[eid] = sm

    games = []
    for e in events:
        eid = str(e.get("id"))
        st = ((e.get("status") or {}).get("type") or {}).get("state")
        summary = summaries.get(eid)
        if summary is None and st == "post":
            with _FINAL_SUMMARY_LOCK:
                summary = _FINAL_SUMMARY_CACHE.get(eid)
        g = _parse_event(e, summary)
        if g:
            games.append(g)
            _dbg(f"parsed {_abbrev_game(g)}")

    # Filter by settings
    live = [g for g in games if g["state"] == "in"]
    if settings.get("live_games_only") and live:
        _dbg(f"slate: live_only → {len(live)} game(s)")
        return live
    out = []
    for g in games:
        if g["state"] == "in":
            out.append(g)
        elif g["state"] == "post" and settings.get("include_final_games", True):
            out.append(g)
        elif g["state"] == "pre" and settings.get("include_scheduled_games", True):
            out.append(g)
    _dbg(
        f"slate: showing {len(out)}/{len(games)} "
        f"(pre={sum(1 for g in out if g['state']=='pre')} "
        f"in={sum(1 for g in out if g['state']=='in')} "
        f"post={sum(1 for g in out if g['state']=='post')})"
    )
    return out


# ---------------------------------------------------------------------------
# Card rendering
# ---------------------------------------------------------------------------
def _fit_lines(text, metrics, max_w, max_lines=2):
    text = (text or "").strip()
    if not text:
        return []
    if metrics.horizontalAdvance(text) <= max_w:
        return [text]
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if metrics.horizontalAdvance(trial) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    # Truncate last line if still too wide
    result = []
    for i, ln in enumerate(lines[:max_lines]):
        if metrics.horizontalAdvance(ln) <= max_w:
            result.append(ln)
        else:
            while ln and metrics.horizontalAdvance(ln + "…") > max_w:
                ln = ln[:-1]
            result.append((ln + "…") if ln else "")
    return [x for x in result if x]


def _pick_qb_entry(qbs, rotate_index):
    if not qbs:
        return None
    return qbs[rotate_index % len(qbs)]


def _pick_qb_line(qbs, rotate_index):
    entry = _pick_qb_entry(qbs, rotate_index)
    return entry["line"] if entry else ""


def _pick_qb_segments(qbs, rotate_index):
    entry = _pick_qb_entry(qbs, rotate_index)
    if not entry:
        return []
    segs = entry.get("segments")
    if segs:
        return segs
    line = entry.get("line") or ""
    return [(line, False)] if line else []


# ---------------------------------------------------------------------------
# Soft background glow (bloom behind content — not edge thickening)
# ---------------------------------------------------------------------------
_GLOW_BLOOM_PAD = 8  # px around logo glow layers (tighter = quieter halo)
_GLOW_TEXT_PAD = 22   # extra room so text bloom can spread past glyphs


def _approx_blur_pixmap(pm, strength=3):
    """Cheap soft blur via downscale→upscale (no Qt GraphicsBlur needed)."""
    if pm is None or pm.isNull() or strength <= 1:
        return pm
    w, h = pm.width(), pm.height()
    sw = max(1, w // strength)
    sh = max(1, h // strength)
    small = pm.scaled(
        sw, sh, QtCore.Qt.IgnoreAspectRatio, QtCore.Qt.SmoothTransformation
    )
    return small.scaled(
        w, h, QtCore.Qt.IgnoreAspectRatio, QtCore.Qt.SmoothTransformation
    )


_GLOW_LAYER_CACHE = {}


def _text_glow_layers(font, text, glow_color):
    """Bloom images for this text. Team names repeat every refresh, so cache them."""
    gc = QtGui.QColor(glow_color)
    key = (
        text,
        font.family(),
        int(font.pixelSize()),
        int(font.weight()),
        bool(font.bold()),
        gc.rgba(),
    )
    with _IMAGE_CACHE_LOCK:
        cached = _GLOW_LAYER_CACHE.get(key)
    if cached is not None:
        return cached

    fm = QtGui.QFontMetrics(font)
    br = fm.tightBoundingRect(text)
    if br.width() <= 0 or br.height() <= 0:
        br = fm.boundingRect(text)
    pad = _GLOW_TEXT_PAD
    tw = max(1, br.width() + pad * 2)
    th = max(1, br.height() + pad * 2)
    ox = pad - br.x()
    oy = pad - br.top()

    glow = _blank_image(tw, th)
    gp = QtGui.QPainter(glow)
    gp.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
    gp.setRenderHint(QtGui.QPainter.Antialiasing, True)
    path = QtGui.QPainterPath()
    path.addText(float(ox), float(oy), font, text)
    for width, alpha in ((10, 70), (6, 110)):
        pen = QtGui.QPen(QtGui.QColor(gc.red(), gc.green(), gc.blue(), alpha))
        pen.setWidth(width)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        gp.strokePath(path, pen)
    core = QtGui.QColor(gc.red(), gc.green(), gc.blue(), 180)
    gp.fillPath(path, core)
    gp.end()

    bloom = _approx_blur_pixmap(glow, strength=8)
    bloom = _approx_blur_pixmap(bloom, strength=6)
    bloom = _approx_blur_pixmap(bloom, strength=4)
    wide = _approx_blur_pixmap(bloom, strength=7)
    layers = (bloom, wide, ox, oy)
    with _IMAGE_CACHE_LOCK:
        if len(_GLOW_LAYER_CACHE) > 400:
            _GLOW_LAYER_CACHE.clear()
        _GLOW_LAYER_CACHE[key] = layers
    return layers


def _draw_text_glow(painter, x, y, text, fill_color, glow_color):
    """Soft diffuse halo *behind* text, then crisp fill on top (no bold edge)."""
    bloom, wide, ox, oy = _text_glow_layers(painter.font(), text, glow_color)
    bx = int(x - ox)
    by = int(y - oy)
    painter.setOpacity(0.28)
    painter.drawImage(bx, by, wide)
    painter.setOpacity(0.40)
    painter.drawImage(bx, by, bloom)
    painter.setOpacity(1.0)

    # Crisp original on top — unchanged size/weight (no near-offset fill copies)
    painter.setPen(QtGui.QColor(fill_color))
    painter.drawText(x, y, text)


def _make_white_silhouette(pixmap):
    if pixmap is None or pixmap.isNull():
        return None
    sil = _blank_image(pixmap.width(), pixmap.height())
    sp = QtGui.QPainter(sil)
    sp.drawImage(0, 0, pixmap)
    sp.setCompositionMode(QtGui.QPainter.CompositionMode_SourceIn)
    sp.fillRect(sil.rect(), QtGui.QColor(255, 255, 255, 255))
    sp.end()
    return sil


def _draw_logo_white_glow(painter, x, y, pixmap):
    """Faint soft white bloom *behind* logo (scaled+blurred silhouette)."""
    if pixmap is None or pixmap.isNull():
        return
    sil = _make_white_silhouette(pixmap)
    if sil is None:
        painter.drawImage(x, y, pixmap)
        return
    w, h = pixmap.width(), pixmap.height()
    pad = _GLOW_BLOOM_PAD
    # Slight enlarge so bloom peeks past edges without a heavy halo
    big_w = int(w * 1.22) + pad * 2
    big_h = int(h * 1.22) + pad * 2
    layer = _blank_image(big_w, big_h)
    lp = QtGui.QPainter(layer)
    scaled = sil.scaled(
        int(w * 1.15), int(h * 1.15),
        QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation,
    )
    sx = (big_w - scaled.width()) // 2
    sy = (big_h - scaled.height()) // 2
    lp.drawImage(sx, sy, scaled)
    lp.end()
    bloom = _approx_blur_pixmap(layer, strength=4)
    bloom = _approx_blur_pixmap(bloom, strength=3)
    # Center bloom on logo
    bx = x + (w - bloom.width()) // 2
    by = y + (h - bloom.height()) // 2
    painter.setOpacity(0.18)
    painter.drawImage(bx, by, bloom)
    painter.setOpacity(0.09)
    wider = _approx_blur_pixmap(bloom, strength=2)
    painter.drawImage(
        x + (w - wider.width()) // 2,
        y + (h - wider.height()) // 2,
        wider,
    )
    painter.setOpacity(1.0)
    painter.drawImage(x, y, pixmap)


def _draw_timeout_bars(painter, score_x, score_w, y, remaining, bar_h, gap, mark_w,
                       color=None):
    """One row of three timeout dashes under a score. Filled = left, blank = used.

    Slots stay put: a used timeout leaves an empty outline instead of
    closing the gap. Filled marks match the team-name color.
    """
    if remaining is None:
        return
    try:
        left = int(remaining)
    except (TypeError, ValueError):
        return
    left = max(0, min(3, left))
    mark_w = max(4, int(mark_w))
    row_w = 3 * mark_w + 2 * gap
    x0 = int(score_x + (score_w - row_w) / 2)
    yy = int(y)
    fill = QtGui.QColor(color) if color is not None else QtGui.QColor("#FFFFFF")
    used = QtGui.QColor(fill)
    used.setAlpha(80)
    for i in range(3):
        xx = int(x0 + i * (mark_w + gap))
        if i < left:
            painter.fillRect(xx, yy, mark_w, bar_h, fill)
        else:
            # Same box as a filled dash. A stroked rect sits off that plane.
            painter.fillRect(xx, yy, mark_w, 1, used)
            if bar_h > 1:
                painter.fillRect(xx, yy + bar_h - 1, mark_w, 1, used)
            if bar_h > 2:
                painter.fillRect(xx, yy, 1, bar_h, used)
                painter.fillRect(xx + mark_w - 1, yy, 1, bar_h, used)


def _draw_icon_glow(painter, x, y, pixmap):
    """Soft white bloom behind small icons (possession football)."""
    if pixmap is None or pixmap.isNull():
        return
    sil = _make_white_silhouette(pixmap)
    if sil is None:
        painter.drawImage(x, y, pixmap)
        return
    w, h = pixmap.width(), pixmap.height()
    big = sil.scaled(
        max(1, int(w * 1.28)), max(1, int(h * 1.28)),
        QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation,
    )
    bloom = _approx_blur_pixmap(big, strength=3)
    painter.setOpacity(0.16)
    painter.drawImage(
        x + (w - bloom.width()) // 2,
        y + (h - bloom.height()) // 2,
        bloom,
    )
    painter.setOpacity(1.0)
    painter.drawImage(x, y, pixmap)


def _game_visual_key(game, qb_rotate_index, settings):
    """Tuple of fields that affect card pixels (for skip-rebuild fingerprint)."""
    show_qb = settings.get("show_qb_stats", True)
    return (
        game.get("game_id"),
        game.get("state"),
        game.get("away_id"),
        game.get("home_id"),
        game.get("away_name"),
        game.get("home_name"),
        game.get("away_score"),
        game.get("home_score"),
        tuple(game.get("away_lines") or ()),
        tuple(game.get("home_lines") or ()),
        game.get("down_distance"),
        game.get("ball_on"),
        game.get("last_play"),
        game.get("status_detail"),
        game.get("kickoff"),
        game.get("possession_id"),
        game.get("away_timeouts"),
        game.get("home_timeouts"),
        _pick_qb_line(game.get("away_qbs") or [], qb_rotate_index) if show_qb else "",
        _pick_qb_line(game.get("home_qbs") or [], qb_rotate_index) if show_qb else "",
    )


def build_game_card(host, game, qb_rotate_index=0):
    """Render one game to a QImage (logical coords, DPR-scaled)."""
    settings = host.settings
    h = host.ticker_height
    dpr = host.dpr

    away_full = game["away_name"]
    home_full = game["home_name"]
    away_label = display_team_name(away_full, settings).upper()
    home_label = display_team_name(home_full, settings).upper()
    away_color = QtGui.QColor(get_team_color(away_full, settings))
    home_color = QtGui.QColor(get_team_color(home_full, settings))

    logo_size = max(40, int(h * 1.10))  # 2× prior (was h * 0.55)
    away_logo = get_team_logo(away_full, logo_size)
    home_logo = get_team_logo(home_full, logo_size)
    away_lpad, away_rpad, away_tpad, _away_bpad, away_vis_w, away_vis_h = _logo_visual(
        away_logo, (away_full, logo_size),
    )
    home_lpad, home_rpad, home_tpad, _home_bpad, home_vis_w, home_vis_h = _logo_visual(
        home_logo, (home_full, logo_size),
    )

    metrics = QtGui.QFontMetrics(host.main_font)
    score_font = getattr(host, "score_font", host.main_font)
    score_m = QtGui.QFontMetrics(score_font)
    small_m = QtGui.QFontMetrics(host.small_font)
    tiny_m = QtGui.QFontMetrics(host.tiny_font)
    time_m = QtGui.QFontMetrics(host.time_font)

    show_qb = settings.get("show_qb_stats", True)
    show_lp = settings.get("show_last_play", True)
    show_ball = settings.get("show_ball_on", True)
    show_poss = settings.get("show_possession", True)
    glow_names = bool(settings.get("glow_team_names", False))
    glow_all = bool(settings.get("glow_all", False))

    away_qb_segs = _pick_qb_segments(game.get("away_qbs") or [], qb_rotate_index) if show_qb else []
    home_qb_segs = _pick_qb_segments(game.get("home_qbs") or [], qb_rotate_index) if show_qb else []
    away_qb = "".join(t for t, _ in away_qb_segs)
    home_qb = "".join(t for t, _ in home_qb_segs)

    away_name_w = metrics.horizontalAdvance(away_label)
    home_name_w = metrics.horizontalAdvance(home_label)
    bold_small = getattr(host, "small_font_bold", host.small_font)  # Gotham Black full size
    regular_small = getattr(host, "small_font_regular", host.small_font)  # same face, size-1
    qb_faux = bool(getattr(host, "qb_faux_bold", False))
    away_qb_w = _qb_segments_width(away_qb_segs, bold_small, regular_small, qb_faux) if away_qb_segs else 0
    home_qb_w = _qb_segments_width(home_qb_segs, bold_small, regular_small, qb_faux) if home_qb_segs else 0
    away_block = max(away_name_w, away_qb_w)
    home_block = max(home_name_w, home_qb_w)
    sym = max(away_block, home_block)
    away_block = home_block = sym

    state = game.get("state")
    status_detail = (game.get("status_detail") or "").strip()
    break_heading = _break_heading(game) if state in ("in", "post") else ""
    linescore = None
    if break_heading:
        linescore = _prepare_linescore(
            getattr(host, "situation_font", host.small_font),
            game,
            h,
            heading=break_heading,
            heading_font=host.time_font,
        )
    show_scores = state in ("in", "post")
    away_score = str(game.get("away_score") or "0") if show_scores else ""
    home_score = str(game.get("home_score") or "0") if show_scores else ""
    away_score_w = score_m.horizontalAdvance(away_score) if away_score else 0
    home_score_w = score_m.horizontalAdvance(home_score) if home_score else 0

    down = (game.get("down_distance") or "").strip()
    ball_on = (game.get("ball_on") or "").strip() if show_ball else ""
    last_play = (game.get("last_play") or "").strip() if show_lp else ""

    # Pregame / final keep the short center. Live games stack clock, down, play.
    vs_m = QtGui.QFontMetrics(host.vs_font)
    sit_font = sit_m = play_font = play_m = None
    center_gap = 1
    center_items = []
    center_kind = "main"
    center_main = ""
    center_sub = ""
    if state == "pre":
        center_main = game.get("kickoff") or status_detail or "vs"
        center_kind = "time"
        center_sub = "vs"
    elif linescore:
        center_main_w = linescore["width"]
    elif state == "post":
        center_main = "F"
        center_sub = status_detail if status_detail.lower() != "final" else ""
    else:
        # LED time face has no readable lowercase ( "1st" draws as "1SC" ).
        clock_text = status_detail.upper()
        situation_line = _live_situation_line(down, ball_on)
        play_text = last_play.upper()
        play_wrap_w = max(220, int(h * 3.8))
        sit_font = QtGui.QFont(host.situation_font)
        play_font = QtGui.QFont(host.play_font)
        sit_px = sit_font.pixelSize()
        play_px = play_font.pixelSize()
        if sit_px < 1:
            sit_px = max(10, int(h * 0.20))
        if play_px < 1:
            play_px = max(6, int(h * 0.145) - 2)
        lp_lines = []
        while True:
            sit_font.setPixelSize(sit_px)
            play_font.setPixelSize(play_px)
            sit_m = QtGui.QFontMetrics(sit_font)
            play_m = QtGui.QFontMetrics(play_font)
            lp_lines = _fit_lines(play_text, play_m, play_wrap_w, 2) if play_text else []
            plan = _live_center_rows(
                h, time_m, sit_m, play_m, clock_text, situation_line, lp_lines,
            )
            if plan["reserved"] <= h:
                break
            shrunk = False
            if play_px > 7:
                play_px -= 1
                shrunk = True
            if sit_px > max(8, play_px + 1):
                sit_px -= 1
                shrunk = True
            if not shrunk:
                break
        center_main_w = 40
        if clock_text:
            center_main_w = max(center_main_w, time_m.horizontalAdvance(clock_text))
        if situation_line:
            center_main_w = max(center_main_w, sit_m.horizontalAdvance(situation_line))
        for ln in lp_lines:
            center_main_w = max(center_main_w, play_m.horizontalAdvance(ln))
        center_main_w = max(int(center_main_w), 48)
        center_gap = 1
        if clock_text:
            center_items.append(("time", clock_text))
        if situation_line:
            center_items.append(("sit", situation_line))
        for ln in lp_lines:
            center_items.append(("play", ln))

    if state != "in" and not linescore:
        _center_fm = time_m if center_kind == "time" else vs_m
        center_main_w = max(
            _center_fm.horizontalAdvance(center_main) if center_main else 0,
            small_m.horizontalAdvance(center_sub) if center_sub else 0,
            40,
        )
        center_main_w = max(center_main_w, 48)
        if center_main:
            center_items.append((center_kind, center_main))
        if center_sub:
            center_items.append(("sub", center_sub))

    pad = 8
    # Same ink-to-ink gap: name ↔ logo ↔ score (logos have uneven canvas pad).
    gap_name_logo = 10
    gap_logo_score = 10
    gap_score_center = 8
    poss_pad = 4
    poss_icon = None
    away_has_ball = False
    home_has_ball = False
    if show_poss and state == "in" and game.get("possession_id"):
        poss_id = str(game.get("possession_id"))
        poss_icon = get_football_icon(max(12, int(h * 0.26)))
        away_has_ball = poss_id == str(game.get("away_id"))
        home_has_ball = poss_id == str(game.get("home_id"))
    poss_icon_w = poss_icon.width() if poss_icon is not None else 0
    away_inside = (poss_pad + poss_icon_w) if away_has_ball else 0
    home_inside = (poss_pad + poss_icon_w) if home_has_ball else 0
    # Pregame: no score digits — logo sits closer to kickoff/vs center
    gap_logo_center = gap_logo_score if not show_scores else gap_logo_score

    if show_scores:
        total_w = (
            pad + away_block + gap_name_logo + away_vis_w + gap_logo_score
            + away_score_w + away_inside + gap_score_center + center_main_w
            + gap_score_center + home_inside + home_score_w + gap_logo_score
            + home_vis_w + gap_name_logo + home_block + pad
        )
    else:
        total_w = (
            pad + away_block + gap_name_logo + away_vis_w + gap_logo_center
            + center_main_w + gap_logo_center
            + home_vis_w + gap_name_logo + home_block + pad
        )
    # Inter-card gap is applied in _compose_strip_image (fixed, MLB-like),
    # not as trailing pixels on the card — keeps spacing even for pre/live/final.

    phys_w = max(1, int(total_w * dpr))
    phys_h = max(1, int(h * dpr))
    image = QtGui.QImage(phys_w, phys_h, QtGui.QImage.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(0)
    painter = QtGui.QPainter(image)
    # Logical coords via devicePixelRatio (no painter.scale — avoids double-DPR)
    painter.setRenderHint(QtGui.QPainter.TextAntialiasing, True)

    # Vertical layout — name optically centered on *drawn* logo; QB hangs below.
    # KeepAspectRatio logos are often shorter than logo_size (e.g. 79→59): name Y
    # must use actual pixmap height, not the request box (that sat names too low).
    qb_ascent = small_m.ascent()
    has_qb = bool(away_qb or home_qb)
    # Center on the visible mark, not the transparent canvas.
    away_logo_y = int(round((h - away_vis_h) / 2.0 - away_tpad))
    home_logo_y = int(round((h - home_vis_h) / 2.0 - home_tpad))
    row_logo_h = max(away_vis_h, home_vis_h)
    logo_mid = h / 2.0
    cap = metrics.capHeight()
    if cap <= 0:
        cap = -metrics.tightBoundingRect("ABCDEFGHIJKLMNOPQRSTUVWXYZ").top()
    if cap <= 0:
        cap = metrics.ascent()
    # Baseline: ALL-CAPS glyph mid == logo mid, then optical lift vs oversized logos
    name_y = int(round(logo_mid + cap / 2.0)) - max(4, row_logo_h // 8)
    qb_y = name_y + 4 + qb_ascent if has_qb else None
    if not getattr(host, "_logged_card_align", False):
        host._logged_card_align = True
        print(
            f"[CARD] align h={h} box={logo_size} ink={away_vis_h}x{home_vis_h} "
            f"logo_y={away_logo_y}/{home_logo_y} logo_mid={logo_mid:.1f} "
            f"cap={int(cap)} name_y={name_y} name_vis_mid={name_y - cap / 2.0:.1f} "
            f"qb_y={qb_y}",
            flush=True,
        )

    x = pad
    # Away name (right-justified in block)
    painter.setFont(host.main_font)
    _nx = x + away_block - away_name_w
    if glow_names or glow_all:
        # Team-color glow when name glow on; faint white under glow_all alone
        _gc = away_color if glow_names else QtGui.QColor(255, 255, 255)
        _draw_text_glow(painter, _nx, name_y, away_label, away_color, _gc)
    else:
        painter.setPen(away_color)
        painter.drawText(_nx, name_y, away_label)
    if qb_y is not None and away_qb_segs:
        _qx = x + away_block - away_qb_w
        _draw_mixed_text(
            painter, _qx, qb_y, away_qb_segs, bold_small, regular_small,
            "#BDBDBD", glow=glow_all, glow_color="#FFFFFF", faux_bold=qb_faux,
        )
    x += away_block + gap_name_logo - _text_right_slack(metrics, away_label, away_name_w)
    away_logo_x = x - away_lpad
    if glow_all:
        _draw_logo_white_glow(painter, away_logo_x, away_logo_y, away_logo)
    else:
        painter.drawImage(away_logo_x, away_logo_y, away_logo)
    x += away_vis_w + (gap_logo_score if show_scores else gap_logo_center)
    if show_scores:
        x -= _text_left_slack(score_m, away_score)

    # Away score (live / final only — blank for pregame).
    # Same optical center as the team name, on the larger score face.
    score_cap = score_m.capHeight()
    if score_cap <= 0:
        score_cap = -score_m.tightBoundingRect("0123456789").top()
    if score_cap <= 0:
        score_cap = score_m.ascent()
    score_y = int(round((name_y - cap / 2.0) + score_cap / 2.0))
    to_bar_h = max(2, int(round(h * 0.04)))
    to_gap = max(4, to_bar_h)
    digit_w = max(8, score_m.horizontalAdvance("0"))
    to_mark_w = max(4, (int(digit_w) - 2 * to_gap) // 3)
    ink = score_m.tightBoundingRect("08")
    timeout_y = score_y + max(1, ink.bottom()) + 3
    if timeout_y + to_bar_h > h - 1:
        timeout_y = max(0, h - 1 - to_bar_h)

    def _paint_poss(ix):
        iy = int(round(score_y - score_cap / 2.0 - poss_icon.height() / 2.0))
        iy = max(0, min(h - poss_icon.height(), iy))
        if glow_all:
            _draw_icon_glow(painter, ix, iy, poss_icon)
        else:
            painter.drawImage(ix, iy, poss_icon)

    def _paint_timeouts(score_x, score_w, remaining, color):
        if state != "in":
            return
        _draw_timeout_bars(
            painter, score_x, score_w, timeout_y, remaining, to_bar_h, to_gap,
            to_mark_w, color,
        )

    away_score_x = x
    if show_scores:
        painter.setFont(score_font)
        if glow_all:
            _draw_text_glow(painter, x, score_y, away_score, "#FFFFFF", "#FFFFFF")
        else:
            painter.setPen(QtGui.QColor("#FFFFFF"))
            painter.drawText(x, score_y, away_score)
        _paint_timeouts(away_score_x, away_score_w, game.get("away_timeouts"), away_color)
        x += away_score_w
        if away_has_ball:
            x += poss_pad
            _paint_poss(x)
            x += poss_icon_w
        x += gap_score_center
    else:
        away_score_x = x  # center starts immediately after logo gap

    # Center column — live: clock / down@spot / play, all between the scores.
    center_left = x
    center_w = center_main_w

    if linescore:
        _draw_linescore(
            painter, center_left, center_w, h, linescore, away_color, home_color,
        )
    elif center_items:
        live_clock = state == "in" and any(k == "time" for k, _ in center_items)
        live_rows = None
        if live_clock:
            clock_text = next((t for k, t in center_items if k == "time"), "")
            sit_text = next((t for k, t in center_items if k == "sit"), "")
            play_lines = [t for k, t in center_items if k == "play"]
            live_rows = _live_center_rows(
                h, time_m, sit_m, play_m, clock_text, sit_text, play_lines,
            )
        else:
            item_metrics = []
            for kind, text in center_items:
                if kind == "main":
                    fm = vs_m
                elif kind == "sub":
                    fm = small_m
                elif kind == "time":
                    fm = time_m
                else:
                    fm = small_m
                item_metrics.append(_glyph_line(fm, text))
            stack_h = (
                sum(hgt for hgt, _ in item_metrics)
                + center_gap * (len(item_metrics) - 1)
            )
            cy = (h - stack_h) // 2
        clock_blue = live_clock and _clock_over_two_minutes(
            next((t for k, t in center_items if k == "time"), "")
        )
        play_i = 0
        for i, (kind, text) in enumerate(center_items):
            if kind == "time":
                painter.setFont(host.time_font)
                fm = time_m
                if _clock_under_two_minutes(text):
                    fill = "#FFD700"
                elif clock_blue:
                    fill = "#00BFFF"
                else:
                    fill = "#FFFFFF"
            elif kind == "main":
                painter.setFont(host.vs_font)
                fm = vs_m
                fill = "#FFD700" if state == "post" else "#FFFFFF"
            elif kind == "sub":
                painter.setFont(host.small_font)
                fm = small_m
                fill = "#A0A0A0"
            elif kind == "sit":
                painter.setFont(sit_font)
                fm = sit_m
                fill = "#00BFFF" if clock_blue else "#FFFFFF"
            else:
                painter.setFont(play_font)
                fm = play_m
                fill = "#D0D0D0"
            if live_rows is not None:
                if kind == "time":
                    top, vis_ascent = live_rows["time"]
                elif kind == "sit":
                    top, vis_ascent = live_rows["sit"]
                else:
                    slot = "play" if play_i == 0 else "play2"
                    play_i += 1
                    top, vis_ascent = live_rows[slot]
                ty = top + vis_ascent
            else:
                vis_h, vis_ascent = item_metrics[i]
                ty = cy + vis_ascent
                cy += vis_h + center_gap
            tw = fm.horizontalAdvance(text)
            tx = center_left + (center_w - tw) // 2
            if kind == "time" and clock_blue:
                left, dash, right = _split_clock_line(text)
                if dash:
                    if glow_all:
                        _draw_text_glow(painter, tx, ty, left, "#00BFFF", "#00BFFF")
                    else:
                        painter.setPen(QtGui.QColor("#00BFFF"))
                        painter.drawText(tx, ty, left)
                    tx += fm.horizontalAdvance(left)
                    if glow_all:
                        _draw_text_glow(painter, tx, ty, dash, "#FFFFFF", "#FFFFFF")
                    else:
                        painter.setPen(QtGui.QColor("#FFFFFF"))
                        painter.drawText(tx, ty, dash)
                    tx += fm.horizontalAdvance(dash)
                    if right:
                        if glow_all:
                            _draw_text_glow(painter, tx, ty, right, "#00BFFF", "#00BFFF")
                        else:
                            painter.setPen(QtGui.QColor("#00BFFF"))
                            painter.drawText(tx, ty, right)
                elif glow_all:
                    _draw_text_glow(painter, tx, ty, text, fill, "#FFFFFF")
                else:
                    painter.setPen(QtGui.QColor(fill))
                    painter.drawText(tx, ty, text)
            elif kind == "sit":
                for piece, is_mark in _sit_color_segments(text):
                    color = "#FFFFFF" if is_mark else fill
                    if glow_all:
                        _draw_text_glow(painter, tx, ty, piece, color, "#FFFFFF")
                    else:
                        painter.setPen(QtGui.QColor(color))
                        painter.drawText(tx, ty, piece)
                    tx += fm.horizontalAdvance(piece)
            elif glow_all:
                _draw_text_glow(painter, tx, ty, text, fill, "#FFFFFF")
            else:
                painter.setPen(QtGui.QColor(fill))
                painter.drawText(tx, ty, text)

    x += center_w
    if show_scores and home_has_ball:
        x += gap_score_center
        _paint_poss(x)
        x += poss_icon_w + poss_pad
    else:
        x += gap_score_center if show_scores else gap_logo_center

    # Home score (live / final only)
    home_score_x = x
    if show_scores:
        painter.setFont(score_font)
        if glow_all:
            _draw_text_glow(painter, x, score_y, home_score, "#FFFFFF", "#FFFFFF")
        else:
            painter.setPen(QtGui.QColor("#FFFFFF"))
            painter.drawText(x, score_y, home_score)
        _paint_timeouts(home_score_x, home_score_w, game.get("home_timeouts"), home_color)

        x += home_score_w + gap_logo_score - _text_right_slack(
            score_m, home_score, home_score_w,
        )
    else:
        x += 0  # already advanced logo-center gap above

    home_logo_x = x - home_lpad
    if glow_all:
        _draw_logo_white_glow(painter, home_logo_x, home_logo_y, home_logo)
    else:
        painter.drawImage(home_logo_x, home_logo_y, home_logo)
    x += home_vis_w + gap_name_logo - _text_left_slack(metrics, home_label)

    painter.setFont(host.main_font)
    if glow_names or glow_all:
        _gc = home_color if glow_names else QtGui.QColor(255, 255, 255)
        _draw_text_glow(painter, x, name_y, home_label, home_color, _gc)
    else:
        painter.setPen(home_color)
        painter.drawText(x, name_y, home_label)
    if qb_y is not None and home_qb_segs:
        _draw_mixed_text(
            painter, x, qb_y, home_qb_segs, bold_small, regular_small,
            "#BDBDBD", glow=glow_all, glow_color="#FFFFFF", faux_bold=qb_faux,
        )

    painter.end()
    return image


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------
class SettingsDialog(QtWidgets.QDialog):
    """Settings dialog with General + Team Colors tabs (MLB-TCKR pattern)."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NFL-TCKR Settings")
        self.setMinimumSize(780, 520)
        self.settings = dict(settings)
        self.color_buttons = {}

        root = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._create_general_tab(), "General")
        self.tabs.addTab(self._create_team_colors_tab(), "Team Colors")
        root.addWidget(self.tabs)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _create_general_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(widget)
        settings = self.settings

        self.speed = QtWidgets.QSpinBox()
        self.speed.setRange(1, 30)
        self.speed.setValue(int(settings.get("speed", 5)))
        layout.addRow("Scroll speed:", self.speed)

        self.update_iv = QtWidgets.QSpinBox()
        self.update_iv.setRange(5, 120)
        self.update_iv.setSuffix(" s")
        self.update_iv.setValue(int(settings.get("update_interval", 15)))
        layout.addRow("Update interval:", self.update_iv)

        self.height = QtWidgets.QSpinBox()
        self.height.setRange(48, 160)
        self.height.setValue(int(settings.get("ticker_height", 72)))
        layout.addRow("Ticker height:", self.height)

        self.docked = QtWidgets.QCheckBox("Docked (reserve desktop space)")
        self.docked.setChecked(settings.get("docked", True))
        self.docked.setToolTip(
            "When checked, registers as a Windows AppBar so other windows and "
            "sister tickers do not draw under the bar. Uncheck for a floating "
            "always-on-top ticker (no desktop reservation)."
        )
        layout.addRow(self.docked)

        # Background transparency: 0% = solid bar, 100% = fully see-through
        opacity = max(0, min(255, int(settings.get("background_opacity", 230))))
        bg_tr_pct = int(round((255 - opacity) * 100 / 255))
        self.transparency = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.transparency.setRange(0, 100)
        self.transparency.setValue(bg_tr_pct)
        self.transparency.setToolTip(
            "Background only — 0% = solid  ·  100% = fully transparent"
        )
        self.transparency.setMinimumWidth(160)
        self._transparency_label = QtWidgets.QLabel(f"{bg_tr_pct}%")
        self._transparency_label.setFixedWidth(40)
        self.transparency.valueChanged.connect(
            lambda v: self._transparency_label.setText(f"{v}%")
        )
        _tr_row = QtWidgets.QHBoxLayout()
        _tr_row.addWidget(self.transparency)
        _tr_row.addWidget(self._transparency_label)
        layout.addRow("Background transparency:", _tr_row)

        # Content transparency: scores, logos, names, QB lines, etc.
        content_op = max(0, min(255, int(settings.get("content_opacity", 255))))
        content_tr_pct = int(round((255 - content_op) * 100 / 255))
        self.content_transparency = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.content_transparency.setRange(0, 100)
        self.content_transparency.setValue(content_tr_pct)
        self.content_transparency.setToolTip(
            "Scores, logos, names, stats — 0% = solid  ·  100% = fully transparent"
        )
        self.content_transparency.setMinimumWidth(160)
        self._content_tr_label = QtWidgets.QLabel(f"{content_tr_pct}%")
        self._content_tr_label.setFixedWidth(40)
        self.content_transparency.valueChanged.connect(
            lambda v: self._content_tr_label.setText(f"{v}%")
        )
        _ctr_row = QtWidgets.QHBoxLayout()
        _ctr_row.addWidget(self.content_transparency)
        _ctr_row.addWidget(self._content_tr_label)
        layout.addRow("Content transparency:", _ctr_row)

        self.name_mode = QtWidgets.QComboBox()
        self.name_mode.addItem("Team Name (e.g. Giants)", "nickname")
        self.name_mode.addItem("City + Team (e.g. New York Giants)", "city")
        self.name_mode.addItem("City (e.g. New York)", "city_only")
        self.name_mode.addItem("Abbreviation (e.g. NYG)", "abbrev")
        if settings.get("use_city_abbreviations"):
            self.name_mode.setCurrentIndex(3)
        elif settings.get("show_city_only"):
            self.name_mode.setCurrentIndex(2)
        elif settings.get("show_team_cities"):
            self.name_mode.setCurrentIndex(1)
        else:
            self.name_mode.setCurrentIndex(0)
        layout.addRow("Team name display:", self.name_mode)

        self.show_lp = QtWidgets.QCheckBox("Show last play")
        self.show_lp.setChecked(settings.get("show_last_play", True))
        layout.addRow(self.show_lp)

        self.show_qb = QtWidgets.QCheckBox("Show QB stats")
        self.show_qb.setChecked(settings.get("show_qb_stats", True))
        layout.addRow(self.show_qb)

        self.show_ball = QtWidgets.QCheckBox("Show ball-on")
        self.show_ball.setChecked(settings.get("show_ball_on", True))
        layout.addRow(self.show_ball)

        self.show_poss = QtWidgets.QCheckBox("Show possession icon")
        self.show_poss.setChecked(settings.get("show_possession", True))
        layout.addRow(self.show_poss)

        self.glow_names = QtWidgets.QCheckBox("Glow team names")
        self.glow_names.setChecked(settings.get("glow_team_names", False))
        self.glow_names.setToolTip("Soft glow behind team names (uses team color)")
        layout.addRow(self.glow_names)

        self.glow_all = QtWidgets.QCheckBox("Glow all items")
        self.glow_all.setChecked(settings.get("glow_all", False))
        self.glow_all.setToolTip(
            "Glow behind scores, stats, and center text; logos get a faint white glow"
        )
        layout.addRow(self.glow_all)

        self.finals = QtWidgets.QCheckBox("Include final games")
        self.finals.setChecked(settings.get("include_final_games", True))
        layout.addRow(self.finals)

        self.scheduled = QtWidgets.QCheckBox("Include scheduled games")
        self.scheduled.setChecked(settings.get("include_scheduled_games", True))
        layout.addRow(self.scheduled)

        self.live_only = QtWidgets.QCheckBox("Live games only")
        self.live_only.setChecked(settings.get("live_games_only", False))
        layout.addRow(self.live_only)

        return widget

    def _create_team_colors_tab(self):
        """AFC left / NFC right — Primary / Secondary / Tertiary / Custom per team."""
        widget = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(10)

        info = QtWidgets.QLabel(
            "Choose Primary, Secondary, or Tertiary from the official palette, "
            "or Custom for any hex. Applied to team names and scoring flash."
        )
        info.setWordWrap(True)
        outer.addWidget(info)

        reset_btn = QtWidgets.QPushButton("Reset All to Defaults")
        reset_btn.clicked.connect(self.reset_team_colors)
        outer.addWidget(reset_btn)

        custom_colors = self.settings.get("team_colors", {})
        self.color_buttons = {}

        AFC_DIVISIONS = [
            ("AFC East",  ["Bills", "Dolphins", "Patriots", "Jets"]),
            ("AFC North", ["Ravens", "Bengals", "Browns", "Steelers"]),
            ("AFC South", ["Texans", "Colts", "Jaguars", "Titans"]),
            ("AFC West",  ["Broncos", "Chiefs", "Raiders", "Chargers"]),
        ]
        NFC_DIVISIONS = [
            ("NFC East",  ["Cowboys", "Giants", "Eagles", "Commanders"]),
            ("NFC North", ["Bears", "Lions", "Packers", "Vikings"]),
            ("NFC South", ["Falcons", "Panthers", "Saints", "Buccaneers"]),
            ("NFC West",  ["Cardinals", "Rams", "49ers", "Seahawks"]),
        ]

        def make_color_row(team):
            palette = NFL_TEAM_COLORS.get(team, ["#FFFFFF", "#FFFFFF", "#FFFFFF"])
            stored = custom_colors.get(team)

            if isinstance(stored, int) and 0 <= stored <= 2:
                init_slot = stored
                init_hex = palette[stored] if stored < len(palette) else palette[0]
            elif isinstance(stored, str) and stored.startswith("#"):
                init_slot = 3
                init_hex = stored
            else:
                init_slot = 0
                init_hex = palette[0]

            slot_combo = QtWidgets.QComboBox()
            slot_combo.setFixedWidth(106)
            slot_combo.addItems(["Primary", "Secondary", "Tertiary", "Custom"])
            slot_combo.setCurrentIndex(init_slot)

            swatch_color = palette[init_slot] if init_slot < 3 else init_hex
            color_btn = QtWidgets.QPushButton()
            color_btn.setFixedSize(28, 22)
            color_btn.setStyleSheet(
                f"background-color: {swatch_color}; border: 1px solid #4a5a7e;"
            )
            color_btn.setEnabled(init_slot == 3)
            color_btn.clicked.connect(lambda checked, t=team: self.pick_team_color(t))

            hex_input = QtWidgets.QLineEdit(init_hex)
            hex_input.setMaxLength(7)
            hex_input.setFixedWidth(68)
            hex_input.setEnabled(init_slot == 3)
            hex_input.textChanged.connect(
                lambda text, t=team: self.update_team_color_preview(t, text)
            )

            self.color_buttons[team] = {
                "slot_combo": slot_combo,
                "button": color_btn,
                "input": hex_input,
                "color": swatch_color,
            }
            slot_combo.currentIndexChanged.connect(
                lambda i, t=team: self._on_team_slot_changed(t, i)
            )

            row = QtWidgets.QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            row.addWidget(slot_combo)
            row.addWidget(color_btn)
            row.addWidget(hex_input)
            row.addStretch()
            return row

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QtWidgets.QWidget()
        cols = QtWidgets.QGridLayout(scroll_widget)
        cols.setContentsMargins(4, 4, 4, 4)
        cols.setHorizontalSpacing(10)
        cols.setVerticalSpacing(6)
        cols.setColumnStretch(0, 1)
        cols.setColumnStretch(1, 1)

        for row_i, ((afc_div, afc_teams), (nfc_div, nfc_teams)) in enumerate(
            zip(AFC_DIVISIONS, NFC_DIVISIONS)
        ):
            for col_i, (div_name, teams) in enumerate(
                [(afc_div, afc_teams), (nfc_div, nfc_teams)]
            ):
                grp = QtWidgets.QGroupBox(div_name)
                form = QtWidgets.QFormLayout(grp)
                form.setContentsMargins(8, 12, 8, 8)
                form.setVerticalSpacing(5)
                form.setHorizontalSpacing(8)
                for team in teams:
                    form.addRow(f"{team}:", make_color_row(team))
                cols.addWidget(grp, row_i, col_i)

        scroll.setWidget(scroll_widget)
        outer.addWidget(scroll)
        return widget

    def pick_team_color(self, team):
        widgets = self.color_buttons.get(team)
        if not widgets:
            return
        if widgets["slot_combo"].currentIndex() != 3:
            widgets["slot_combo"].setCurrentIndex(3)
        color = QtWidgets.QColorDialog.getColor(
            QtGui.QColor(widgets["color"]), self, f"Choose color for {team}"
        )
        if color.isValid():
            hex_color = color.name()
            widgets["color"] = hex_color
            widgets["input"].setText(hex_color)
            widgets["button"].setStyleSheet(
                f"background-color: {hex_color}; border: 1px solid #4a5a7e;"
            )

    def update_team_color_preview(self, team, hex_color):
        if hex_color.startswith("#") and len(hex_color) == 7:
            try:
                QtGui.QColor(hex_color)
                widgets = self.color_buttons.get(team)
                if widgets:
                    widgets["color"] = hex_color
                    widgets["button"].setStyleSheet(
                        f"background-color: {hex_color}; border: 1px solid #4a5a7e;"
                    )
            except Exception:
                pass

    def _on_team_slot_changed(self, team, index):
        widgets = self.color_buttons.get(team)
        if not widgets:
            return
        palette = NFL_TEAM_COLORS.get(team, ["#FFFFFF", "#FFFFFF", "#FFFFFF"])
        is_custom = index == 3
        widgets["button"].setEnabled(is_custom)
        widgets["input"].setEnabled(is_custom)
        if not is_custom:
            color = palette[index] if index < len(palette) else palette[0]
            widgets["color"] = color
            widgets["button"].setStyleSheet(
                f"background-color: {color}; border: 1px solid #4a5a7e;"
            )
        else:
            cur = widgets["input"].text()
            if cur.startswith("#") and len(cur) == 7:
                widgets["color"] = cur
                widgets["button"].setStyleSheet(
                    f"background-color: {cur}; border: 1px solid #4a5a7e;"
                )

    def reset_team_colors(self):
        reply = QtWidgets.QMessageBox.question(
            self,
            "Reset Team Colors",
            "Reset all team colors to NFL defaults?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        for team, widgets in self.color_buttons.items():
            primary = NFL_TEAM_COLORS.get(team, ["#FFFFFF"])[0]
            widgets["slot_combo"].setCurrentIndex(0)
            widgets["color"] = primary
            widgets["input"].setText(primary)

    def apply(self):
        s = self.settings
        s["speed"] = self.speed.value()
        s["update_interval"] = self.update_iv.value()
        s["ticker_height"] = self.height.value()
        s["docked"] = self.docked.isChecked()
        # Invert UI transparency % → stored alpha (0=clear, 255=opaque)
        s["background_opacity"] = int(
            round(255 * (100 - self.transparency.value()) / 100)
        )
        s["content_opacity"] = int(
            round(255 * (100 - self.content_transparency.value()) / 100)
        )
        mode = self.name_mode.currentData()
        s["show_team_cities"] = mode == "city"
        s["show_city_only"] = mode == "city_only"
        s["use_city_abbreviations"] = mode == "abbrev"
        s["show_last_play"] = self.show_lp.isChecked()
        s["show_qb_stats"] = self.show_qb.isChecked()
        s["show_ball_on"] = self.show_ball.isChecked()
        s["show_possession"] = self.show_poss.isChecked()
        s["glow_team_names"] = self.glow_names.isChecked()
        s["glow_all"] = self.glow_all.isChecked()
        s["include_final_games"] = self.finals.isChecked()
        s["include_scheduled_games"] = self.scheduled.isChecked()
        s["live_games_only"] = self.live_only.isChecked()

        # MLB pattern: store slot int for Secondary/Tertiary, hex for Custom;
        # omit Primary (default).
        team_colors = {}
        for team, widgets in self.color_buttons.items():
            slot_index = widgets["slot_combo"].currentIndex()
            if slot_index in (1, 2):
                team_colors[team] = slot_index
            elif slot_index == 3:
                hex_val = widgets["input"].text()
                if hex_val.startswith("#") and len(hex_val) == 7:
                    team_colors[team] = hex_val
        s["team_colors"] = team_colors
        return s


def _layout_tuple(settings, height, dpr):
    s = settings
    return (
        int(height),
        int(s.get("game_spacing_percent", 100)),
        int(s.get("font_scale_percent", 160)),
        int(s.get("player_font_scale_percent", 75)),
        s.get("font", "Ozone"),
        s.get("player_info_font", "Gotham Black"),
        bool(s.get("show_qb_stats", True)),
        bool(s.get("show_last_play", True)),
        bool(s.get("show_ball_on", True)),
        bool(s.get("show_possession", True)),
        bool(s.get("glow_team_names", False)),
        bool(s.get("glow_all", False)),
        bool(s.get("use_city_abbreviations", False)),
        bool(s.get("show_city_only", False)),
        bool(s.get("show_team_cities", False)),
        round(float(dpr), 3),
    )


def _slate_fp(settings, games, qb_index, height, dpr):
    layout = _layout_tuple(settings, height, dpr)
    if not games:
        return ("empty", layout)
    return (
        tuple(_game_visual_key(g, qb_index, settings) for g in games),
        layout,
    )


def _poll_interval_ms(settings, games):
    """Live games use the settings interval. Idle slates poll slowly.

    A game within 10 minutes of kickoff keeps the fast interval so the
    switch to in-progress is not late.
    """
    base = max(5, int(settings.get("update_interval", 15)))
    games = games or []
    if any(g.get("state") == "in" for g in games):
        return base * 1000
    now = datetime.datetime.now().astimezone()
    for g in games:
        if g.get("state") != "pre":
            continue
        start = g.get("start") or ""
        try:
            kick = datetime.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            kick = kick.astimezone()
        except Exception:
            continue
        delta = (kick - now).total_seconds()
        if -120 <= delta <= _KICKOFF_SOON_SECONDS:
            return base * 1000
    return max(base, _IDLE_POLL_SECONDS) * 1000


def _compose_strip_image(card_images, settings, height, dpr):
    """Build the scrolling strip off the UI thread. Returns (QImage, period)."""
    if not card_images:
        return None, 0.0
    h = int(height)
    dpr = float(dpr)
    space_pct = max(0, min(200, int(settings.get("game_spacing_percent", 100))))
    space_scale = space_pct / 100.0
    inter_gap = max(29, int(round(h * 1.80 * space_scale)))
    widths = []
    for img in card_images:
        idpr = float(img.devicePixelRatio()) or dpr
        widths.append(max(1, int(round(img.width() / idpr))))
    n = len(widths)
    period = sum(widths) + n * inter_gap
    strip = QtGui.QImage(
        max(1, int(period * 2 * dpr)),
        max(1, int(h * dpr)),
        QtGui.QImage.Format_ARGB32_Premultiplied,
    )
    strip.setDevicePixelRatio(dpr)
    strip.fill(0)
    p = QtGui.QPainter(strip)
    x = 0.0
    for _ in range(2):
        for img, w in zip(card_images, widths):
            p.drawImage(QtCore.QRectF(x, 0, w, h), img)
            x += w + inter_gap
    p.end()
    return strip, float(period)


def _render_strip(host, games):
    """Paint every card and the strip. Safe off the GUI thread (QImage only)."""
    cards = []
    if not games:
        img = _blank_image(int(320 * host.dpr), int(host.ticker_height * host.dpr))
        img.setDevicePixelRatio(host.dpr)
        p = QtGui.QPainter(img)
        p.setFont(host.main_font)
        p.setPen(QtGui.QColor("#FFD700"))
        p.drawText(20, host.ticker_height // 2 + 6, "NFL-TCKR — loading scores…")
        p.end()
        cards = [img]
    else:
        for g in games:
            try:
                cards.append(build_game_card(host, g, host.qb_rotate_index))
            except Exception as e:
                print(f"[CARD] {g.get('game_id')}: {e}")
    return _compose_strip_image(cards, host.settings, host.ticker_height, host.dpr)


class _SlateThread(QtCore.QThread):
    """Network fetch only. Painting stays on the UI thread — QPainter on a
    second thread stalls the scroll clock even when the paint itself is fast.
    """

    result_ready = QtCore.pyqtSignal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        payload = self._fn()
        if payload is not None:
            self.result_ready.emit(payload)


class _FontHost:
    """Font snapshot so cards can be painted on a QThread."""

    _FONT_ATTRS = (
        "main_font", "score_font", "small_font", "small_font_bold",
        "small_font_regular", "tiny_font", "vs_font", "time_font",
        "situation_font", "play_font",
    )

    def __init__(self, ticker):
        self.settings = dict(ticker.settings)
        self.ticker_height = int(ticker.ticker_height)
        self.dpr = float(ticker.dpr)
        self.qb_rotate_index = int(ticker.qb_rotate_index)
        self.qb_faux_bold = bool(getattr(ticker, "qb_faux_bold", False))
        self._logged_card_align = True
        for name in self._FONT_ATTRS:
            src = getattr(ticker, name, None)
            if src is None:
                continue
            font = QtGui.QFont(src)
            px = font.pixelSize()
            if px > 0:
                font.setPixelSize(px)
            setattr(self, name, font)


class _CardBuildThread(QtCore.QThread):
    """Paint changed cards onto QImages. The scroll blit stays on the UI thread."""

    cards_ready = QtCore.pyqtSignal(object)

    def __init__(self, host, jobs, gen, fp):
        super().__init__()
        self._host = host
        self._jobs = jobs
        self._gen = gen
        self._fp = fp

    def run(self):
        images = {}
        for key, game in self._jobs:
            try:
                img = build_game_card(self._host, game, self._host.qb_rotate_index)
                if img is not None and not img.isNull():
                    images[key] = img.copy()
            except Exception as e:
                print(f"[CARD] {game.get('game_id')}: {e}")
            time.sleep(0)
        self.cards_ready.emit({
            "gen": self._gen,
            "fp": self._fp,
            "images": images,
        })


# ---------------------------------------------------------------------------
# Ticker window
# ---------------------------------------------------------------------------
class NFLTicker(QtWidgets.QWidget):
    games_ready = QtCore.pyqtSignal(list)
    fetch_failed = QtCore.pyqtSignal(str)
    slate_ready = QtCore.pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.settings = get_settings()
        self.games = []
        self.card_images = []
        self.scroll_offset = 0.0  # float for sub-pixel scroll (MLB pattern)
        self._intro_hold = not _cli_faststart
        self._scroll_primed = False  # first loop enters from the right
        self.paused = False
        self.qb_rotate_index = 0
        self._fetching = False
        self._worker_busy = False
        self._worker_pending = False
        self._pending_fetch = False
        self._job_gen = 0
        self._slate_threads = []
        self._scroll_entries = []  # (QImage, logical width) drawn per frame
        self._loop_marker = None
        self._loop_marker_key = None
        self._card_gap = 0
        self._strip_w = 0.0
        self._build_gen = 0
        self._build_fp = None
        self._build_i = 0
        self._build_cards = []
        self._build_cache = {}
        self._scroll_speed_px_per_ms = 0.0
        self._scroll_step_px = 0.0  # fixed px per timer tick (stable dx)
        self._last_frame_ms = 0.0
        self._slate_fp = None  # last composed visual fingerprint
        self._card_cache = {}
        self._card_cache_layout = None
        self.cached_background = None
        self._cached_bg_key = None

        # AppBar / desktop reservation (MLB-TCKR path)
        self._appbar_registered = False
        self._appbar_passive_dock = False
        self._appbar_reserved_phys = None
        self._appbar_hmonitor = None
        self._appbar_on_primary = True
        self._appbar_stack_top_phys = 0
        self._sister_gone_streak = 0
        self._force_shell_appbar = False
        self._appbar_init_done = False
        self._ticker_hidden_for_fullscreen = False
        self._fullscreen_override_exes = {
            x.lower() for x in self.settings.get("fullscreen_override_exes", [])
        }

        # Scoring-play flash (MLB-style sweep overlay)
        self._alert_queue = []
        self._current_alert = None
        self._alert_phase = "idle"  # idle | in | hold | out
        self._alert_phase_start = 0.0
        self._seen_scoring_ids = set()  # "gameId:playId" already observed
        self._seeded_games = set()  # game_ids whose existing plays were seeded (no flash)
        self._elapsed = QtCore.QElapsedTimer()
        self._elapsed.start()
        self._alert_timer = QtCore.QTimer(self)
        self._alert_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self._alert_timer.timeout.connect(self._tick_alert)

        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.Tool
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.setMouseTracking(True)
        self.setWindowTitle("NFL-TCKR")
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self._context_menu(self.mapToGlobal(pos))
        )

        screens = QtWidgets.QApplication.screens()
        mon = min(int(self.settings.get("monitor_index", 0)), max(0, len(screens) - 1))
        self._screen = screens[mon]
        self._target_screen = self._screen  # alias used by AppBar helpers
        geo = self._screen.geometry()
        self.dpr = float(self._screen.devicePixelRatio())
        self.ticker_height = int(self.settings.get("ticker_height", 72))
        self.setGeometry(geo.x(), geo.y(), geo.width(), self.ticker_height)

        self._fullscreen_timer = QtCore.QTimer(self)
        self._fullscreen_timer.timeout.connect(self._check_fullscreen)
        self._fullscreen_timer.setInterval(2000)

        self._init_fonts()
        self._build_tray()

        self.games_ready.connect(self._on_games, QtCore.Qt.QueuedConnection)
        self.slate_ready.connect(self._on_slate, QtCore.Qt.QueuedConnection)
        self.fetch_failed.connect(
            lambda m: print(f"[ESPN] {m}"), QtCore.Qt.QueuedConnection
        )

        # Animation clock: prefer DwmFlush VBlank (MLB). QTimer is fallback only —
        # residual "little judder" with fixed dx was phase drift vs the display.
        _hz = self._screen.refreshRate() if self._screen else 60.0
        if _hz < 20.0:
            _hz = 60.0
        self._display_hz = _hz
        self._vblank_period_ms = 1000.0 / _hz
        # QTimer fallback uses a short interval; VBlank path uses period for step.
        self._scroll_timer_interval_ms = min(8, max(4, round(1000.0 / _hz)))
        self._update_scroll_speed()
        print(
            f"[TICKER] Display refresh: {_hz:.0f} Hz → "
            f"VBlank step {self._vblank_period_ms:.3f} ms, "
            f"QTimer fallback {self._scroll_timer_interval_ms} ms "
            f"(step {self._scroll_step_px:.3f} px/refresh, "
            f"{self._scroll_speed_px_per_ms:.4f} px/ms)"
        )

        self.scroll_timer = QtCore.QTimer(self)
        self.scroll_timer.timeout.connect(lambda: self._tick_scroll(_vsync=False))
        self.scroll_timer.setTimerType(QtCore.Qt.PreciseTimer)

        self._vblank_scroll_pending = False
        self._vblank_fail_reason = ""
        self._vblank_driver = _VBlankDriver(notify_target=self)
        self._vblank_driver.start()
        # Keep QTimer stopped while VBlank owns the clock (started on fail).

        # Scroll judder window (NFL_TCKR_SCROLL_DEBUG=1) — mirrors MLB _judder_profile_frame
        self._scroll_dbg_win_t0 = time.monotonic()
        self._scroll_dbg_tick_dts = []
        self._scroll_dbg_paint_dts = []
        self._scroll_dbg_paint_costs = []
        self._scroll_dbg_deltas_px = []
        self._scroll_dbg_spikes = 0
        self._scroll_dbg_n_ticks = 0
        self._scroll_dbg_n_paints = 0
        self._scroll_dbg_n_paused = 0
        self._scroll_dbg_last_paint_ms = 0.0
        if _NFL_SCROLL_DEBUG:
            _scroll_dbg(
                "profiler enabled — "
                f"VBlank {self._vblank_period_ms:.2f}ms "
                f"fixed-step={self._scroll_step_px:.3f}px "
                f"display={self._display_hz:.0f}Hz "
                f"speed={self._scroll_speed_px_per_ms:.4f}px/ms "
                f"(summary ~1s; SPIKE when tick dt > 1.5× VBlank; "
                f"off: omit --debug / SCROLL_DEBUG=False / NFL_TCKR_SCROLL_DEBUG=0)"
            )

        self.update_timer = QtCore.QTimer(self)
        self.update_timer.timeout.connect(self.refresh_games)
        self.update_timer.start(int(self.settings.get("update_interval", 15)) * 1000)

        self.qb_timer = QtCore.QTimer(self)
        self.qb_timer.timeout.connect(self._rotate_qb)
        self.qb_timer.start(QB_ROTATE_MS)

        if self._intro_hold:
            self._intro_timer = QtCore.QTimer(self)
            self._intro_timer.setSingleShot(True)
            self._intro_timer.timeout.connect(self._end_intro)
            self._intro_timer.start(INTRO_HOLD_MS)
            print(f"[NFL-TCKR] title hold {INTRO_HOLD_MS}ms (skip with --faststart)")
        else:
            print("[NFL-TCKR] --faststart: skipping title hold")

        self.refresh_games()

    def _init_fonts(self):
        scale = self.settings.get("font_scale_percent", 160) / 100.0
        # MLB-TCKR formula for pitcher/batter / player-info text
        pscale = self.settings.get("player_font_scale_percent", 75) / 100.0
        ticker_request = self.settings.get("font", "Ozone") or "Ozone"
        player_request = self.settings.get("player_info_font", "Gotham Black") or "Gotham Black"
        family = resolve_font_family(ticker_request, "Arial Black")
        player_family = resolve_font_family(player_request, family)
        ticker_source = font_resolve_source(ticker_request, "Arial Black")
        player_source = font_resolve_source(player_request, family)
        h = self.ticker_height

        self.main_font = QtGui.QFont(family)
        self.main_font.setPixelSize(max(12, int(h * 0.38 * scale)))
        self.main_font.setBold(True)

        # Scores sit beside the names; a bit larger than the team-name face.
        self.score_font = QtGui.QFont(family)
        self.score_font.setPixelSize(max(14, int(h * 0.45 * scale)))
        self.score_font.setBold(True)

        # QB / player stats — the selected player-info face, not a substituted sans.
        # Labels are the same face, 1px smaller.
        base_small_px = max(6, int(h * 0.22 * scale * 0.5)) + 3
        small_px = max(6, int(base_small_px * pscale))
        label_px = max(5, small_px - 1)
        self.small_font = QtGui.QFont(player_family)
        self.small_font.setPixelSize(small_px)
        self.small_font_bold = QtGui.QFont(self.small_font)
        self.small_font_regular = QtGui.QFont(self.small_font)
        self.small_font_regular.setPixelSize(label_px)
        self.qb_faux_bold = False

        # Down uses the player-info face; last play uses fonts/zed-sans-regular.ttf.
        # Unscaled by font_scale_percent so clock + down + two play lines fit the bar.
        situation_px = max(10, int(h * 0.20))
        play_px = max(6, int(h * 0.145) - 2)
        if play_px >= situation_px:
            play_px = max(6, situation_px - 2)
        self.situation_font = QtGui.QFont(player_family)
        self.situation_font.setPixelSize(situation_px)
        play_family, play_source = _load_bundled_font_file("zed-sans-regular.ttf")
        if not play_family:
            play_family = resolve_font_family("Zed Sans", player_family)
            play_source = font_resolve_source("Zed Sans", player_family)
        self.play_font = QtGui.QFont(play_family)
        self.play_font.setPixelSize(play_px)

        self.tiny_font = QtGui.QFont(family)
        self.tiny_font.setPixelSize(max(7, int(h * 0.15 * scale * pscale)))

        self.vs_font = QtGui.QFont(family)
        self.vs_font.setPixelSize(max(10, int(h * 0.28 * scale)))
        self.vs_font.setBold(True)

        # Kickoff / quarter-clock — visibly smaller than vs_font / prior center text
        self.time_font = QtGui.QFont(family)
        self.time_font.setPixelSize(max(6, int(h * 0.16 * scale)) + 1)
        self.time_font.setBold(True)

        _dbg(f"font ticker request '{ticker_request}' -> {family} ({ticker_source})")
        _dbg(
            f"font player info request '{player_request}' -> "
            f"{player_family} ({player_source})"
        )
        _dbg(
            f"font last play request 'zed-sans-regular.ttf' -> "
            f"{play_family} ({play_source})"
        )
        for label, font in (
            ("team names", self.main_font),
            ("scores", self.score_font),
            ("clock / kickoff / break heading", self.time_font),
            ("pregame vs / final mark", self.vs_font),
            ("qb name and numbers", self.small_font_bold),
            ("qb labels (YDS TD INT)", self.small_font_regular),
            ("down and distance", self.situation_font),
            ("last play", self.play_font),
            ("center sub line", self.small_font),
            ("version watermark", self.tiny_font),
        ):
            _dbg(f"font {label}: {_font_debug_desc(font)}")
        _dbg(
            f"font quarter table: {_font_debug_desc(self.situation_font)} "
            f"(sized down to fit the bar)"
        )
        _dbg(f"font scoring alert: {family} (ticker face, sized to the bar)")

    def _build_tray(self):
        icon_path = FOOTBALL_ICON_PATH
        if os.path.isfile(icon_path):
            icon = QtGui.QIcon(icon_path)
        else:
            icon = self.style().standardIcon(QtWidgets.QStyle.SP_ComputerIcon)
        self.tray = QtWidgets.QSystemTrayIcon(icon, self)
        self.tray.setToolTip(f"NFL-TCKR {VERSION}")
        menu = QtWidgets.QMenu()
        menu.addAction("Settings…", self.open_settings)
        menu.addAction("Refresh now", self.refresh_games)
        menu.addSeparator()
        menu.addAction("Pause / Resume", self.toggle_pause)
        menu.addSeparator()
        menu.addAction("About…", self.show_about)
        menu.addSeparator()
        menu.addAction("Quit", QtWidgets.QApplication.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.open_settings()
            if reason == QtWidgets.QSystemTrayIcon.DoubleClick else None
        )
        self.tray.show()

    def _context_menu(self, global_pos):
        menu = QtWidgets.QMenu(self)
        menu.addAction("Settings…", self.open_settings)
        menu.addAction("Refresh", self.refresh_games)
        menu.addAction("Pause / Resume", self.toggle_pause)
        menu.addSeparator()
        menu.addAction("About…", self.show_about)
        menu.addSeparator()
        menu.addAction("Quit", QtWidgets.QApplication.quit)
        menu.exec_(global_pos)

    def show_about(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("About NFL-TCKR")
        dlg.setModal(True)
        layout = QtWidgets.QVBoxLayout(dlg)
        title = QtWidgets.QLabel(f"NFL-TCKR {VERSION}")
        font = title.font()
        font.setBold(True)
        font.setPointSize(max(11, font.pointSize() + 2))
        title.setFont(font)
        body = QtWidgets.QLabel(
            "Author: Paul R. Charovkine [krypdoh]<br>"
            "Copyright: 2026 All Rights Reserved<br>"
            'Website: <a href="https://github.com/krypdoh/NFL-TCKR">'
            "github.com/krypdoh/NFL-TCKR</a>"
        )
        body.setTextFormat(QtCore.Qt.RichText)
        body.setOpenExternalLinks(True)
        body.setTextInteractionFlags(QtCore.Qt.TextBrowserInteraction)
        layout.addWidget(title)
        layout.addWidget(body)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)
        dlg.exec_()

    def open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self.settings = dlg.apply()
            save_settings(self.settings)
            self._fullscreen_override_exes = {
                x.lower()
                for x in self.settings.get("fullscreen_override_exes", [])
            }
            new_height = int(self.settings.get("ticker_height", 72))
            self.ticker_height = new_height
            geo = self._screen.geometry()
            self.setGeometry(geo.x(), geo.y(), geo.width(), self.ticker_height)
            # Re-register AppBar after height / docked changes (needs live HWND size)
            self.remove_appbar()
            if self.settings.get("docked", True):
                self.setup_appbar()
            self._init_fonts()
            self.update_timer.setInterval(int(self.settings.get("update_interval", 15)) * 1000)
            self._update_scroll_speed()
            self.cached_background = None
            self._cached_bg_key = None
            self._slate_fp = None  # force strip rebuild after settings (colors/fonts/etc.)
            self.refresh_games()

    def toggle_pause(self):
        self.paused = not self.paused

    def refresh_games(self):
        self._request_slate(True)

    def _request_slate(self, fetch):
        """Fetch off the UI thread. Card pixels are built on the UI thread."""
        if not fetch:
            self._publish_strip()
            return
        self._job_gen += 1
        self._pending_fetch = True
        if self._worker_busy:
            self._worker_pending = True
            return
        self._start_slate_worker(self._job_gen)

    def _start_slate_worker(self, gen):
        self._worker_busy = True
        self._worker_pending = False
        self._pending_fetch = False
        settings = dict(self.settings)

        def worker():
            payload = {
                "gen": gen,
                "fetch": True,
                "error": None,
                "games": None,
            }
            try:
                payload["games"] = fetch_nfl_games(settings)
            except Exception as e:
                payload["error"] = str(e)
            return payload

        thread = _SlateThread(worker)
        self._slate_threads.append(thread)
        thread.result_ready.connect(self._on_slate, QtCore.Qt.QueuedConnection)
        thread.finished.connect(lambda t=thread: self._drop_slate_thread(t))
        thread.start()

    def _drop_slate_thread(self, thread):
        try:
            self._slate_threads.remove(thread)
        except ValueError:
            pass
        thread.deleteLater()

    def _on_slate(self, payload):
        """Swap in a strip that was already painted. Scroll keeps using the old one until then."""
        stale = payload.get("gen") != self._job_gen
        self._worker_busy = False
        if payload.get("error"):
            print(f"[ESPN] {payload.get('error')}")
        if not stale and not payload.get("error"):
            games = payload.get("games") or []
            self._detect_scoring_alerts(games)
            self.games = games
            self._publish_strip()
            if payload.get("fetch"):
                self._apply_poll_interval(games)
                print(f"[NFL-TCKR] {len(self.games)} game(s) loaded")
        if self._worker_pending or stale:
            self._worker_pending = False
            self._start_slate_worker(self._job_gen)

    def _publish_strip(self):
        """Queue card paints between frames. The cards on screen keep scrolling."""
        layout = _layout_tuple(self.settings, self.ticker_height, self.dpr)
        if layout != self._card_cache_layout:
            self._card_cache = {}
            self._card_cache_layout = layout
        fp = _slate_fp(
            self.settings, self.games, self.qb_rotate_index,
            self.ticker_height, self.dpr,
        )
        if fp == self._slate_fp and self._scroll_entries:
            return
        if fp == self._build_fp:
            return
        self._build_gen += 1
        gen = self._build_gen
        self._build_fp = fp
        games = list(self.games or [])
        if not games:
            image, _period = _render_strip(self, [])
            cards = [image] if image is not None and not image.isNull() else []
            self._install_scroll_cards(cards, {}, fp)
            return
        order = []
        misses = []
        cache = {}
        for g in games:
            key = _game_visual_key(g, self.qb_rotate_index, self.settings)
            order.append(key)
            img = self._card_cache.get(key)
            if img is None:
                misses.append((key, g))
            else:
                cache[key] = img
        self._build_order = order
        self._build_cache = cache
        if not misses:
            self._install_scroll_cards(
                [cache[k] for k in order if k in cache], cache, fp,
            )
            return
        if not self._scroll_entries and not self._intro_hold:
            self._show_loading_card()
        host = _FontHost(self)
        thread = _CardBuildThread(host, misses, gen, fp)
        self._slate_threads.append(thread)
        thread.cards_ready.connect(self._on_cards_built, QtCore.Qt.QueuedConnection)
        thread.finished.connect(lambda t=thread: self._drop_slate_thread(t))
        thread.start()

    def _loop_marker_entry(self):
        """NFL shield card that leads the loop. Cached until height or DPR changes."""
        key = (int(self.ticker_height), round(float(self.dpr), 3))
        if self._loop_marker is not None and self._loop_marker_key == key:
            return self._loop_marker
        img = build_loop_marker_card(self)
        dpr = float(img.devicePixelRatio()) or float(self.dpr) or 1.0
        width = max(1, int(round(img.width() / dpr)))
        self._loop_marker = (img, width)
        self._loop_marker_key = key
        return self._loop_marker

    def _show_loading_card(self):
        """One cheap card so the bar can scroll while the real cards are painted."""
        image, _period = _render_strip(self, [])
        if image is None or image.isNull():
            return
        dpr = float(image.devicePixelRatio()) or float(self.dpr) or 1.0
        width = max(1, int(round(image.width() / dpr)))
        gap = self._card_gap or max(29, int(round(self.ticker_height * 1.80)))
        self._card_gap = gap
        entries = [self._loop_marker_entry(), (image, width)]
        self._scroll_entries = entries
        self._strip_w = float(sum(w for _img, w in entries) + len(entries) * gap)
        if not self._intro_hold and not self._scroll_primed:
            self._prime_scroll_from_right()
        self.update()

    def _on_cards_built(self, payload):
        if payload.get("gen") != self._build_gen:
            return
        images = payload.get("images") or {}
        cache = dict(self._build_cache)
        cache.update(images)
        cards = [cache[k] for k in self._build_order if cache.get(k) is not None]
        self._install_scroll_cards(cards, cache, payload.get("fp"))

    def _install_scroll_cards(self, cards, cache, fp):
        h = int(self.ticker_height)
        dpr = float(self.dpr)
        space_pct = max(0, min(200, int(self.settings.get("game_spacing_percent", 100))))
        gap = max(29, int(round(h * 1.80 * (space_pct / 100.0))))
        entries = [self._loop_marker_entry()]
        for img in cards:
            if img is None or img.isNull():
                continue
            idpr = float(img.devicePixelRatio()) or dpr
            entries.append((img, max(1, int(round(img.width() / idpr)))))
        if not entries:
            self._build_fp = None
            return
        period = float(sum(w for _img, w in entries) + len(entries) * gap)
        old_period = self._strip_w
        self._card_cache = cache
        self._scroll_entries = entries
        self._card_gap = gap
        self._strip_w = period
        if not self._intro_hold:
            if not self._scroll_primed:
                self._prime_scroll_from_right()
            elif period > 0 and self.scroll_offset >= 0:
                if old_period > 0 and abs(old_period - period) > 0.5:
                    self.scroll_offset = self.scroll_offset % period
                elif self.scroll_offset >= period:
                    self.scroll_offset = self.scroll_offset % period
        self._slate_fp = fp
        self._build_fp = None
        self.update()

    def _prime_scroll_from_right(self):
        """Place the first card just off the right edge so the loop walks in."""
        if self._scroll_primed or self._intro_hold:
            return
        if not self._scroll_entries or self._strip_w <= 0:
            return
        self.scroll_offset = -float(max(1, self.width()))
        self._scroll_primed = True

    def _end_intro(self):
        self._intro_hold = False
        self._prime_scroll_from_right()
        self.update()

    def _draw_intro_title(self, painter, w, h):
        text = "NFL-TCKR"
        font = QtGui.QFont(self.main_font)
        font.setPixelSize(max(16, int(h * 0.55)))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QtGui.QColor("#FFD700"))
        fm = QtGui.QFontMetrics(font)
        tw = fm.horizontalAdvance(text)
        cap = fm.capHeight()
        if cap <= 0:
            cap = fm.ascent()
        x = (w - tw) // 2
        y = int(round(h / 2.0 + cap / 2.0))
        painter.drawText(x, y, text)

    def _apply_poll_interval(self, games):
        ms = _poll_interval_ms(self.settings, games)
        self.update_timer.setInterval(ms)
        self.update_timer.start()

    def _on_games(self, games):
        new_games = games or []
        self._detect_scoring_alerts(new_games)
        self.games = new_games
        print(f"[NFL-TCKR] {len(self.games)} game(s) loaded")
        self._request_slate(False)

    def _detect_scoring_alerts(self, games):
        """Queue MLB-style flashes for newly seen ESPN scoringPlays (deduped by id)."""
        for g in games:
            gid = str(g.get("game_id") or "")
            if not gid:
                continue
            plays = g.get("scoring_plays") or []
            # Seed on first sight so historical/finals don't spam on startup
            if gid not in self._seeded_games:
                for p in plays:
                    pid = p.get("id")
                    if pid:
                        self._seen_scoring_ids.add(f"{gid}:{pid}")
                self._seeded_games.add(gid)
                continue
            # Only flash new plays while game is live (not pre; not replaying finals load)
            if g.get("state") != "in":
                for p in plays:
                    pid = p.get("id")
                    if pid:
                        self._seen_scoring_ids.add(f"{gid}:{pid}")
                continue
            for p in plays:
                pid = p.get("id")
                if not pid:
                    continue
                key = f"{gid}:{pid}"
                if key in self._seen_scoring_ids:
                    continue
                self._seen_scoring_ids.add(key)
                team_full = p.get("team_full") or ""
                # Match scoring team to away/home full name when possible
                if str(p.get("team_id")) == str(g.get("away_id")):
                    team_full = g.get("away_name") or team_full
                elif str(p.get("team_id")) == str(g.get("home_id")):
                    team_full = g.get("home_name") or team_full
                nick = get_team_nickname(team_full) or p.get("team_abbr") or "TEAM"
                text = format_scoring_alert_message(p, nick)
                color = get_team_color(team_full, self.settings)
                self._alert_queue.append({
                    "text": text,
                    "team_color": color,
                    "game_id": gid,
                    "play_id": pid,
                })
                _dbg(f"SCORE FLASH queued: {text} ({key})")
        if self._current_alert is None and self._alert_queue:
            self._start_next_alert()

    def _start_next_alert(self):
        if not self._alert_queue:
            return
        self._current_alert = self._alert_queue.pop(0)
        self._alert_phase = "in"
        self._alert_phase_start = self._elapsed.nsecsElapsed() / 1_000_000.0
        self._alert_timer.start(self._scroll_timer_interval_ms)
        _dbg(f"SCORE FLASH showing: {self._current_alert.get('text')}")
        self.update()

    def _tick_alert(self):
        if self._current_alert is None:
            self._alert_timer.stop()
            return
        now = self._elapsed.nsecsElapsed() / 1_000_000.0
        elapsed = now - self._alert_phase_start
        hold_ms = float(self.settings.get("scoring_alert_duration_ms", SCORE_ALERT_HOLD_MS))
        if self._alert_phase == "in" and elapsed >= SCORE_ALERT_IN_MS:
            self._alert_phase = "hold"
            self._alert_phase_start = now
        elif self._alert_phase == "hold" and elapsed >= hold_ms:
            self._alert_phase = "out"
            self._alert_phase_start = now
        elif self._alert_phase == "out" and elapsed >= SCORE_ALERT_OUT_MS:
            self._finish_alert()
            return
        self.update()

    def _finish_alert(self):
        self._alert_timer.stop()
        self._current_alert = None
        self._alert_phase = "idle"
        self.update()
        if self._alert_queue:
            QtCore.QTimer.singleShot(250, self._start_next_alert)

    def _render_scoring_alert(self, painter, phase, phase_elapsed_ms):
        """Full-bar team-color flash with sweep reveal (borrowed from MLB-TCKR)."""
        alert = self._current_alert
        if alert is None:
            return
        w, h = self.width(), self.height()
        if phase == "in":
            bg_a = min(1.0, phase_elapsed_ms / 200.0)
        elif phase == "hold":
            bg_a = 1.0
        else:
            bg_a = max(0.0, 1.0 - phase_elapsed_ms / SCORE_ALERT_OUT_MS)
        if bg_a <= 0.01:
            return

        tc = QtGui.QColor(alert["team_color"])
        tc.setAlphaF(0.82 * bg_a)
        painter.fillRect(0, 0, w, h, tc)

        vign = QtGui.QLinearGradient(0, 0, 0, h)
        vign.setColorAt(0.0, QtGui.QColor(0, 0, 0, int(110 * bg_a)))
        vign.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
        painter.fillRect(0, 0, w, h, vign)

        text = alert.get("text") or ""
        family = load_ticker_font()
        min_px = max(10, int(h * 0.25))
        max_px = int(h * 0.58)
        font_px = min_px
        for candidate in range(max_px, min_px - 1, -1):
            f = QtGui.QFont(family)
            f.setPixelSize(candidate)
            f.setBold(True)
            if QtGui.QFontMetrics(f).horizontalAdvance(text) <= w - 32:
                font_px = candidate
                break
        af = QtGui.QFont(family)
        af.setPixelSize(font_px)
        af.setBold(True)
        fm = QtGui.QFontMetrics(af)
        tw = fm.horizontalAdvance(text)
        tx = (w - tw) // 2
        br = fm.boundingRect("ABCWMgy0123456789")
        ty = (h - br.height()) // 2 - br.top()

        if phase == "in":
            t = min(phase_elapsed_ms, float(SCORE_ALERT_IN_MS))
            sweep_x = (w + 40) * (1.0 - t / SCORE_ALERT_IN_MS) - 20
            dim = QtGui.QColor("#FFD700")
            dim.setAlphaF(0.22 * bg_a)
            painter.setFont(af)
            painter.setPen(dim)
            painter.drawText(tx, ty, text)
            left_w = max(0.0, sweep_x)
            if left_w > 0:
                painter.save()
                painter.setClipRect(QtCore.QRectF(0, 0, left_w, h))
                painter.setPen(QtGui.QColor("#FFD700"))
                painter.drawText(tx, ty, text)
                painter.restore()
            beam_w = max(1, int(w * 0.08))
            beam = QtGui.QLinearGradient(sweep_x - beam_w, 0, sweep_x + beam_w, 0)
            beam.setColorAt(0.0, QtGui.QColor(255, 215, 0, 0))
            beam.setColorAt(0.5, QtGui.QColor(255, 255, 220, int(200 * bg_a)))
            beam.setColorAt(1.0, QtGui.QColor(255, 215, 0, 0))
            painter.fillRect(QtCore.QRectF(sweep_x - beam_w, 0, beam_w * 2, h), beam)
        else:
            gold = QtGui.QColor("#FFD700")
            gold.setAlphaF(bg_a)
            painter.setFont(af)
            painter.setPen(gold)
            # Gentle pulse on hold
            if phase == "hold":
                pulse = 1.0 + 0.03 * abs(((phase_elapsed_ms / 400.0) % 2) - 1)
                painter.save()
                painter.translate(w / 2, h / 2)
                painter.scale(pulse, pulse)
                painter.translate(-w / 2, -h / 2)
                painter.drawText(tx, ty, text)
                painter.restore()
            else:
                painter.drawText(tx, ty, text)

    def _rotate_qb(self):
        self.qb_rotate_index += 1
        if self.games:
            self._request_slate(False)

    def _update_scroll_speed(self):
        """Logical px/ms + fixed px per display refresh (VBlank or timer tick)."""
        raw = float(self.settings.get("speed", 5))
        self._scroll_speed_px_per_ms = (raw * 0.35) / 16.667
        # Prefer full refresh period so VBlank and steady QTimer look the same speed.
        period = float(
            getattr(self, "_vblank_period_ms", 0)
            or getattr(self, "_scroll_timer_interval_ms", 16)
            or 16
        )
        self._scroll_step_px = self._scroll_speed_px_per_ms * period

    def _vblank_owns_clock(self):
        return (
            hasattr(self, "_vblank_driver")
            and self._vblank_driver.isRunning()
            and not self.paused
            and self._current_alert is None
        )

    def _on_vblank_failed(self, reason):
        print(f"[TICKER] VBlank driver stopped ({reason}) — QTimer fallback")
        self._ensure_scroll_timer(force=True)

    def _ensure_scroll_timer(self, force=False):
        """Start QTimer scroll if VBlank is dead or force=True."""
        vblank_running = (
            hasattr(self, "_vblank_driver") and self._vblank_driver.isRunning()
        )
        if vblank_running and not force:
            return
        if force and vblank_running:
            try:
                self._vblank_driver.stop()
            except Exception:
                pass
        if force or not self.scroll_timer.isActive():
            self._last_frame_ms = self._elapsed.nsecsElapsed() / 1_000_000.0
            self.scroll_timer.start(self._scroll_timer_interval_ms)
            if force:
                print(
                    f"[TICKER] Scroll timer fallback active "
                    f"({self._scroll_timer_interval_ms} ms)"
                )

    def event(self, event):
        if event.type() == _GUI_WAKE_EVENT_TYPE:
            code = getattr(event, "code", 0)
            if code == _GUI_WAKE_SCROLL:
                self._vblank_scroll_pending = False
                self._tick_scroll(_vsync=True)
            elif code == _GUI_WAKE_VBLANK_FAIL:
                self._on_vblank_failed(
                    getattr(self, "_vblank_fail_reason", "") or "unknown"
                )
            return True
        return super().event(event)

    def _slate_fingerprint(self):
        return _slate_fp(
            self.settings, self.games, self.qb_rotate_index,
            self.ticker_height, self.dpr,
        )

    def _rebuild_cards(self):
        """Schedule a strip render off the UI thread."""
        self._request_slate(False)

    def _scroll_dbg_note_tick(self, tick_dt_ms, dx_px, paused, reason=""):
        """Accumulate timer-tick samples; emit SPIKE + ~1s summary."""
        if not _NFL_SCROLL_DEBUG:
            return
        self._scroll_dbg_n_ticks += 1
        if paused:
            self._scroll_dbg_n_paused += 1
        if tick_dt_ms is None:
            self._scroll_dbg_maybe_flush()
            return
        if not paused:
            self._scroll_dbg_tick_dts.append(tick_dt_ms)
            self._scroll_dbg_deltas_px.append(dx_px)
        target = float(getattr(self, "_vblank_period_ms", 0) or self._scroll_timer_interval_ms or 16.0)
        # 1.5× catches soft cadence hits the old 2× threshold missed (user jitter 3–7 ms)
        if tick_dt_ms > 1.5 * target:
            self._scroll_dbg_spikes += 1
            pause_note = f" paused={reason or 'yes'}" if paused else ""
            _scroll_dbg(
                f"SPIKE tick dt={tick_dt_ms:.1f}ms "
                f"(target {target:.0f}ms, {tick_dt_ms / target:.1f}×) "
                f"dx={dx_px:.2f}px offset={self.scroll_offset:.1f}"
                f"{pause_note}"
            )
        self._scroll_dbg_maybe_flush()

    def _scroll_dbg_note_paint(self, paint_dt_ms, paint_cost_ms):
        if not _NFL_SCROLL_DEBUG:
            return
        self._scroll_dbg_n_paints += 1
        if paint_dt_ms is not None:
            self._scroll_dbg_paint_dts.append(paint_dt_ms)
            # Paint cadence often ~display Hz (~16ms @60Hz); spike vs 1.5× VBlank budget
            vblank_ms = 1000.0 / max(20.0, self._display_hz)
            if paint_dt_ms > 1.5 * vblank_ms:
                _scroll_dbg(
                    f"SPIKE paint interval={paint_dt_ms:.1f}ms "
                    f"(~{vblank_ms:.1f}ms VBlank, {paint_dt_ms / vblank_ms:.1f}×) "
                    f"cost={paint_cost_ms:.2f}ms offset={self.scroll_offset:.1f}"
                )
        self._scroll_dbg_paint_costs.append(paint_cost_ms)
        self._scroll_dbg_maybe_flush()

    def _scroll_dbg_maybe_flush(self):
        now = time.monotonic()
        if now - self._scroll_dbg_win_t0 < 1.0:
            return
        win_s = now - self._scroll_dbg_win_t0
        t_mn, t_avg, t_mx, t_jit, t_sd = _scroll_dbg_stats(self._scroll_dbg_tick_dts)
        p_mn, p_avg, p_mx, p_jit, p_sd = _scroll_dbg_stats(self._scroll_dbg_paint_dts)
        c_mn, c_avg, c_mx, _, _ = _scroll_dbg_stats(self._scroll_dbg_paint_costs)
        d_mn, d_avg, d_mx, _, _ = _scroll_dbg_stats(self._scroll_dbg_deltas_px)
        target = float(getattr(self, "_vblank_period_ms", 0) or self._scroll_timer_interval_ms or 16.0)
        pause_state = (
            "alert" if self._current_alert is not None
            else ("hover/space" if self.paused else "run")
        )
        clock = (
            "VBlank"
            if (
                hasattr(self, "_vblank_driver")
                and self._vblank_driver.isRunning()
                and not self.scroll_timer.isActive()
            )
            else "QTimer"
        )

        def _f(v, places=1):
            return f"{v:.{places}f}" if v is not None else "—"

        parts = [
            f"{win_s:.2f}s",
            f"ticks={self._scroll_dbg_n_ticks}",
            f"paints={self._scroll_dbg_n_paints}",
            f"paused_ticks={self._scroll_dbg_n_paused}",
            f"now={pause_state}",
            f"clock={clock}",
            f"target={target:.1f}ms",
            f"step={self._scroll_step_px:.3f}px",
        ]
        if t_avg is not None:
            parts.append(
                f"tick dt min/avg/max/jitter/stdev="
                f"{_f(t_mn)}/{_f(t_avg)}/{_f(t_mx)}/{_f(t_jit)}/{_f(t_sd)}ms"
            )
            parts.append(f"spikes(>1.5×)={self._scroll_dbg_spikes}")
        if p_avg is not None:
            parts.append(
                f"paint dt min/avg/max/jitter="
                f"{_f(p_mn)}/{_f(p_avg)}/{_f(p_mx)}/{_f(p_jit)}ms"
            )
        if c_avg is not None:
            parts.append(f"paint cost avg/max={_f(c_avg, 2)}/{_f(c_mx, 2)}ms")
        if d_avg is not None:
            parts.append(
                f"dx min/avg/max={_f(d_mn, 2)}/{_f(d_avg, 2)}/{_f(d_mx, 2)}px "
                f"offset={self.scroll_offset:.1f}"
            )
        _scroll_dbg(" | ".join(parts))

        self._scroll_dbg_win_t0 = now
        self._scroll_dbg_tick_dts = []
        self._scroll_dbg_paint_dts = []
        self._scroll_dbg_paint_costs = []
        self._scroll_dbg_deltas_px = []
        self._scroll_dbg_spikes = 0
        self._scroll_dbg_n_ticks = 0
        self._scroll_dbg_n_paints = 0
        self._scroll_dbg_n_paused = 0

    def _tick_scroll(self, _vsync=False):
        """Advance one frame. VBlank: fixed px/refresh + sync repaint (no catch-up)."""
        now_ms = self._elapsed.nsecsElapsed() / 1_000_000.0
        tick_dt = (now_ms - self._last_frame_ms) if self._last_frame_ms > 0 else None
        if self._intro_hold:
            self._last_frame_ms = now_ms
            self._scroll_dbg_note_tick(tick_dt, 0.0, paused=True, reason="intro")
            if _vsync:
                self.repaint()
            else:
                self.update()
            return
        if not self._scroll_primed:
            self._prime_scroll_from_right()
        if self._current_alert is not None:
            # Freeze scroll while scoring flash plays; don't accumulate catch-up
            self._last_frame_ms = now_ms
            self._scroll_dbg_note_tick(tick_dt, 0.0, paused=True, reason="alert")
            return
        if self.paused or not self._scroll_entries or self._strip_w <= 0:
            self._last_frame_ms = now_ms
            reason = "hover/space" if self.paused else "no-strip"
            self._scroll_dbg_note_tick(tick_dt, 0.0, paused=True, reason=reason)
            return

        if _vsync:
            # Missed VBlanks skipped (no multi-step catch-up jump)
            dx = self._scroll_step_px if self._scroll_step_px > 0 else 0.0
        else:
            # QTimer fallback: wall-clock dt so short timer intervals keep speed
            if self._last_frame_ms > 0 and tick_dt is not None:
                dt = min(tick_dt, 100.0)
                dx = self._scroll_speed_px_per_ms * dt
            else:
                dx = self._scroll_step_px if self._scroll_step_px > 0 else 0.0

        self.scroll_offset += dx
        while self.scroll_offset >= self._strip_w:
            self.scroll_offset -= self._strip_w
        self._last_frame_ms = now_ms
        self._scroll_dbg_note_tick(tick_dt, dx, paused=False)
        # VBlank: paint NOW so the backing store is ready before the next
        # compositor deadline. update() is low-priority and used to land late.
        if _vsync:
            self.repaint()
        else:
            self.update()

    def paintEvent(self, event):
        _paint_t0 = time.perf_counter() if _NFL_SCROLL_DEBUG else 0.0
        paint_dt = None
        if _NFL_SCROLL_DEBUG:
            now_ms = self._elapsed.nsecsElapsed() / 1_000_000.0
            if self._scroll_dbg_last_paint_ms > 0:
                paint_dt = now_ms - self._scroll_dbg_last_paint_ms
            self._scroll_dbg_last_paint_ms = now_ms

        painter = QtGui.QPainter(self)
        w, h = self.width(), self.height()

        # Scoring alert overlays the whole bar
        if self._current_alert is not None:
            now = self._elapsed.nsecsElapsed() / 1_000_000.0
            phase_elapsed = now - self._alert_phase_start
            self._render_scoring_alert(painter, self._alert_phase, phase_elapsed)
            painter.end()
            if _NFL_SCROLL_DEBUG:
                cost = (time.perf_counter() - _paint_t0) * 1000.0
                self._scroll_dbg_note_paint(paint_dt, cost)
            return

        # Cached LED / solid background (avoid per-frame line loops)
        opacity = int(self.settings.get("background_opacity", 230))
        led = bool(self.settings.get("led_background", True))
        bg_key = (led, opacity, w, h, VERSION)
        if self._cached_bg_key != bg_key or self.cached_background is None:
            self.cached_background = QtGui.QPixmap(w, h)
            self.cached_background.fill(QtCore.Qt.transparent)
            bg = QtGui.QPainter(self.cached_background)
            bg.fillRect(0, 0, w, h, QtGui.QColor(0, 0, 0, opacity))
            if led:
                bg.setPen(QtGui.QColor(20, 40, 20, 80))
                for y in range(0, h, 3):
                    bg.drawLine(0, y, w, y)
            bg.setPen(QtGui.QColor(80, 80, 80, 120))
            bg.setFont(self.tiny_font)
            bg.drawText(6, h - 4, f"NFL-TCKR {VERSION}")
            bg.end()
            self._cached_bg_key = bg_key
        painter.drawPixmap(0, 0, self.cached_background)

        if self._intro_hold:
            self._draw_intro_title(painter, w, h)
            painter.end()
            if _NFL_SCROLL_DEBUG:
                cost = (time.perf_counter() - _paint_t0) * 1000.0
                self._scroll_dbg_note_paint(paint_dt, cost)
            return

        # Scroll content (scores/logos/names) — content_opacity like MLB-TCKR
        if self._scroll_entries and self._strip_w > 0:
            content_alpha = max(0, min(255, int(self.settings.get("content_opacity", 255)))) / 255.0
            painter.setOpacity(content_alpha)
            _smooth = self.dpr < 2.0
            if _smooth:
                painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
            offset = self.scroll_offset
            period = self._strip_w
            gap = self._card_gap
            view_w = w
            for lap in (0.0, period):
                x = lap - offset
                for img, cw in self._scroll_entries:
                    if x + cw > 0 and x < view_w:
                        painter.drawImage(QtCore.QRectF(x, 0, cw, h), img)
                    x += cw + gap
                    if x >= view_w:
                        break
            if _smooth:
                painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, False)
            painter.setOpacity(1.0)

        painter.end()
        if _NFL_SCROLL_DEBUG:
            cost = (time.perf_counter() - _paint_t0) * 1000.0
            self._scroll_dbg_note_paint(paint_dt, cost)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Space:
            self.toggle_pause()
        elif event.key() == QtCore.Qt.Key_R:
            self.refresh_games()
        elif event.key() == QtCore.Qt.Key_S:
            self.open_settings()
        elif event.key() == QtCore.Qt.Key_Q:
            QtWidgets.QApplication.quit()
        else:
            super().keyPressEvent(event)

    def enterEvent(self, event):
        self.paused = True
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.paused = False
        super().leaveEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        # AppBar needs a valid, visible HWND — defer until the window is realized.
        if not getattr(self, "_appbar_init_done", False):
            self._appbar_init_done = True
            if self.settings.get("docked", True):
                # 0 ms is often too early (client rect / HWND style not final).
                QtCore.QTimer.singleShot(100, self.setup_appbar)
                # Retry once if the first attempt could not register.
                QtCore.QTimer.singleShot(500, self._ensure_appbar_registered)
            if not self._fullscreen_timer.isActive():
                self._fullscreen_timer.start()

    def _ensure_appbar_registered(self):
        """Second-chance AppBar setup if the deferred first call did not stick."""
        if not self.settings.get("docked", True):
            return
        if getattr(self, "_appbar_registered", False):
            return
        print("[AppBar] Not registered yet — retrying setup_appbar")
        self.setup_appbar()

    def closeEvent(self, event):
        try:
            if hasattr(self, "_vblank_driver") and self._vblank_driver.isRunning():
                self._vblank_driver.stop()
                self._vblank_driver.wait(500)
        except Exception:
            pass
        for thread in list(getattr(self, "_slate_threads", [])):
            thread.wait(200)
        if hasattr(self, "scroll_timer") and self.scroll_timer.isActive():
            self.scroll_timer.stop()
        if hasattr(self, "_fullscreen_timer"):
            self._fullscreen_timer.stop()
        self.remove_appbar()
        event.accept()

    def nativeEvent(self, eventType, message):
        """Handle AppBar notifications (e.g. sister TCKR registering above us)."""
        if sys.platform == "win32":
            et = eventType
            if isinstance(et, bytes):
                et = et.decode("ascii", errors="ignore")
            if et == "windows_generic_MSG":
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_APPBAR and msg.wParam == ABN_POSCHANGED:
                    if getattr(self, "_appbar_registered", False) and not getattr(
                        self, "_appbar_passive_dock", False
                    ):
                        QtCore.QTimer.singleShot(
                            0,
                            lambda: self._refresh_appbar_geometry(
                                notify_work_area=False, reason="ABN_POSCHANGED"
                            ),
                        )
                    return True, 0
        return super().nativeEvent(eventType, message)

    # ------------------------------------------------------------------
    # AppBar / fullscreen (ported from MLB-TCKR, lean sister coordination)
    # ------------------------------------------------------------------
    def _is_primary_monitor_handle(self, hmonitor):
        if sys.platform != "win32" or not hmonitor:
            return True

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint32),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", ctypes.c_uint32),
            ]

        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmonitor, ctypes.byref(mi))
        return bool(mi.dwFlags & 0x00000001)  # MONITORINFOF_PRIMARY

    def _is_sister_ticker_hwnd(self, hwnd):
        """True for our bar or another *-TCKR / TCKR sister AppBar."""
        if sys.platform != "win32" or not hwnd:
            return False
        if hwnd == int(self.winId()):
            return True
        user32 = ctypes.windll.user32
        title_buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title_buf, 256)
        title = title_buf.value.strip()
        if title in ("TCKR", "MLB-TCKR", "NFL-TCKR") or title.endswith("-TCKR"):
            return True
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            kernel32 = ctypes.windll.kernel32
            hproc = kernel32.OpenProcess(0x1000, False, pid.value)
            if hproc:
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_ulong(1024)
                kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size))
                kernel32.CloseHandle(hproc)
                exe = os.path.basename(buf.value).lower()
                if exe in ("tckr.exe", "mlb-tckr.exe", "nfl-tckr.exe"):
                    return True
        return False

    def _other_sister_visible_on_monitor(self):
        """True while another sister ticker window is visible on our monitor."""
        if sys.platform != "win32":
            return False
        user32 = ctypes.windll.user32
        hmonitor = getattr(self, "_appbar_hmonitor", None)
        if not hmonitor:
            hmonitor = user32.MonitorFromWindow(int(self.winId()), 0x00000002)
        our_hwnd = int(self.winId())
        found = False
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _enum_cb(hwnd, _lp):
            nonlocal found
            if found:
                return True
            if hwnd == our_hwnd or not user32.IsWindowVisible(hwnd):
                return True
            if not self._is_sister_ticker_hwnd(hwnd):
                return True
            if user32.MonitorFromWindow(hwnd, 0x00000002) != hmonitor:
                return True
            found = True
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
        return found

    def _promote_passive_dock_to_appbar(self):
        """Claim the top AppBar slot after a sister ticker exits."""
        if not getattr(self, "_appbar_passive_dock", False):
            return
        print("[AppBar] Sister ticker exited — claiming top AppBar slot")
        self._appbar_passive_dock = False
        self._appbar_stack_top_phys = 0
        self._appbar_reserved_phys = None
        self._sister_gone_streak = 0
        self._force_shell_appbar = True
        if getattr(self, "_appbar_on_primary", True):
            ctypes.windll.user32.SystemParametersInfoW(0x002F, 0, None, 0)
        self.setup_appbar()
        self._force_shell_appbar = False

    def _check_sister_appbar_layout(self):
        """Promote from passive dock when the sister above us leaves."""
        if sys.platform != "win32":
            return
        if not self.settings.get("docked", True):
            return
        if not getattr(self, "_appbar_passive_dock", False):
            return
        if self._other_sister_visible_on_monitor():
            self._sister_gone_streak = 0
            return
        # Sister gone — wait one poll so ABM_REMOVE can settle, then promote.
        streak = getattr(self, "_sister_gone_streak", 0) + 1
        self._sister_gone_streak = streak
        if streak >= 1:
            self._promote_passive_dock_to_appbar()

    def _work_area_notify_allowed(self):
        return getattr(self, "_appbar_stack_top_phys", 0) == 0

    def _notify_same_monitor_work_area_change(self):
        """Tell same-monitor windows to refresh work-area (no HWND_BROADCAST)."""
        if sys.platform != "win32":
            return
        hmonitor = getattr(self, "_appbar_hmonitor", None)
        if not hmonitor:
            return
        user32 = ctypes.windll.user32
        WM_SETTINGCHANGE = 0x001A
        SPI_SETWORKAREA = 0x002F
        # Nudge Explorer / WM to re-read work area after AppBar SETPOS.
        if self._is_primary_monitor_handle(hmonitor):
            user32.SystemParametersInfoW(SPI_SETWORKAREA, 0, None, 0)
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _enum_cb(hwnd, _lp):
            if self._is_sister_ticker_hwnd(hwnd):
                return True
            if not user32.IsWindow(hwnd):
                return True
            if user32.MonitorFromWindow(hwnd, 0x00000002) != hmonitor:
                return True
            user32.PostMessageW(hwnd, WM_SETTINGCHANGE, SPI_SETWORKAREA, 0)
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)

    def _force_work_area_below_strip(self, hmonitor, strip_bottom_phys):
        """If ABM_SETPOS did not shrink the work area, set it explicitly.

        Preserves left/right/bottom from the current work rect (taskbar, etc.)
        and only pushes the top edge down to clear our reserved strip.
        """
        if sys.platform != "win32" or not hmonitor:
            return False

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint32),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", ctypes.c_uint32),
            ]

        user32 = ctypes.windll.user32
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(mi)):
            return False
        # Already clear of our strip — shell AppBar path worked.
        if mi.rcWork.top >= int(strip_bottom_phys):
            return False
        rect = wintypes.RECT()
        rect.left = mi.rcWork.left
        rect.top = int(strip_bottom_phys)
        rect.right = mi.rcWork.right
        rect.bottom = mi.rcWork.bottom
        if rect.top >= rect.bottom:
            return False
        SPIF_UPDATEINIFILE = 0x01
        SPIF_SENDCHANGE = 0x02
        ok = user32.SystemParametersInfoW(
            0x002F, 0, ctypes.byref(rect), SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
        )
        if ok:
            print(
                f"[AppBar] Forced SPI_SETWORKAREA top={rect.top} "
                f"(was {mi.rcWork.top}; shell did not shrink work area)"
            )
        return bool(ok)

    def _schedule_workarea_rebroadcasts(self):
        """Re-notify same-monitor windows after AppBar registration settles."""
        if not getattr(self, "_appbar_on_primary", False):
            return
        self._workarea_broadcast_gen = getattr(self, "_workarea_broadcast_gen", 0) + 1
        gen = self._workarea_broadcast_gen

        def _rebroadcast():
            if gen != getattr(self, "_workarea_broadcast_gen", 0):
                return
            if not getattr(self, "_appbar_registered", False):
                return
            self._notify_same_monitor_work_area_change()

        QtCore.QTimer.singleShot(1000, _rebroadcast)
        QtCore.QTimer.singleShot(3000, _rebroadcast)

    def _appbar_slot_top_phys(self, phys_y, work_top_offset, phys_height):
        """Physical Y offset from monitor top where our AppBar strip should start."""
        old = getattr(self, "_appbar_reserved_phys", None)
        if old is not None:
            our_top = old[1] - phys_y
            our_bottom = old[3] - phys_y
            if work_top_offset >= our_bottom:
                return our_top
            if work_top_offset > our_top:
                return work_top_offset
            return our_top
        return work_top_offset

    def _refresh_appbar_geometry(self, notify_work_area=False, reason=""):
        if sys.platform != "win32":
            return
        if not getattr(self, "_appbar_registered", False):
            return

        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32
        hwnd = int(self.winId())
        screen = getattr(self, "_target_screen", QtWidgets.QApplication.primaryScreen())
        dpr = float(screen.devicePixelRatio()) if screen else 1.0

        client_rect = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(client_rect))
        phys_height = client_rect.bottom - client_rect.top
        if phys_height <= 0:
            phys_height = max(1, math.ceil(self.ticker_height * dpr))

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint32),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", ctypes.c_uint32),
            ]

        hmonitor = user32.MonitorFromWindow(hwnd, 0x00000002)
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        user32.GetMonitorInfoW(hmonitor, ctypes.byref(mi))

        phys_x = mi.rcMonitor.left
        phys_y = mi.rcMonitor.top
        phys_width = mi.rcMonitor.right - mi.rcMonitor.left
        work_top_offset = mi.rcWork.top - phys_y
        slot_top = self._appbar_slot_top_phys(phys_y, work_top_offset, phys_height)

        abd = APPBARDATA()
        abd.cbSize = ctypes.sizeof(APPBARDATA)
        abd.hWnd = hwnd
        abd.uCallbackMessage = WM_APPBAR
        abd.uEdge = ABE_TOP
        abd.rc.left = phys_x
        abd.rc.top = phys_y + slot_top
        abd.rc.right = phys_x + phys_width
        abd.rc.bottom = abd.rc.top + phys_height

        shell32.SHAppBarMessage(ABM_QUERYPOS, ctypes.byref(abd))
        abd.rc.bottom = abd.rc.top + phys_height

        new_reserved = (
            int(abd.rc.left),
            int(abd.rc.top),
            int(abd.rc.right),
            int(abd.rc.bottom),
        )
        old_reserved = getattr(self, "_appbar_reserved_phys", None)
        if old_reserved == new_reserved:
            return

        if old_reserved is None or slot_top != old_reserved[1] - phys_y:
            self._appbar_stack_top_phys = int(abd.rc.top - phys_y)

        shell32.SHAppBarMessage(ABM_SETPOS, ctypes.byref(abd))
        self._appbar_reserved_phys = new_reserved
        self._appbar_hmonitor = hmonitor

        HWND_TOPMOST = -1
        user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            int(abd.rc.left),
            int(abd.rc.top),
            int(abd.rc.right - abd.rc.left),
            int(abd.rc.bottom - abd.rc.top),
            0x0010,  # SWP_NOACTIVATE
        )
        shell32.SHAppBarMessage(ABM_WINDOWPOSCHANGED, ctypes.byref(abd))
        shell32.SHAppBarMessage(ABM_ACTIVATE, ctypes.byref(abd))
        self._force_work_area_below_strip(hmonitor, abd.rc.bottom)

        if notify_work_area:
            self._notify_same_monitor_work_area_change()

        tag = f" ({reason})" if reason else ""
        print(
            f"[AppBar] Refreshed position{tag} — "
            f"reserved phys=({abd.rc.left},{abd.rc.top},"
            f"{abd.rc.right},{abd.rc.bottom})"
        )

    def setup_appbar(self):
        """Register as Windows AppBar to reserve desktop space at the top.

        Always registers with the shell when docked. ABM_QUERYPOS stacks us below
        any existing top reservations (taskbar / sister TCKR). The old "passive
        dock" path skipped ABM_SETPOS entirely, so the bar never reserved space.
        """
        if sys.platform != "win32":
            return
        if not self.settings.get("docked", True):
            print("[AppBar] docked=False — skipping reservation (floating mode)")
            return

        # Avoid double ABM_NEW if already registered
        if getattr(self, "_appbar_registered", False):
            self._refresh_appbar_geometry(notify_work_area=True, reason="setup_refresh")
            return

        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32
        hwnd = int(self.winId())
        if hwnd == 0:
            print("[AppBar] winId() is 0 — cannot register yet")
            return

        screen = getattr(self, "_target_screen", QtWidgets.QApplication.primaryScreen())
        dpr = float(screen.devicePixelRatio()) if screen else 1.0

        client_rect = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(client_rect))
        phys_height = client_rect.bottom - client_rect.top
        if phys_height <= 0:
            phys_height = max(1, math.ceil(self.ticker_height * dpr))
            print(
                f"[AppBar] Warning: GetClientRect height=0; "
                f"fallback phys_height={phys_height}px"
            )

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint32),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", ctypes.c_uint32),
            ]

        hmonitor = user32.MonitorFromWindow(hwnd, 0x00000002)
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        user32.GetMonitorInfoW(hmonitor, ctypes.byref(mi))

        phys_x = mi.rcMonitor.left
        phys_y = mi.rcMonitor.top
        phys_width = mi.rcMonitor.right - mi.rcMonitor.left
        prior_top_reserved = mi.rcWork.top - phys_y
        self._appbar_stack_top_phys = int(max(0, prior_top_reserved))

        if prior_top_reserved > 0:
            print(
                f"[AppBar] {prior_top_reserved}px already reserved at top — "
                f"QUERYPOS will stack NFL-TCKR below it (still registering AppBar)"
            )

        self._appbar_passive_dock = False

        abd = APPBARDATA()
        abd.cbSize = ctypes.sizeof(APPBARDATA)
        abd.hWnd = hwnd
        abd.uCallbackMessage = WM_APPBAR
        abd.uEdge = ABE_TOP
        abd.rc.left = phys_x
        abd.rc.top = phys_y + max(0, prior_top_reserved)
        abd.rc.right = phys_x + phys_width
        abd.rc.bottom = abd.rc.top + phys_height

        new_result = shell32.SHAppBarMessage(ABM_NEW, ctypes.byref(abd))
        if not new_result:
            print(
                f"[AppBar] ABM_NEW failed (hwnd={hwnd}) — "
                f"desktop reservation unavailable"
            )
            return
        self._appbar_registered = True

        shell32.SHAppBarMessage(ABM_QUERYPOS, ctypes.byref(abd))
        # Preserve our exact height from the (possibly adjusted) top.
        abd.rc.bottom = abd.rc.top + phys_height
        shell32.SHAppBarMessage(ABM_SETPOS, ctypes.byref(abd))

        self._appbar_reserved_phys = (
            int(abd.rc.left),
            int(abd.rc.top),
            int(abd.rc.right),
            int(abd.rc.bottom),
        )
        self._appbar_hmonitor = hmonitor
        self._appbar_stack_top_phys = int(abd.rc.top - phys_y)

        # Physical SetWindowPos — Qt setGeometry remaps via primary DPI.
        HWND_TOPMOST = -1
        SWP_NOACTIVATE = 0x0010
        user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            int(abd.rc.left),
            int(abd.rc.top),
            int(abd.rc.right - abd.rc.left),
            int(abd.rc.bottom - abd.rc.top),
            SWP_NOACTIVATE,
        )
        shell32.SHAppBarMessage(ABM_WINDOWPOSCHANGED, ctypes.byref(abd))
        shell32.SHAppBarMessage(ABM_ACTIVATE, ctypes.byref(abd))

        self._appbar_on_primary = self._is_primary_monitor_handle(hmonitor)
        # If the shell did not shrink the work area, force it (common with Tool windows).
        self._force_work_area_below_strip(hmonitor, abd.rc.bottom)
        self._notify_same_monitor_work_area_change()
        self._schedule_workarea_rebroadcasts()

        _logical_height = int((abd.rc.bottom - abd.rc.top) / max(dpr, 0.01))
        print(
            f"[AppBar] Registered — DPR={dpr}, "
            f"monitor phys=({phys_x},{phys_y},{phys_x + phys_width},{phys_y + phys_height}), "
            f"reserved phys=({abd.rc.left},{abd.rc.top},{abd.rc.right},{abd.rc.bottom}), "
            f"logical height={_logical_height}px"
        )

    def remove_appbar(self):
        """Unregister AppBar and release reserved desktop space."""
        if sys.platform != "win32":
            return
        if getattr(self, "_appbar_passive_dock", False):
            self._appbar_passive_dock = False
            self._appbar_reserved_phys = None
            print("[AppBar] Passive dock released")
            return
        if not getattr(self, "_appbar_registered", False):
            return
        was_primary = getattr(self, "_appbar_on_primary", False)
        hmonitor = getattr(self, "_appbar_hmonitor", None)
        self._appbar_registered = False
        try:
            shell32 = ctypes.windll.shell32
            abd = APPBARDATA()
            abd.cbSize = ctypes.sizeof(APPBARDATA)
            abd.hWnd = int(self.winId())
            abd.uCallbackMessage = WM_APPBAR
            shell32.SHAppBarMessage(ABM_REMOVE, ctypes.byref(abd))
            self._appbar_reserved_phys = None
            # Undo any forced SPI work-area inset: ask shell to republish.
            if was_primary:
                ctypes.windll.user32.SystemParametersInfoW(0x002F, 0, None, 0x01 | 0x02)
            if hmonitor:
                self._appbar_hmonitor = hmonitor
                self._notify_same_monitor_work_area_change()
            print("[AppBar] Unregistered — desktop space released")
        except Exception as e:
            print(f"[AppBar] Warning: ABM_REMOVE failed: {e}")

    def _check_fullscreen(self):
        """Hide ticker (opacity 0) when a fullscreen app owns our monitor."""
        if sys.platform != "win32":
            return
        self._check_sister_appbar_layout()
        try:
            user32 = ctypes.windll.user32
            fg_hwnd = user32.GetForegroundWindow()
            our_hwnd = int(self.winId())
            if fg_hwnd == 0 or fg_hwnd == our_hwnd:
                return

            class_buf = ctypes.create_unicode_buffer(512)
            user32.GetClassNameW(fg_hwnd, class_buf, 512)
            window_class = class_buf.value or ""
            if window_class in {
                "Progman",
                "WorkerW",
                "Shell_TrayWnd",
                "Shell_SecondaryTrayWnd",
            }:
                return

            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(fg_hwnd, ctypes.byref(pid))
            exe_name = ""
            if pid.value:
                kernel32 = ctypes.windll.kernel32
                hproc = kernel32.OpenProcess(0x1000, False, pid.value)
                if hproc:
                    exe_buf = ctypes.create_unicode_buffer(1024)
                    exe_size = ctypes.c_ulong(1024)
                    kernel32.QueryFullProcessImageNameW(
                        hproc, 0, exe_buf, ctypes.byref(exe_size)
                    )
                    kernel32.CloseHandle(hproc)
                    exe_name = os.path.basename(exe_buf.value).lower()

            if exe_name and exe_name in self._fullscreen_override_exes:
                return

            _SCREENSHOT_TOOLS = frozenset(
                {
                    "greenshot.exe",
                    "sharex.exe",
                    "snagit32.exe",
                    "snagit64.exe",
                    "snagiteditor.exe",
                    "screenpresso.exe",
                    "picpick.exe",
                    "lightshot.exe",
                    "gyazo.exe",
                    "hypersnap.exe",
                    "flameshot.exe",
                }
            )
            if exe_name and exe_name in _SCREENSHOT_TOOLS:
                return

            fg_rect = wintypes.RECT()
            user32.GetWindowRect(fg_hwnd, ctypes.byref(fg_rect))
            fw = fg_rect.right - fg_rect.left
            fh = fg_rect.bottom - fg_rect.top
            if fw <= 0 or fh <= 0:
                return

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_uint32),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", ctypes.c_uint32),
                ]

            hmon = user32.MonitorFromWindow(fg_hwnd, 0x00000002)
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
            mw = mi.rcMonitor.right - mi.rcMonitor.left
            mh = mi.rcMonitor.bottom - mi.rcMonitor.top

            style = user32.GetWindowLongW(fg_hwnd, -16)
            exstyle = user32.GetWindowLongW(fg_hwnd, -20)
            has_caption = bool(style & 0x00C00000)
            is_clickthru = bool(exstyle & 0x00000020)
            is_layered = bool(exstyle & 0x00080000)
            covers_monitor = fw >= mw and fh >= mh
            is_fullscreen = (
                covers_monitor
                and not has_caption
                and not is_clickthru
                and not is_layered
            )

            title_buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(fg_hwnd, title_buf, 512)
            window_title = title_buf.value or "(untitled)"

            our_hmon = user32.MonitorFromWindow(our_hwnd, 0x00000002)
            same_monitor = hmon == our_hmon
            effective_fullscreen = is_fullscreen and same_monitor

            if effective_fullscreen != self._ticker_hidden_for_fullscreen:
                print(
                    f"[TICKER] _check_fullscreen: '{window_title}' "
                    f"exe={exe_name or '?'} hwnd={fg_hwnd}"
                )
                print(
                    f"         window {fw}x{fh} vs monitor {mw}x{mh}, "
                    f"covers={covers_monitor}, caption={has_caption}, "
                    f"fullscreen={is_fullscreen}, same_monitor={same_monitor}"
                )

            if is_fullscreen and same_monitor and not self._ticker_hidden_for_fullscreen:
                self._ticker_hidden_for_fullscreen = True
                # Opacity (not hide) keeps AppBar registration / reserved strip stable.
                self.setWindowOpacity(0.0)
                print(
                    f"[TICKER] Full-screen app detected: '{window_title}' — ticker hidden"
                )
            elif (
                not is_fullscreen or not same_monitor
            ) and self._ticker_hidden_for_fullscreen:
                self._ticker_hidden_for_fullscreen = False
                self.setWindowOpacity(1.0)
                print("[TICKER] Full-screen app gone — ticker restored")
        except Exception as e:
            print(f"[TICKER] _check_fullscreen error: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    _ensure_appdata()
    # High-DPI
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("NFL-TCKR")
    print(f"[NFL-TCKR] v{VERSION}", flush=True)
    print(
        f"[NFL-TCKR] DEBUG={'ON' if _NFL_DEBUG else 'OFF'} (env NFL_TCKR_DEBUG)",
        flush=True,
    )
    if _NFL_SCROLL_DEBUG:
        print(
            f"[NFL-TCKR] SCROLL_DEBUG=ON "
            f"(--debug={_cli_debug}; SCROLL_DEBUG={SCROLL_DEBUG}; "
            f"env NFL_TCKR_SCROLL_DEBUG={_env_scroll or 'unset'})",
            flush=True,
        )
        print("[SCROLL] profiler enabled", flush=True)
    else:
        print(
            "[NFL-TCKR] Scroll debug off — run with --debug "
            "(or NFL_TCKR_SCROLL_DEBUG=1)",
            flush=True,
        )
    if _cli_faststart:
        print("[NFL-TCKR] --faststart: skip title hold", flush=True)
    print(f"[NFL-TCKR] logos: {LOGO_DIR}", flush=True)
    print(
        f"[NFL-TCKR] football icon: {FOOTBALL_ICON_PATH} "
        f"({'ok' if os.path.isfile(FOOTBALL_ICON_PATH) else 'MISSING'})",
        flush=True,
    )
    print(
        f"[NFL-TCKR] loop logo: {NFL_COM_LOGO_PATH} "
        f"({'ok' if os.path.isfile(NFL_COM_LOGO_PATH) else 'MISSING'})",
        flush=True,
    )
    ticker = NFLTicker()
    app.aboutToQuit.connect(ticker.remove_appbar)
    app.aboutToQuit.connect(
        lambda: (
            ticker._vblank_driver.stop()
            if hasattr(ticker, "_vblank_driver")
            else None
        )
    )
    ticker.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
