"""
Author: Paul R. Charovkine <krypdoh@gmail.com>
Program: NFL-TCKR.py
Date: 2026.0927
Copyright: 2026 Paul R. Charovkine

Description:
NFL ticker application that displays live football game data in a scrolling
ticker bar — logos, colored names, scores, down & distance, last play, QB
stats, ball-on, possession, and optional post-game leaders. Data via ESPN
public site API. Integrates with Windows AppBar for docked desktop reservation
(same model as MLB-TCKR).
"""

VERSION = "0.1.47"

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
SCORE_ALERT_HEADLINE_MS = 3000  # hold: all-caps type line
SCORE_ALERT_DETAIL_MS = 4000  # hold: play detail line
SCORE_ALERT_HOLD_MS = SCORE_ALERT_HEADLINE_MS + SCORE_ALERT_DETAIL_MS  # 7000
SCORE_ALERT_IN_MS = 600
SCORE_ALERT_OUT_MS = 400
TEST_ADVANCE_MS = 4500  # fake live slate clock / play tick (-test only)
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
_cli_test = "-test" in sys.argv or "--test" in sys.argv
sys.argv = [
    a for a in sys.argv
    if a not in ("--debug", "--faststart", "-test", "--test")
]
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


_GENERIC_TICKER_FONTS = frozenset({
    "arial", "arial black", "segoe ui", "tahoma", "calibri", "verdana",
    "ms shell dlg", "ms shell dlg 2", "sans-serif", "sans serif",
})

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
        "include_postgame_stats": False,
        "postgame_font": "Gotham Black",
        "include_scheduled_games": True,
        "live_games_only": False,
        "show_last_play": True,
        "show_drive_summary": False,
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
        "use_proxy": False,
        "proxy": "",
        "use_cert": False,
        "cert_file": "",
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            defaults.update(saved)
        except Exception:
            pass
    saved_font = str(defaults.get("font") or "").strip()
    if saved_font.lower() in _GENERIC_TICKER_FONTS:
        defaults["font"] = "Ozone"
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


def normalize_proxy_url(proxy_value):
    """Ensure proxy URL has a scheme prefix (http:// added if missing)."""
    if not proxy_value:
        return ""
    proxy_value = str(proxy_value).strip()
    if not proxy_value:
        return ""
    if not proxy_value.lower().startswith(("http://", "https://")):
        proxy_value = f"http://{proxy_value}"
    return proxy_value


_SYSTEM_CA_BUNDLE_PATH = ""


def _build_system_ca_bundle():
    """Merge certifi with Windows CA/ROOT stores for corporate SSL inspection."""
    global _SYSTEM_CA_BUNDLE_PATH
    if _SYSTEM_CA_BUNDLE_PATH and os.path.isfile(_SYSTEM_CA_BUNDLE_PATH):
        return _SYSTEM_CA_BUNDLE_PATH
    try:
        import base64
        import ssl
        import certifi

        with open(certifi.where(), "rb") as fh:
            bundle = fh.read()
        added = 0
        if sys.platform == "win32":
            for store in ("CA", "ROOT"):
                try:
                    for cert_bytes, encoding, _trust in ssl.enum_certificates(store):
                        if isinstance(cert_bytes, bytes) and encoding == "x509_asn":
                            pem = (
                                b"-----BEGIN CERTIFICATE-----\n"
                                + base64.encodebytes(cert_bytes)
                                + b"-----END CERTIFICATE-----\n"
                            )
                            bundle += pem
                            added += 1
                except Exception:
                    pass
        os.makedirs(APPDATA_DIR, exist_ok=True)
        dest = os.path.join(APPDATA_DIR, "system_ca_bundle.pem")
        with open(dest, "wb") as fh:
            fh.write(bundle)
        _SYSTEM_CA_BUNDLE_PATH = dest
        print(f"[SSL] System CA bundle built: {added} system root(s) → {dest}")
        return dest
    except Exception as exc:
        print(f"[SSL] Could not build system CA bundle: {exc}")
        return ""


def apply_proxy_settings():
    """Push proxy/cert into env so requests (and ESPN fetches) pick them up."""
    settings = get_settings()
    proxy_value = normalize_proxy_url(settings.get("proxy", ""))
    if settings.get("use_proxy") and proxy_value:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ[key] = proxy_value
        print(f"[PROXY] Enabled: {proxy_value}")
    else:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(key, None)

    cert_file = settings.get("cert_file", "") or ""
    if settings.get("use_cert") and cert_file and os.path.isfile(cert_file):
        os.environ["REQUESTS_CA_BUNDLE"] = cert_file
        os.environ["SSL_CERT_FILE"] = cert_file
        print(f"[PROXY] Certificate: {cert_file}")
        return

    if settings.get("use_proxy") and proxy_value:
        sys_bundle = _build_system_ca_bundle()
        if sys_bundle:
            os.environ["REQUESTS_CA_BUNDLE"] = sys_bundle
            os.environ["SSL_CERT_FILE"] = sys_bundle
            print(f"[PROXY] Using system CA bundle: {sys_bundle}")
            return

    local = os.environ.get("LOCALAPPDATA", "")
    appdata_cacert = (
        os.path.join(local, "NFL-TCKR", "certifi", "cacert.pem") if local else ""
    )
    if appdata_cacert and os.path.isfile(appdata_cacert):
        os.environ["REQUESTS_CA_BUNDLE"] = appdata_cacert
        os.environ["SSL_CERT_FILE"] = appdata_cacert
    elif getattr(sys, "_MEIPASS", None):
        meipass_cacert = os.path.join(sys._MEIPASS, "certifi", "cacert.pem")
        if os.path.isfile(meipass_cacert):
            os.environ["REQUESTS_CA_BUNDLE"] = meipass_cacert
            os.environ["SSL_CERT_FILE"] = meipass_cacert
    else:
        os.environ.pop("REQUESTS_CA_BUNDLE", None)


def _request_proxies():
    """requests proxies dict, or None when proxy is off."""
    settings = get_settings()
    url = normalize_proxy_url(settings.get("proxy", ""))
    if settings.get("use_proxy") and url:
        return {"http": url, "https": url}
    return None


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
_BUNDLED_FONT_FAMILIES = {}  # family name -> source filename


def _font_search_dirs():
    """Folders that may hold ticker .ttf/.otf files (dev tree, onefile extract, AppData)."""
    dirs = []

    def add(path):
        if path and path not in dirs:
            dirs.append(path)

    add(os.path.join(APP_DIR, "fonts"))
    add(APP_DIR)
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        add(os.path.join(exe_dir, "fonts"))
        add(exe_dir)
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            add(os.path.join(meipass, "fonts"))
            add(meipass)
    else:
        add(os.path.join(REPO_ROOT, "fonts"))
        add(os.path.join(REPO_ROOT, "docs"))
    add(os.path.join(APPDATA_DIR, "fonts"))
    add(APPDATA_DIR)
    return dirs


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
    """ttf/otf files shipped in the project fonts folder (or the frozen extract)."""
    seen = set()
    for folder in _font_search_dirs():
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


def register_all_font_files():
    """Register every bundled .ttf/.otf with Qt. Call after QApplication exists."""
    global _BUNDLED_FONT_FAMILIES, _SYSTEM_FONT_FAMILIES
    if _SYSTEM_FONT_FAMILIES is None:
        _SYSTEM_FONT_FAMILIES = set(QtGui.QFontDatabase().families())
    registered = 0
    for path in _bundled_font_files():
        fid = QtGui.QFontDatabase.addApplicationFont(path)
        if fid < 0:
            print(f"[FONT] failed to register {os.path.basename(path)}", flush=True)
            continue
        registered += 1
        for fam in QtGui.QFontDatabase.applicationFontFamilies(fid) or []:
            if fam not in _BUNDLED_FONT_FAMILIES:
                _BUNDLED_FONT_FAMILIES[fam] = os.path.basename(path)
                print(f"[FONT] Registered '{fam}' from {os.path.basename(path)}", flush=True)
    print(
        f"[NFL-TCKR] bundled fonts: {registered} file(s) "
        f"({len(_BUNDLED_FONT_FAMILIES)} family name(s))",
        flush=True,
    )
    return registered


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

    Frozen (exe) prefers a fonts-folder face so the bundle looks the same
    on machines that do not have Ozone/Gotham installed.
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
    frozen = bool(getattr(sys, "frozen", False))

    def _from_bundle(name):
        best = None
        best_score = 0
        for fam in _BUNDLED_FONT_FAMILIES:
            score = _font_name_score(name, fam)
            if score > best_score:
                best_score = score
                best = fam
        if best:
            return best
        return _load_bundled_family(name)

    def _pick(name, via_default):
        if not name:
            return None, None
        if frozen:
            loaded = _from_bundle(name)
            if loaded:
                families.add(loaded)
                src = _BUNDLED_FONT_FAMILIES.get(loaded, "")
                extra = f" fonts/{src}" if src else ""
                source = (
                    "default, fonts folder" if via_default else f"fonts folder{extra}"
                )
                return loaded, source
        if name in _SYSTEM_FONT_FAMILIES or name in families:
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


def _ticker_font_request(settings):
    """Ticker LED face: Ozone from fonts/, never leftover Arial from Settings.json."""
    requested = str(settings.get("font") or "").strip() or "Ozone"
    if requested.lower() in _GENERIC_TICKER_FONTS:
        return "Ozone"
    if _BUNDLED_FONT_FAMILIES:
        if any(_font_name_score(requested, fam) > 0 for fam in _BUNDLED_FONT_FAMILIES):
            return requested
        return "Ozone"
    return requested


def _postgame_font_request(settings):
    """Post-game stats face from Settings; generic leftovers fall back to Gotham Black."""
    requested = str(settings.get("postgame_font") or "").strip()
    if not requested:
        requested = str(settings.get("player_info_font") or "").strip() or "Gotham Black"
    if requested.lower() in _GENERIC_TICKER_FONTS:
        return "Gotham Black"
    return requested


def _bundled_font_face_names():
    bundled = sorted(_BUNDLED_FONT_FAMILIES.keys(), key=str.lower)
    faces = ["Ozone"] + [f for f in bundled if f.lower() != "ozone"]
    return faces or ["Ozone"]


def _fill_font_combo(combo, selected):
    for face in _bundled_font_face_names():
        combo.addItem(face)
    want = (selected or "").strip()
    idx = combo.findText(want) if want else -1
    if idx < 0 and want:
        combo.addItem(want)
        idx = combo.findText(want)
    combo.setCurrentIndex(max(0, idx))


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
        r = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            proxies=_request_proxies(),
        )
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
    """Split a down line so '&' and the 'on'/'ON' connector can be drawn white.

    Only the standalone separator tokens are marked — '&' as a character and
    'on'/'ON' as a whole word (word boundaries, case-insensitive), so letters
    inside team names or other text are not whitened.
    """
    parts = []
    src = text or ""
    pos = 0
    for m in re.finditer(r"&|\bon\b", src, flags=re.IGNORECASE):
        if m.start() > pos:
            parts.append((src[pos:m.start()], False))
        parts.append((m.group(), True))
        pos = m.end()
    if pos < len(src):
        parts.append((src[pos:], False))
    return parts


def _as_bool(val):
    """Coerce ESPN JSON booleans that sometimes arrive as strings."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes")
    return bool(val)


def _is_fourth_down(situation, down_text=""):
    """True when ESPN situation is 4th down (numeric or '4th…' text)."""
    try:
        if int(situation.get("down")) == 4:
            return True
    except (TypeError, ValueError, AttributeError):
        pass
    text = (down_text or "").strip().lower()
    if not text and isinstance(situation, dict):
        text = (
            situation.get("downDistanceText")
            or situation.get("shortDownDistanceText")
            or ""
        ).strip().lower()
    return text.startswith("4th")


def _short_network_name(name):
    """Map ESPN broadcast labels to short ticker names (CBS, AMZN, …)."""
    raw = (name or "").strip()
    if not raw:
        return ""
    key = re.sub(r"\s+", " ", raw).lower()
    mapped = {
        "prime video": "AMZN",
        "amazon prime video": "AMZN",
        "amazon prime": "AMZN",
        "amazon": "AMZN",
        "nfl network": "NFLN",
        "nfln": "NFLN",
        "espn+": "ESPN+",
        "espn plus": "ESPN+",
        "disney+": "DISNEY+",
        "peacock": "PEACOCK",
        "netflix": "NFLX",
        "paramount+": "P+",
        "paramount plus": "P+",
    }
    if key in mapped:
        return mapped[key]
    # Already short network codes (CBS, FOX, NBC, ESPN, ABC, …)
    if len(raw) <= 5:
        return raw.upper()
    return raw


def _competition_broadcast(comp):
    """One short national (or first) broadcast name from competition media."""
    for bc in (comp or {}).get("broadcasts") or []:
        names = bc.get("names") or []
        if names:
            short = _short_network_name(names[0])
            if short:
                return short
    for geo in (comp or {}).get("geoBroadcasts") or []:
        media = geo.get("media") or {}
        short = _short_network_name(
            media.get("shortName") or media.get("name") or geo.get("type", {}).get("shortName")
        )
        if short:
            return short
    return ""


def _competition_spread(comp):
    """Pregame spread text like 'NYJ -3.5' from competition odds details."""
    odds = (comp or {}).get("odds") or []
    if isinstance(odds, dict):
        odds = [odds]
    for entry in odds:
        if not isinstance(entry, dict):
            continue
        details = (entry.get("details") or "").strip()
        if details:
            return details
        spread = entry.get("spread")
        if spread is None:
            continue
        fav = ""
        for side in ("homeTeamOdds", "awayTeamOdds"):
            side_odds = entry.get(side) or {}
            if side_odds.get("favorite"):
                team = side_odds.get("team") or {}
                fav = (team.get("abbreviation") or "").strip()
                if fav:
                    break
        try:
            spr = abs(float(spread))
        except (TypeError, ValueError):
            continue
        if fav:
            return f"{fav} -{spr:g}"
    return ""


def _drive_summary_line(summary):
    """Short current-drive line: '12 plays, 75 yards'. Empty if incomplete."""
    drives = (summary or {}).get("drives") or {}
    current = drives.get("current")
    if not current:
        return ""
    plays = current.get("offensivePlays")
    yards = current.get("yards")
    if plays is None or yards is None:
        # Some payloads only put the short form in description.
        desc = (current.get("description") or "").strip()
        match = re.match(
            r"(\d+)\s+plays?,\s+(-?\d+)\s+yards?",
            desc,
            flags=re.IGNORECASE,
        )
        if not match:
            return ""
        plays, yards = match.group(1), match.group(2)
    try:
        plays_n = int(plays)
        yards_n = int(yards)
    except (TypeError, ValueError):
        return ""
    return f"{plays_n} plays, {yards_n} yards"


def _sit_word_color(game, clock_blue, settings):
    """Fill color for down/distance words (markers stay white separately).

    Priority (highest wins): 4th down gold, else red zone, else possession
    team color, else clock-blue / white.
    """
    if game.get("is_fourth_down"):
        return "#FFD700"
    if game.get("is_red_zone"):
        return "#FF4040"
    poss = str(game.get("possession_id") or "")
    if poss:
        if poss == str(game.get("away_id") or ""):
            return get_team_color(game.get("away_name") or "", settings)
        if poss == str(game.get("home_id") or ""):
            return get_team_color(game.get("home_name") or "", settings)
    return "#00BFFF" if clock_blue else "#FFFFFF"


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
                      play_lines, gap=1, max_play_lines=2):
    """Fixed vertical slots for live clock / down / play wrap lines.

    The clock row is always the same, even when down or last play is empty.
    Slots are centered in the bar as a group so the time sits in the upper
    half (above the score midline) without hugging the top edge.
    Last-play lines are nudged down slightly; clock and down stay put.
    max_play_lines is 2 or 3 — height fit may drop from 3 to 2.
    """
    play_nudge = 2  # px; last-play only — do not move clock / down
    n_play = 3 if int(max_play_lines) >= 3 else 2
    clock_h, clock_a = _glyph_line(time_m, clock_text or "0:00 - 2ND")
    sit_h, sit_a = _glyph_line(sit_m, situation_line or "1ST & 10 ON AAA 00")
    play_sample = (play_lines[0] if play_lines else "LAST PLAY")
    play_h, play_a = _glyph_line(play_m, play_sample)
    clock_to_sit = max(4, gap + 3)
    sit_to_play = max(3, gap + 2)
    play_to_play = gap
    reserved = (
        clock_h + sit_h + n_play * play_h
        + clock_to_sit + sit_to_play + play_to_play * (n_play - 1)
    )
    y0 = max(2, (int(height) - reserved) // 2)
    sit_y = y0 + clock_h + clock_to_sit
    play_y = sit_y + sit_h + sit_to_play + play_nudge
    play2_y = play_y + play_h + play_to_play
    play3_y = play2_y + play_h + play_to_play
    # Keep play lines inside the bar if the nudge would clip the bottom.
    last_bottom = (play3_y if n_play >= 3 else play2_y) + play_h
    overflow = last_bottom - int(height)
    if overflow > 0:
        play_y = max(sit_y + sit_h + sit_to_play, play_y - overflow)
        play2_y = play_y + play_h + play_to_play
        play3_y = play2_y + play_h + play_to_play
    return {
        "time": (y0, clock_a),
        "sit": (sit_y, sit_a),
        "play": (play_y, play_a),
        "play2": (play2_y, play_a),
        "play3": (play3_y, play_a),
        "reserved": reserved,
        "max_play_lines": n_play,
    }


def _prepare_linescore(font, game, height, heading="", heading_font=None):
    """Period table (Q1–Q4, OT if present): header, away, home. No total column.

    None if ESPN sent no lines. OT / 2OT columns appear only when linescores
    include those periods — never invented.
    """
    away_q = list(game.get("away_lines") or [])
    home_q = list(game.get("home_lines") or [])
    if not away_q and not home_q:
        return None
    n = max(len(away_q), len(home_q), 4)

    def _pad(vals):
        vals = list(vals) + [""] * (n - len(vals))
        return [str(v) for v in vals[:n]]

    rows = [
        [""] + [_period_header(i) for i in range(n)],
        [str(game.get("away_abbr") or "").upper()] + _pad(away_q),
        [str(game.get("home_abbr") or "").upper()] + _pad(home_q),
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


def _qb_line_segments(name, yds, td, inter, jersey="", catt=""):
    """Mixed-size parts for under-name QB stats (same face; labels 1px smaller).

    Format: #16 Lawrence 20/33 182 YDS, 3 TD, 1 INT
    Full size (True): #jersey, last name, C/ATT, yards, TD count, INT count.
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
    ])
    if catt:
        segs.extend([
            (str(catt), True),
            (" ", False),
        ])
    segs.extend([
        (str(yds), True),
        (" YDS, ", False),
        (str(td), True),
        (" TD, ", False),
        (str(inter), True),
        (" INT", False),
    ])
    return segs


def _format_qb_line(name, yds, td, inter, jersey="", catt=""):
    """Plain-text QB stats line (debug / visual-key); see _qb_line_segments for size."""
    return "".join(t for t, _ in _qb_line_segments(name, yds, td, inter, jersey, catt))


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
    """Parse '14/20' or '14-20' → (display '14/20', attempts int)."""
    if not catt:
        return "", 0
    sep = "/" if "/" in catt else ("-" if "-" in catt else None)
    if not sep:
        return str(catt), 0
    parts = catt.split(sep, 1)
    try:
        attempts = int(parts[1].replace(",", "").strip())
        return f"{parts[0].strip()}/{parts[1].strip()}", attempts
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
                segs = _qb_line_segments(last, yds, td, inter, jersey, completes)
                qbs.append({
                    "name": last,
                    "jersey": jersey,
                    "line": _format_qb_line(last, yds, td, inter, jersey, completes),
                    "segments": segs,
                    "attempts": attempts,
                    "scope": "game",
                })
    return qbs


def _stat_col_index(labels, keys, *names):
    """Index of a boxscore column by label or key (case-insensitive)."""
    lab = [str(x).upper() for x in (labels or [])]
    kee = [str(x).upper() for x in (keys or [])]
    for name in names:
        n = str(name).upper()
        if n in lab:
            return lab.index(n)
        if n in kee:
            return kee.index(n)
    return -1


def _stat_float(value):
    try:
        return float(str(value or "0").replace(",", "").replace("T", "").strip() or 0)
    except ValueError:
        return 0.0


def _stat_compact(value):
    """'181' or '1.5' with no thousands separators."""
    s = str(value or "").replace(",", "").strip()
    if not s:
        return ""
    try:
        n = float(s)
        if n == int(n):
            return str(int(n))
        return s
    except ValueError:
        return s


def _stat_at(stats, labels, keys, *names):
    idx = _stat_col_index(labels, keys, *names)
    if idx < 0 or idx >= len(stats or []):
        return ""
    return str((stats or [])[idx] or "").strip()


def _boxscore_block(players, team_id, abbr=""):
    tid = str(team_id or "")
    ab = (abbr or "").upper()
    for block in players or []:
        team = block.get("team") or {}
        if tid and str(team.get("id") or "") == tid:
            return block
    if ab:
        for block in players or []:
            team = block.get("team") or {}
            if (team.get("abbreviation") or "").upper() == ab:
                return block
    return None


def _boxscore_category(block, *names):
    want = {n.lower() for n in names}
    for cat in (block or {}).get("statistics") or []:
        if (cat.get("name") or "").lower() in want:
            return cat
        if (cat.get("displayName") or "").lower() in want:
            return cat
    return None


def _unwrap_boxscore_athlete(row_or_athlete):
    if isinstance(row_or_athlete, dict):
        inner = row_or_athlete.get("athlete")
        if isinstance(inner, dict):
            return inner
        return row_or_athlete
    return {}


def _athlete_short_dot_name(athlete):
    """ESPN shortName ('C. Ward') or first-initial + last name."""
    athlete = _unwrap_boxscore_athlete(athlete)
    if not isinstance(athlete, dict):
        return "Player"
    short = (athlete.get("shortName") or "").strip()
    if short:
        return short
    last = format_player_lastname(athlete)
    first = (athlete.get("firstName") or "").strip()
    if first:
        return f"{first[0].upper()}. {last}"
    disp = (athlete.get("displayName") or athlete.get("fullName") or "").strip()
    parts = disp.split()
    if len(parts) >= 2 and parts[0]:
        return f"{parts[0][0].upper()}. {last}"
    return last or "Player"


def _athlete_pos_abbr(athlete):
    athlete = _unwrap_boxscore_athlete(athlete)
    if not isinstance(athlete, dict):
        return ""
    pos = athlete.get("position")
    if isinstance(pos, dict):
        return (pos.get("abbreviation") or pos.get("displayName") or "").strip().upper()
    if isinstance(pos, str):
        return pos.strip().upper()
    return ""


def _postgame_player_clause(athlete, bits):
    name = _athlete_short_dot_name(athlete)
    pos = _athlete_pos_abbr(athlete)
    head = f"{name} {pos}".strip() if pos else name
    extras = [b for b in bits if b]
    if extras:
        return f"{head}, {', '.join(extras)}"
    return head


def _pick_boxscore_leader(cat, *score_names):
    """Athlete with the highest numeric value in the named column; else first listed."""
    if not cat:
        return None, [], [], [], -1, 0.0
    labels = cat.get("labels") or []
    keys = cat.get("keys") or cat.get("names") or []
    athletes = cat.get("athletes") or []
    idx = _stat_col_index(labels, keys, *score_names)
    if not athletes:
        return None, [], labels, keys, idx, 0.0
    best = athletes[0]
    best_n = -1.0
    if idx >= 0:
        for ath in athletes:
            stats = ath.get("stats") or []
            n = _stat_float(stats[idx] if idx < len(stats) else 0)
            if n > best_n:
                best_n = n
                best = ath
    else:
        best_n = 0.0
    return best, best.get("stats") or [], labels, keys, idx, best_n


def _postgame_td_bit(td):
    """'1TD' when the leader scored; omit 0 TD."""
    if _stat_float(td) <= 0:
        return ""
    compact = _stat_compact(td)
    return f"{compact}TD" if compact else ""


def _parse_postgame_leaders(boxscore_players, team_id, abbr, team_full):
    """Away/home crawl: PASS / RUSH / REC / SACKS / TACKLE leaders from boxscore."""
    nick = get_team_nickname(team_full) or ""
    tag = (NFL_CITY_ABBR.get(nick) or abbr or nick or "TEAM").upper()
    block = _boxscore_block(boxscore_players, team_id, abbr)
    sections = []
    if not block:
        return {"nick": nick, "tag": tag, "team_full": team_full or "", "sections": sections}

    passing = _boxscore_category(block, "passing")
    passer, stats, labels, keys, _idx, _n = _pick_boxscore_leader(
        passing, "YDS", "passingYards",
    )
    if passer:
        catt_raw = _stat_at(stats, labels, keys, "C/ATT", "CMP/ATT")
        catt, _att = _parse_catt(catt_raw) if catt_raw else ("", 0)
        if not catt:
            cmp_ = _stat_compact(_stat_at(stats, labels, keys, "CMP", "COMP", "C", "completions"))
            att = _stat_compact(_stat_at(stats, labels, keys, "ATT", "passingAttempts"))
            if cmp_ and att:
                catt = f"{cmp_}/{att}"
        yds = _stat_compact(_stat_at(stats, labels, keys, "YDS", "passingYards"))
        td = _stat_compact(_stat_at(stats, labels, keys, "TD", "passingTouchdowns"))
        bits = []
        if catt:
            bits.append(catt)
        if yds:
            bits.append(f"{yds}YDS")
        td_bit = _postgame_td_bit(td)
        if td_bit:
            bits.append(td_bit)
        sections.append((f"{tag} PASS", _postgame_player_clause(passer, bits)))

    rushing = _boxscore_category(block, "rushing")
    rusher, stats, labels, keys, _idx, _n = _pick_boxscore_leader(
        rushing, "YDS", "rushingYards",
    )
    if rusher:
        car = _stat_compact(_stat_at(stats, labels, keys, "CAR", "ATT", "rushingAttempts"))
        yds = _stat_compact(_stat_at(stats, labels, keys, "YDS", "rushingYards"))
        td = _stat_compact(_stat_at(stats, labels, keys, "TD", "rushingTouchdowns"))
        bits = []
        if car:
            bits.append(f"{car}CAR")
        if yds:
            bits.append(f"{yds}YDS")
        td_bit = _postgame_td_bit(td)
        if td_bit:
            bits.append(td_bit)
        sections.append(("RUSH", _postgame_player_clause(rusher, bits)))

    receiving = _boxscore_category(block, "receiving")
    recvr, stats, labels, keys, _idx, _n = _pick_boxscore_leader(
        receiving, "YDS", "receivingYards",
    )
    if recvr:
        rec = _stat_compact(_stat_at(stats, labels, keys, "REC", "receptions"))
        yds = _stat_compact(_stat_at(stats, labels, keys, "YDS", "receivingYards"))
        td = _stat_compact(_stat_at(stats, labels, keys, "TD", "receivingTouchdowns"))
        bits = []
        if rec:
            bits.append(f"{rec}REC")
        if yds:
            bits.append(f"{yds}YDS")
        td_bit = _postgame_td_bit(td)
        if td_bit:
            bits.append(td_bit)
        sections.append(("REC", _postgame_player_clause(recvr, bits)))

    defense = _boxscore_category(block, "defensive", "defense")
    sacker, stats, labels, keys, _idx, sacks_n = _pick_boxscore_leader(
        defense, "SACKS", "SACK", "sacks",
    )
    if sacker and sacks_n > 0:
        sck = _stat_compact(_stat_at(stats, labels, keys, "SACKS", "SACK", "sacks"))
        if sck:
            sections.append(("SACKS", _postgame_player_clause(sacker, [f"{sck}SCK"])))

    tackler, stats, labels, keys, _idx, tck_n = _pick_boxscore_leader(
        defense, "TOT", "TOTAL", "totalTackles", "SOLO",
    )
    if tackler and tck_n > 0:
        tck = _stat_compact(_stat_at(stats, labels, keys, "TOT", "TOTAL", "totalTackles", "SOLO"))
        if tck:
            sections.append(("TACKLE", _postgame_player_clause(tackler, [f"{tck}TCK"])))

    return {"nick": nick, "tag": tag, "team_full": team_full or "", "sections": sections}


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
        completes = f"{m.group(1)}/{m.group(2)}"
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
                segs = _qb_line_segments(last, yds, td, inter, jersey, completes)
                qbs.append({
                    "name": last,
                    "jersey": jersey,
                    "line": _format_qb_line(last, yds, td, inter, jersey, completes),
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
        "is_red_zone": False,
        "is_fourth_down": False,
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
    out["is_red_zone"] = _as_bool(situation.get("isRedZone"))
    out["is_fourth_down"] = _is_fourth_down(situation, out["down_distance"] or dd)

    # Raw yard spot ("NYG 45"); the card composes "2nd & 22 on NYG 45".
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
    """Down and spot for the live center, e.g. '2nd & 22 on NYG 45'.

    ESPN downDistanceText often already ends with 'at NYG 47'. Drop that
    clause and keep the 'on NYG 47' spot.
    """
    down = (down or "").strip()
    spot = (ball_on or "").strip()
    if spot.lower().startswith("ball on "):
        spot = spot[8:].strip()
    if down and spot:
        down = re.sub(r"\s+at\s+.+$", "", down, count=1, flags=re.IGNORECASE).strip()
        return f"{down} on {spot}" if down else f"on {spot}"
    if down:
        return down
    if spot:
        return f"on {spot}"
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


def _scoring_alert_team_label(play, team_label):
    """Nickname casing for scoring flash (Giants, not GIANTS)."""
    label = (team_label or "TEAM").strip()
    if label:
        label = label[0].upper() + label[1:] if len(label) > 1 else label.upper()
    return get_team_nickname(play.get("team_full") or "") or label


def format_scoring_alert_headline(play, team_label):
    """Short all-caps hold line, e.g. 'GIANTS TOUCHDOWN!'."""
    nick = _scoring_alert_team_label(play, team_label).upper()
    raw = play.get("text") or ""
    type_text = (play.get("type_text") or "").lower()
    scoring = (play.get("scoring_name") or "").lower()
    main = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip() or raw

    if "field goal" in type_text or scoring in ("field-goal", "fieldgoal"):
        return f"{nick} FIELD GOAL!"
    if "safety" in type_text or scoring == "safety":
        return f"{nick} SAFETY!"
    if "two-point" in type_text or "two point" in main.lower() or scoring in (
        "two-point-conversion", "2pt",
    ):
        return f"{nick} TWO-POINT!"
    if "extra point" in type_text or scoring in ("extra-point", "pat"):
        return f"{nick} EXTRA POINT!"
    if (
        scoring == "touchdown"
        or "touchdown" in type_text
        or "pass" in type_text
        or "rush" in type_text
        or re.search(r"\bpass from\b", main, re.I)
        or re.search(r"\bRush\b", main)
    ):
        return f"{nick} TOUCHDOWN!"
    return f"{nick} SCORE!"


def format_scoring_alert_message(play, team_label):
    """Detail flash text, e.g. 'Giants Score: Scattebo 22 YD Run Touchdown!'."""
    label = _scoring_alert_team_label(play, team_label)

    raw = play.get("text") or ""
    type_text = (play.get("type_text") or "").lower()
    scoring = (play.get("scoring_name") or "").lower()
    main = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip() or raw

    if "field goal" in type_text or scoring in ("field-goal", "fieldgoal"):
        m = re.match(r"^(.+?)\s+(\d+)\s*Yds?\s+Field Goal", main, re.I)
        if m:
            return (
                f"{label} Score: {_short_player_name(m.group(1))} "
                f"{m.group(2)} YD Field Goal!"
            )
        return f"{label} Score: FIELD GOAL {main}"

    if "pass" in type_text or re.search(r"\bpass from\b", main, re.I):
        m = re.match(r"^(.+?)\s+(\d+)\s*Yds?\s+pass\s+from\s+(.+)$", main, re.I)
        if m:
            return (
                f"{label} Score: {_short_player_name(m.group(1))} "
                f"{m.group(2)} YD Pass Touchdown!"
            )
        return f"{label} Score: PASS {main} Touchdown!"

    if "rush" in type_text or re.search(r"\bRush\b", main):
        m = re.match(r"^(.+?)\s+(\d+)\s*Yds?\s+Rush", main, re.I)
        if m:
            return (
                f"{label} Score: {_short_player_name(m.group(1))} "
                f"{m.group(2)} YD Run Touchdown!"
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
    drive_summary = _drive_summary_line(summary) if summary else ""

    # For finals: no live situation rows (compact quarter digits under scores)
    if state == "post":
        sit["down_distance"] = ""
        sit["last_play"] = ""
        sit["ball_on"] = ""
        sit["possession_id"] = ""
        sit["away_timeouts"] = None
        sit["home_timeouts"] = None
        sit["is_red_zone"] = False
        sit["is_fourth_down"] = False
        drive_summary = ""

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

    # Pregame-only broadcast / spread (hidden once the game is live or final)
    broadcast = _competition_broadcast(comp) if state == "pre" else ""
    spread = _competition_spread(comp) if state == "pre" else ""

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
                    segs = _qb_line_segments(last, yds, td, inter, jersey, completes)
                    entry = {
                        "name": last,
                        "jersey": jersey,
                        "line": _format_qb_line(last, yds, td, inter, jersey, completes),
                        "segments": segs,
                        "attempts": attempts or QB_MIN_ATTEMPTS,
                        "scope": "season",
                    }
                    if tid == a["id"] and not away_qbs:
                        away_qbs.append(entry)
                    elif tid == h["id"] and not home_qbs:
                        home_qbs.append(entry)

    scoring_plays = _extract_scoring_plays(summary) if summary else []

    away_post = {"nick": "", "team_full": "", "sections": []}
    home_post = {"nick": "", "team_full": "", "sections": []}
    if state == "post" and summary:
        players = (summary.get("boxscore") or {}).get("players") or []
        away_post = _parse_postgame_leaders(players, a["id"], a["abbr"], a["full"])
        home_post = _parse_postgame_leaders(players, h["id"], h["abbr"], h["full"])

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
        "away_post": away_post,
        "home_post": home_post,
        "down_distance": sit["down_distance"],
        "ball_on": sit["ball_on"],
        "last_play": sit["last_play"],
        "drive_summary": drive_summary,
        "possession_id": sit["possession_id"],
        "is_red_zone": bool(sit.get("is_red_zone")),
        "is_fourth_down": bool(sit.get("is_fourth_down")),
        "away_timeouts": sit["away_timeouts"],
        "home_timeouts": sit["home_timeouts"],
        "broadcast": broadcast,
        "spread": spread,
        "kickoff": kickoff_local,
        "start": start,
        "scoring_plays": scoring_plays,
    }


def _empty_post_leaders():
    return {"nick": "", "team_full": "", "sections": []}


def _test_game(
    *,
    game_id,
    state,
    away_name,
    home_name,
    away_id,
    home_id,
    away_abbr,
    home_abbr,
    away_score="0",
    home_score="0",
    away_lines=None,
    home_lines=None,
    away_record="",
    home_record="",
    status="Scheduled",
    status_detail="",
    down_distance="",
    ball_on="",
    last_play="",
    drive_summary="",
    possession_id="",
    is_red_zone=False,
    is_fourth_down=False,
    away_timeouts=None,
    home_timeouts=None,
    broadcast="",
    spread="",
    kickoff="",
    away_qbs=None,
    home_qbs=None,
    scoring_plays=None,
):
    """Normalized game dict matching `_parse_event` / `self.games` shape."""
    return {
        "game_id": str(game_id),
        "state": state,
        "status": status,
        "status_detail": status_detail,
        "away_name": away_name,
        "home_name": home_name,
        "away_id": str(away_id),
        "home_id": str(home_id),
        "away_abbr": away_abbr,
        "home_abbr": home_abbr,
        "away_score": str(away_score),
        "home_score": str(home_score),
        "away_lines": list(away_lines or []),
        "home_lines": list(home_lines or []),
        "away_record": away_record,
        "home_record": home_record,
        "away_qbs": list(away_qbs or []),
        "home_qbs": list(home_qbs or []),
        "away_post": _empty_post_leaders(),
        "home_post": _empty_post_leaders(),
        "down_distance": down_distance,
        "ball_on": ball_on,
        "last_play": last_play,
        "drive_summary": drive_summary,
        "possession_id": str(possession_id or ""),
        "is_red_zone": bool(is_red_zone),
        "is_fourth_down": bool(is_fourth_down),
        "away_timeouts": away_timeouts,
        "home_timeouts": home_timeouts,
        "broadcast": broadcast,
        "spread": spread,
        "kickoff": kickoff,
        "start": "",
        "scoring_plays": list(scoring_plays or []),
    }


def build_test_games():
    """Fake slate for `-test` / `--test` — no ESPN. Same shape as fetch output."""
    # 1) Live red zone, 1st down, clock > 2:00, possession + timeouts
    rz = _test_game(
        game_id="test-rz",
        state="in",
        status="In Progress",
        status_detail="3:42 - 3rd",
        away_name="Kansas City Chiefs",
        home_name="Buffalo Bills",
        away_id="test-kc",
        home_id="test-buf",
        away_abbr="KC",
        home_abbr="BUF",
        away_score="17",
        home_score="20",
        away_lines=["7", "3", "7"],
        home_lines=["7", "7", "6"],
        down_distance="1st & Goal",
        ball_on="BUF 8",
        last_play="Mahomes pass complete to Kelce for 12 yards",
        possession_id="test-kc",
        is_red_zone=True,
        is_fourth_down=False,
        away_timeouts=2,
        home_timeouts=3,
    )
    # 2) Live 4th down under 2:00 (gold clock + gold down line)
    fd = _test_game(
        game_id="test-4th",
        state="in",
        status="In Progress",
        status_detail="1:45 - 4th",
        away_name="Philadelphia Eagles",
        home_name="Dallas Cowboys",
        away_id="test-phi",
        home_id="test-dal",
        away_abbr="PHI",
        home_abbr="DAL",
        away_score="24",
        home_score="21",
        away_lines=["7", "10", "0", "7"],
        home_lines=["0", "7", "7", "7"],
        down_distance="4th & 2",
        ball_on="DAL 38",
        last_play="Hurts rush for 3 yards to the DAL 38",
        possession_id="test-phi",
        is_red_zone=False,
        is_fourth_down=True,
        away_timeouts=1,
        home_timeouts=2,
    )
    # 3) Live midfield — last play + drive summary for both settings
    mid = _test_game(
        game_id="test-mid",
        state="in",
        status="In Progress",
        status_detail="8:15 - 2nd",
        away_name="Green Bay Packers",
        home_name="Detroit Lions",
        away_id="test-gb",
        home_id="test-det",
        away_abbr="GB",
        home_abbr="DET",
        away_score="14",
        home_score="10",
        away_lines=["7", "7"],
        home_lines=["3", "7"],
        down_distance="2nd & 7",
        ball_on="GB 45",
        last_play="Love pass incomplete intended for Doubs",
        drive_summary="12 plays, 75 yards",
        possession_id="test-gb",
        is_red_zone=False,
        is_fourth_down=False,
        away_timeouts=3,
        home_timeouts=3,
    )
    # 4) Final Q1–Q4 linescores (center table, no total column)
    fin = _test_game(
        game_id="test-final",
        state="post",
        status="Final",
        status_detail="Final",
        away_name="San Francisco 49ers",
        home_name="Seattle Seahawks",
        away_id="test-sf",
        home_id="test-sea",
        away_abbr="SF",
        home_abbr="SEA",
        away_score="28",
        home_score="24",
        away_lines=["7", "10", "3", "8"],
        home_lines=["0", "7", "10", "7"],
    )
    # 5) Final with OT period in linescores
    ot = _test_game(
        game_id="test-ot",
        state="post",
        status="Final/OT",
        status_detail="Final/OT",
        away_name="Baltimore Ravens",
        home_name="Pittsburgh Steelers",
        away_id="test-bal",
        home_id="test-pit",
        away_abbr="BAL",
        home_abbr="PIT",
        away_score="27",
        home_score="24",
        away_lines=["3", "7", "7", "7", "3"],
        home_lines=["7", "3", "7", "7", "0"],
    )
    # 6) Pregame — kickoff, records, network, spread
    pre = _test_game(
        game_id="test-pre",
        state="pre",
        status="Scheduled",
        status_detail="Sun, 4:25 PM",
        away_name="Miami Dolphins",
        home_name="New York Jets",
        away_id="test-mia",
        home_id="test-nyj",
        away_abbr="MIA",
        home_abbr="NYJ",
        away_record="4-1",
        home_record="3-2",
        kickoff="4:25 PM",
        broadcast="CBS",
        spread="NYJ -3.5",
    )
    return [rz, fd, mid, fin, ot, pre]


_TEST_PLAY_LINES = (
    "Pass complete for 8 yards to the stick",
    "Rush up the middle for 3 yards",
    "Incomplete pass broken up in coverage",
    "Screen pass for 12 yards and a first down",
    "Sack — loss of 6 yards",
    "Field goal is good from 41 yards",
)

_TEST_DOWN_CYCLE = (
    ("1st & 10", False),
    ("2nd & 7", False),
    ("3rd & 5", False),
    ("4th & 2", True),
)

_TEST_RZ_DOWN_CYCLE = (
    ("1st & Goal", False),
    ("2nd & Goal", False),
    ("3rd & 3", False),
    ("4th & 1", True),
)

_TEST_SPOTS = ("GB 45", "DET 48", "GB 38", "50", "DET 42", "GB 33")


def _advance_test_clock(status_detail, seconds=7):
    """Tick a live clock string like '3:42 - 3rd' down; wrap within the quarter."""
    text = (status_detail or "").strip()
    match = re.match(
        r"^(\d{1,2}):(\d{2})(\s*-\s*)(.+)$",
        text,
    )
    if not match:
        return text
    total = int(match.group(1)) * 60 + int(match.group(2))
    total = max(0, total - seconds)
    if total <= 0:
        # Keep a visible under-2:00 or mid-quarter clock so gold / blue still demo
        quarter = (match.group(4) or "").strip().lower()
        if quarter in ("2nd", "4th", "ot"):
            total = 95  # 1:35 — under two minutes
        else:
            total = 185  # 3:05 — over two minutes
    return f"{total // 60}:{total % 60:02d}{match.group(3)}{match.group(4)}"


def advance_test_games(games, tick):
    """Mutate live fake games in place for one -test timer tick. Returns games."""
    if not games:
        return games
    live = [g for g in games if g.get("state") == "in"]
    if not live:
        return games

    down_i = tick % len(_TEST_DOWN_CYCLE)
    rz_i = tick % len(_TEST_RZ_DOWN_CYCLE)
    play = _TEST_PLAY_LINES[tick % len(_TEST_PLAY_LINES)]
    spot = _TEST_SPOTS[tick % len(_TEST_SPOTS)]

    for g in live:
        g["status_detail"] = _advance_test_clock(g.get("status_detail") or "")
        gid = g.get("game_id")

        if gid == "test-rz":
            dd, is4 = _TEST_RZ_DOWN_CYCLE[rz_i]
            g["down_distance"] = dd
            g["is_fourth_down"] = is4
            # Toggle red zone every other down step so both colors show
            g["is_red_zone"] = (rz_i % 2 == 0) or not is4
            if is4:
                g["is_red_zone"] = False  # 4th-down gold wins; clear RZ for clarity
            yards = 9 - (rz_i % 4)
            g["ball_on"] = f"BUF {max(1, yards)}"
            g["last_play"] = play
            if tick % 5 == 0:
                # Swap possession occasionally
                aid, hid = g.get("away_id"), g.get("home_id")
                g["possession_id"] = hid if g.get("possession_id") == aid else aid

        elif gid == "test-4th":
            # Stay on 4th under 2:00 most ticks; briefly show other downs
            if tick % 6 == 0:
                dd, is4 = _TEST_DOWN_CYCLE[down_i]
                g["down_distance"] = dd
                g["is_fourth_down"] = is4
            else:
                g["down_distance"] = "4th & 2"
                g["is_fourth_down"] = True
            g["is_red_zone"] = False
            # Keep clock under 2:00 in 4th for gold clock demo
            if not _clock_under_two_minutes(g.get("status_detail") or ""):
                g["status_detail"] = "1:45 - 4th"
            g["ball_on"] = f"DAL {38 - (tick % 5)}"
            g["last_play"] = play
            if tick % 7 == 0:
                aid, hid = g.get("away_id"), g.get("home_id")
                g["possession_id"] = hid if g.get("possession_id") == aid else aid

        elif gid == "test-mid":
            dd, is4 = _TEST_DOWN_CYCLE[down_i]
            g["down_distance"] = dd
            g["is_fourth_down"] = is4
            g["is_red_zone"] = False
            g["ball_on"] = spot
            g["last_play"] = play
            plays_n = 8 + (tick % 8)
            yards_n = 35 + (tick * 5) % 50
            g["drive_summary"] = f"{plays_n} plays, {yards_n} yards"
            if tick % 6 == 0:
                aid, hid = g.get("away_id"), g.get("home_id")
                g["possession_id"] = hid if g.get("possession_id") == aid else aid
            # Occasional score bump + new scoring_play so the existing alert path can fire
            if tick > 0 and tick % 10 == 0:
                try:
                    home = int(g.get("home_score") or 0)
                except (TypeError, ValueError):
                    home = 0
                home += 7
                g["home_score"] = str(home)
                plays = list(g.get("scoring_plays") or [])
                pid = f"test-score-{tick}"
                plays.append({
                    "id": pid,
                    "text": "Amon-Ra St. Brown 15 Yd pass from Jared Goff",
                    "type_text": "Passing Touchdown",
                    "scoring_name": "touchdown",
                    "team_id": str(g.get("home_id") or ""),
                    "team_full": g.get("home_name") or "",
                    "team_abbr": g.get("home_abbr") or "",
                })
                g["scoring_plays"] = plays

    return games


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
    """Soft blur via downscale then upscale (no Qt GraphicsBlur needed)."""
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
_LOGO_GLOW_CACHE = {}
_ICON_GLOW_CACHE = {}


def _glow_cache_get(cache, key):
    with _IMAGE_CACHE_LOCK:
        cached = cache.get(key)
        if cached is None:
            return None
        # Insertion order is recency. A hit must not drop the entry.
        del cache[key]
        cache[key] = cached
        return cached


def _glow_cache_put(cache, key, value, limit):
    with _IMAGE_CACHE_LOCK:
        if key in cache:
            del cache[key]
        cache[key] = value
        # Drop the oldest entry only. Clearing the whole cache forced every
        # later card (and the next QB rotation) to rebuild every halo.
        while len(cache) > limit:
            cache.pop(next(iter(cache)))


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
    cached = _glow_cache_get(_GLOW_LAYER_CACHE, key)
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
    _glow_cache_put(_GLOW_LAYER_CACHE, key, layers, 1600)
    # One card used to blur every glyph without returning to the scroll
    # thread. Yield so a cold halo cannot hold the clock for the whole card.
    time.sleep(0)
    return layers


def _draw_text_glow(painter, x, y, text, fill_color, glow_color):
    """Soft diffuse halo *behind* text, then crisp fill on top (no bold edge)."""
    if not text or not str(text).strip():
        painter.setPen(QtGui.QColor(fill_color))
        painter.drawText(x, y, text)
        return
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


def _logo_glow_layers(pixmap):
    """Faint white bloom for this logo. Logos repeat on every card rebuild."""
    key = (int(pixmap.cacheKey()), pixmap.width(), pixmap.height())
    cached = _glow_cache_get(_LOGO_GLOW_CACHE, key)
    if cached is not None:
        return cached
    sil = _make_white_silhouette(pixmap)
    if sil is None:
        return None
    w, h = pixmap.width(), pixmap.height()
    pad = _GLOW_BLOOM_PAD
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
    wider = _approx_blur_pixmap(bloom, strength=2)
    layers = (bloom, wider)
    _glow_cache_put(_LOGO_GLOW_CACHE, key, layers, 80)
    time.sleep(0)
    return layers


def _draw_logo_white_glow(painter, x, y, pixmap):
    """Faint soft white bloom *behind* logo (scaled+blurred silhouette)."""
    if pixmap is None or pixmap.isNull():
        return
    layers = _logo_glow_layers(pixmap)
    if not layers:
        painter.drawImage(x, y, pixmap)
        return
    bloom, wider = layers
    w, h = pixmap.width(), pixmap.height()
    painter.setOpacity(0.18)
    painter.drawImage(
        x + (w - bloom.width()) // 2,
        y + (h - bloom.height()) // 2,
        bloom,
    )
    painter.setOpacity(0.09)
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


def _icon_glow_layers(pixmap):
    key = (int(pixmap.cacheKey()), pixmap.width(), pixmap.height())
    cached = _glow_cache_get(_ICON_GLOW_CACHE, key)
    if cached is not None:
        return cached
    sil = _make_white_silhouette(pixmap)
    if sil is None:
        return None
    w, h = pixmap.width(), pixmap.height()
    bw = max(1, int(w * 1.28))
    bh = max(1, int(h * 1.28))
    big = sil.scaled(
        bw, bh, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation,
    )
    bloom = _approx_blur_pixmap(big, strength=3)
    _glow_cache_put(_ICON_GLOW_CACHE, key, bloom, 32)
    time.sleep(0)
    return bloom


def _draw_icon_glow(painter, x, y, pixmap):
    """Soft white bloom behind small icons (possession football)."""
    if pixmap is None or pixmap.isNull():
        return
    bloom = _icon_glow_layers(pixmap)
    if bloom is None:
        painter.drawImage(x, y, pixmap)
        return
    w, h = pixmap.width(), pixmap.height()
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
    show_lp = settings.get("show_last_play", True)
    show_drive = settings.get("show_drive_summary", False)
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
        game.get("drive_summary"),
        game.get("status_detail"),
        game.get("kickoff"),
        game.get("broadcast"),
        game.get("spread"),
        game.get("away_record"),
        game.get("home_record"),
        game.get("possession_id"),
        bool(game.get("is_red_zone")),
        bool(game.get("is_fourth_down")),
        game.get("away_timeouts"),
        game.get("home_timeouts"),
        bool(show_lp),
        bool(show_drive),
        _pick_qb_line(game.get("away_qbs") or [], qb_rotate_index) if show_qb else "",
        _pick_qb_line(game.get("home_qbs") or [], qb_rotate_index) if show_qb else "",
    )


def _score_is_wide(value):
    """True when a score needs more than the reserved two-digit column."""
    return len(str(value or "").strip()) >= 3


def _game_base_key(game, qb_rotate_index, settings):
    """Pixels that do not change when the clock, down, play, score, or passer line ticks.

    Live updates and passer rotation stamp this image instead of repainting
    logos and names. qb_rotate_index is unused; the passer line is overlay.
    """
    state = game.get("state")
    heading = _break_heading(game) if state in ("in", "post") else ""
    return (
        game.get("game_id"),
        state,
        game.get("away_id"),
        game.get("home_id"),
        game.get("away_name"),
        game.get("home_name"),
        game.get("away_abbr"),
        game.get("home_abbr"),
        tuple(game.get("away_lines") or ()),
        tuple(game.get("home_lines") or ()),
        heading,
        game.get("kickoff"),
        game.get("broadcast"),
        game.get("spread"),
        game.get("away_record"),
        game.get("home_record"),
        _score_is_wide(game.get("away_score")),
        _score_is_wide(game.get("home_score")),
        bool(settings.get("show_possession", True)),
    )


def _postgame_visual_key(game, side):
    stats = game.get(f"{side}_post") or {}
    team = game.get("away_name") if side == "away" else game.get("home_name")
    return (
        "post",
        game.get("game_id"),
        side,
        tuple(tuple(s) for s in (stats.get("sections") or ())),
        team,
    )


def _strip_card_jobs(games, qb_rotate_index, settings):
    """Scroll order: each game card, then away/home post-game stats for finals."""
    jobs = []
    want_post = bool(settings.get("include_postgame_stats", False))
    for g in games or []:
        jobs.append((_game_visual_key(g, qb_rotate_index, settings), "game", g, None))
        if not want_post or g.get("state") != "post":
            continue
        for side in ("away", "home"):
            stats = g.get(f"{side}_post") or {}
            if not (stats.get("sections") or []):
                continue
            jobs.append((_postgame_visual_key(g, side), "post", g, side))
    return jobs


def build_postgame_stats_card(host, game, side):
    """One-line crawl: TEAM PASS / RUSH / REC / SACKS / TACKLE; headers in team color."""
    settings = host.settings
    h = host.ticker_height
    dpr = host.dpr
    stats = game.get(f"{side}_post") or {}
    sections = stats.get("sections") or []
    team_full = (
        stats.get("team_full")
        or (game.get("away_name") if side == "away" else game.get("home_name"))
        or ""
    )
    team_color = QtGui.QColor(get_team_color(team_full, settings))
    glow_names = bool(settings.get("glow_team_names", False))
    glow_all = bool(settings.get("glow_all", False))

    font = QtGui.QFont(
        getattr(host, "postgame_font", None)
        or getattr(host, "situation_font", None)
        or getattr(host, "small_font_bold", host.small_font)
    )
    fm = QtGui.QFontMetrics(font)
    body_color = QtGui.QColor("#FFFFFF")
    gap = "  "
    runs = []
    for i, section in enumerate(sections):
        if not section or len(section) < 2:
            continue
        header, body = section[0], section[1]
        if i:
            runs.append((gap, body_color, glow_all))
        if header:
            runs.append((f"{header}:", team_color, glow_names or glow_all))
        if body:
            runs.append((f" {body}", body_color, glow_all))

    line_w = sum(fm.horizontalAdvance(text) for text, _c, _g in runs if text)
    pad = 10
    total_w = max(48, pad + line_w + pad)
    image = QtGui.QImage(
        max(1, int(total_w * dpr)),
        max(1, int(h * dpr)),
        QtGui.QImage.Format_ARGB32_Premultiplied,
    )
    image.setDevicePixelRatio(dpr)
    image.fill(0)
    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
    painter.setFont(font)
    cap = fm.capHeight()
    if cap <= 0:
        cap = -fm.tightBoundingRect("ABCDEFGHIJKLMNOPQRSTUVWXYZ").top()
    if cap <= 0:
        cap = fm.ascent()
    y = int(round(h / 2.0 + cap / 2.0))
    x = float(pad)
    for text, color, do_glow in runs:
        if not text:
            continue
        ix = int(round(x))
        if do_glow:
            _draw_text_glow(painter, ix, y, text, color, color)
        else:
            painter.setPen(color)
            painter.drawText(ix, y, text)
        x += fm.horizontalAdvance(text)
    painter.end()
    time.sleep(0)
    return image


def build_game_card(host, game, qb_rotate_index=0, layer="full", target=None):
    """Render one game to a QImage (logical coords, DPR-scaled).

    layer "full" paints everything. "base" skips the live overlay (scores,
    timeouts, possession, clock / down / play) so that image can be cached.
    "overlay" paints only that overlay onto target (a copy of the base).
    """
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
    show_drive = settings.get("show_drive_summary", False)
    show_ball = settings.get("show_ball_on", True)
    show_poss = settings.get("show_possession", True)
    glow_names = bool(settings.get("glow_team_names", False))
    glow_all = bool(settings.get("glow_all", False))

    away_qb_list = game.get("away_qbs") or []
    home_qb_list = game.get("home_qbs") or []
    away_qb_segs = _pick_qb_segments(away_qb_list, qb_rotate_index) if show_qb else []
    home_qb_segs = _pick_qb_segments(home_qb_list, qb_rotate_index) if show_qb else []
    away_qb = "".join(t for t, _ in away_qb_segs)
    home_qb = "".join(t for t, _ in home_qb_segs)

    away_name_w = metrics.horizontalAdvance(away_label)
    home_name_w = metrics.horizontalAdvance(home_label)
    bold_small = getattr(host, "small_font_bold", host.small_font)  # Gotham Black full size
    regular_small = getattr(host, "small_font_regular", host.small_font)  # same face, size-1
    qb_faux = bool(getattr(host, "qb_faux_bold", False))
    away_qb_w = _qb_segments_width(away_qb_segs, bold_small, regular_small, qb_faux) if away_qb_segs else 0
    home_qb_w = _qb_segments_width(home_qb_segs, bold_small, regular_small, qb_faux) if home_qb_segs else 0
    # Reserve the widest passer line so a rotation does not change card width
    # or force a new base image.
    away_qb_slot = away_qb_w
    home_qb_slot = home_qb_w
    if show_qb:
        for _qi in range(max(len(away_qb_list), len(home_qb_list))):
            if _qi < len(away_qb_list):
                away_qb_slot = max(
                    away_qb_slot,
                    _qb_segments_width(
                        _pick_qb_segments(away_qb_list, _qi),
                        bold_small, regular_small, qb_faux,
                    ),
                )
            if _qi < len(home_qb_list):
                home_qb_slot = max(
                    home_qb_slot,
                    _qb_segments_width(
                        _pick_qb_segments(home_qb_list, _qi),
                        bold_small, regular_small, qb_faux,
                    ),
                )

    state = game.get("state")
    # Pregame records sit outside the names (away left of name, home right).
    away_rec = (game.get("away_record") or "").strip() if state == "pre" else ""
    home_rec = (game.get("home_record") or "").strip() if state == "pre" else ""
    away_rec_gap = tiny_m.horizontalAdvance(" ") if away_rec else 0
    home_rec_gap = tiny_m.horizontalAdvance(" ") if home_rec else 0
    away_rec_w = tiny_m.horizontalAdvance(away_rec) if away_rec else 0
    home_rec_w = tiny_m.horizontalAdvance(home_rec) if home_rec else 0
    away_name_total = away_name_w + away_rec_gap + away_rec_w
    home_name_total = home_name_w + home_rec_gap + home_rec_w
    away_block = max(away_name_total, away_qb_slot)
    home_block = max(home_name_total, home_qb_slot)
    sym = max(away_block, home_block)
    away_block = home_block = sym

    status_detail = (game.get("status_detail") or "").strip()
    # Center linescore for in-game quarter breaks and finals (periods only).
    break_heading = _break_heading(game) if state in ("in", "post") else ""
    linescore = None
    if break_heading:
        linescore = _prepare_linescore(
            getattr(host, "linescore_font", None)
            or getattr(host, "main_font", host.small_font),
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
    # Reserve at least two score digits so 7→17 does not shift center / logos.
    # Wider only when a score is already 3+ digits (rare) so it is not clipped.
    _score_slot_min = score_m.horizontalAdvance("00") if show_scores else 0
    away_score_col = max(away_score_w, _score_slot_min) if away_score else 0
    home_score_col = max(home_score_w, _score_slot_min) if home_score else 0

    down = (game.get("down_distance") or "").strip()
    ball_on = (game.get("ball_on") or "").strip() if show_ball else ""
    last_play_raw = (game.get("last_play") or "").strip()
    drive_raw = (game.get("drive_summary") or "").strip()
    if show_drive:
        bottom_row = drive_raw or last_play_raw
    elif show_lp:
        bottom_row = last_play_raw
    else:
        bottom_row = ""

    # Pregame / final keep the short center. Live games stack clock, down, play.
    vs_m = QtGui.QFontMetrics(host.vs_font)
    sit_font = sit_m = play_font = play_m = None
    pre_sub_font = pre_sub_m = None
    center_gap = 1
    center_items = []
    center_kind = "main"
    center_main = ""
    center_subs = []
    live_max_play_lines = 2
    if state == "pre":
        center_main = game.get("kickoff") or status_detail or "vs"
        center_kind = "time"
        # Network / spread: clearly larger than small_font, under kickoff size.
        pre_sub_font = QtGui.QFont(host.time_font)
        _time_px = host.time_font.pixelSize()
        if _time_px < 1:
            _time_px = max(6, int(h * 0.16)) + 1
        pre_sub_font.setPixelSize(max(8, _time_px - 2))
        pre_sub_m = QtGui.QFontMetrics(pre_sub_font)
        # Ideal row gap; shrink later if the stack will not fit the bar.
        center_gap = 5
        net = (game.get("broadcast") or "").strip()
        spr = (game.get("spread") or "").strip()
        if net:
            center_subs.append(net)
        if spr:
            center_subs.append(spr)
        if not center_subs:
            center_subs.append("vs")
    elif linescore:
        center_main_w = linescore["width"]
    elif state == "post":
        # Final with no ESPN linescores: narrow gap between scores only.
        center_main_w = max(12, int(h * 0.18))
        center_items = []
    else:
        # LED Ozone face has no readable lowercase ( "1st" draws as "1SC" ).
        clock_text = status_detail.upper()
        situation_line = _live_situation_line(down, ball_on).upper()
        play_text = bottom_row.upper()
        # Down line: same Ozone LED face as team names / scores / quarter table.
        sit_font = QtGui.QFont(host.situation_font)
        play_font = QtGui.QFont(host.play_font)
        sit_px = sit_font.pixelSize()
        play_px = play_font.pixelSize()
        if sit_px < 1:
            sit_px = max(10, int(h * 0.20))
        if play_px < 1:
            play_px = max(8, min(14, int(h * 0.14)))
        sit_px0, play_px0 = sit_px, play_px
        # Stable live center: clock + fixed max-down samples only.
        # Play wraps inside this column; it must not widen the card.
        sit_font.setPixelSize(sit_px0)
        sit_m = QtGui.QFontMetrics(sit_font)
        _clock_budget = time_m.horizontalAdvance("12:00 - 2ND")
        _down_budget = sit_m.horizontalAdvance("1ST & GOAL ON WSH 49")
        center_main_w = max(int(_clock_budget), int(_down_budget), 48)
        max_play_lines = 3
        lp_lines = []
        while True:
            sit_font.setPixelSize(sit_px)
            play_font.setPixelSize(play_px)
            sit_m = QtGui.QFontMetrics(sit_font)
            play_m = QtGui.QFontMetrics(play_font)
            lp_lines = (
                _fit_lines(play_text, play_m, center_main_w, max_play_lines)
                if play_text else []
            )
            plan = _live_center_rows(
                h, time_m, sit_m, play_m, clock_text, situation_line, lp_lines,
                max_play_lines=max_play_lines,
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
                if max_play_lines > 2:
                    # Shrink play glyphs before giving up a wrap line.
                    max_play_lines = 2
                    sit_px, play_px = sit_px0, play_px0
                    continue
                break
        live_max_play_lines = max_play_lines
        center_gap = 1
        if clock_text:
            center_items.append(("time", clock_text))
        if situation_line:
            center_items.append(("sit", situation_line))
        for ln in lp_lines:
            center_items.append(("play", ln))

    if state == "pre":
        _center_fm = time_m if center_kind == "time" else vs_m
        center_main_w = max(
            _center_fm.horizontalAdvance(center_main) if center_main else 0,
            40,
        )
        for sub in center_subs:
            center_main_w = max(center_main_w, pre_sub_m.horizontalAdvance(sub))
        center_main_w = max(center_main_w, 48)
        if center_main:
            center_items.append((center_kind, center_main))
        for sub in center_subs:
            center_items.append(("sub", sub))
        # Fit kickoff / network / spread in the bar with clear gaps + edge room.
        _pre_heights = []
        if center_main:
            _pre_heights.append(_glyph_line(time_m, center_main)[0])
        for sub in center_subs:
            _pre_heights.append(_glyph_line(pre_sub_m, sub)[0])
        _n_pre = len(_pre_heights)
        if _n_pre > 1:
            _edge = 2
            _content = sum(_pre_heights)
            _avail = h - 2 * _edge
            _need = _content + center_gap * (_n_pre - 1)
            if _need > _avail:
                center_gap = max(2, (_avail - _content) // (_n_pre - 1))
    elif state != "in" and state != "post" and not linescore:
        _center_fm = time_m if center_kind == "time" else vs_m
        center_main_w = max(
            _center_fm.horizontalAdvance(center_main) if center_main else 0,
            40,
        )
        center_main_w = max(center_main_w, 48)
        if center_main:
            center_items.append((center_kind, center_main))

    pad = 8
    # Same ink-to-ink gap: name ↔ logo ↔ score (logos have uneven canvas pad).
    gap_name_logo = 10
    gap_logo_score = 10
    gap_score_center = 8
    poss_pad = 4
    poss_icon = None
    away_has_ball = False
    home_has_ball = False
    # Always reserve both icon slots on a live card so a possession flip
    # does not change the width or force a full rebuild.
    reserve_poss = bool(show_poss and state == "in")
    if reserve_poss:
        poss_icon = get_football_icon(max(12, int(h * 0.26)))
        poss_id = str(game.get("possession_id") or "")
        away_has_ball = bool(poss_id) and poss_id == str(game.get("away_id"))
        home_has_ball = bool(poss_id) and poss_id == str(game.get("home_id"))
    poss_icon_w = poss_icon.width() if poss_icon is not None else 0
    poss_slot = (poss_pad + poss_icon_w) if reserve_poss else 0
    away_inside = poss_slot
    home_inside = poss_slot
    # Pregame: no score digits — logo sits closer to kickoff/vs center
    gap_logo_center = gap_logo_score if not show_scores else gap_logo_score

    if show_scores:
        total_w = (
            pad + away_block + gap_name_logo + away_vis_w + gap_logo_score
            + away_score_col + away_inside + gap_score_center + center_main_w
            + gap_score_center + home_inside + home_score_col + gap_logo_score
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
    if target is not None:
        image = target
    else:
        image = QtGui.QImage(phys_w, phys_h, QtGui.QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(0)
    painter = QtGui.QPainter(image)
    # Logical coords via devicePixelRatio (no painter.scale — avoids double-DPR)
    painter.setRenderHint(QtGui.QPainter.TextAntialiasing, True)

    def _clear_slot(rx, rw):
        """Drop previous overlay pixels in a reserved column (full bar height)."""
        if layer != "overlay" or rw <= 0:
            return
        painter.save()
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_Clear)
        painter.fillRect(QtCore.QRectF(rx, 0, rw, h), QtCore.Qt.transparent)
        painter.restore()

    paint_static = layer != "overlay"
    paint_live = layer != "base"

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
    # Away name (right-justified in block); pregame record OUTSIDE left of name.
    painter.setFont(host.main_font)
    _nx = x + away_block - away_name_total
    if paint_static and away_rec:
        painter.setFont(host.tiny_font)
        if glow_all:
            _draw_text_glow(painter, _nx, name_y, away_rec, "#BDBDBD", "#FFFFFF")
        else:
            painter.setPen(QtGui.QColor("#BDBDBD"))
            painter.drawText(_nx, name_y, away_rec)
        _nx = _nx + away_rec_w + away_rec_gap
        painter.setFont(host.main_font)
    if paint_static and (glow_names or glow_all):
        # Team-color glow when name glow on; faint white under glow_all alone
        _gc = away_color if glow_names else QtGui.QColor(255, 255, 255)
        _draw_text_glow(painter, _nx, name_y, away_label, away_color, _gc)
    elif paint_static:
        painter.setPen(away_color)
        painter.drawText(_nx, name_y, away_label)
    if layer != "base" and qb_y is not None and away_qb_segs:
        _qx = x + away_block - away_qb_w
        _draw_mixed_text(
            painter, _qx, qb_y, away_qb_segs, bold_small, regular_small,
            "#BDBDBD", glow=glow_all, glow_color="#FFFFFF", faux_bold=qb_faux,
        )
    x += away_block + gap_name_logo - _text_right_slack(metrics, away_label, away_name_w)
    away_logo_x = x - away_lpad
    if paint_static and glow_all:
        _draw_logo_white_glow(painter, away_logo_x, away_logo_y, away_logo)
    elif paint_static:
        painter.drawImage(away_logo_x, away_logo_y, away_logo)
    x += away_vis_w + (gap_logo_score if show_scores else gap_logo_center)

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
        if paint_live:
            _clear_slot(x, away_score_col)
            painter.setFont(score_font)
            score_draw_x = x + max(0, (away_score_col - away_score_w) // 2)
            if glow_all:
                _draw_text_glow(painter, score_draw_x, score_y, away_score, "#FFFFFF", "#FFFFFF")
            else:
                painter.setPen(QtGui.QColor("#FFFFFF"))
                painter.drawText(score_draw_x, score_y, away_score)
            # Timeouts centered on the reserved two-digit score column.
            _paint_timeouts(away_score_x, away_score_col, game.get("away_timeouts"), away_color)
        x += away_score_col
        if reserve_poss:
            x += poss_pad
            _clear_slot(x, poss_icon_w)
            if paint_live and away_has_ball:
                _paint_poss(x)
            x += poss_icon_w
        x += gap_score_center
    else:
        away_score_x = x  # center starts immediately after logo gap

    # Center column — live: clock / down on spot / play, all between the scores.
    center_left = x
    center_w = center_main_w
    # Live clock/down/play is the overlay. Linescore and pregame stay on the base.
    live_center = state == "in" and not linescore
    if live_center:
        _clear_slot(center_left, center_w)
    draw_center = paint_live if live_center else paint_static

    if draw_center and linescore:
        _draw_linescore(
            painter, center_left, center_w, h, linescore, away_color, home_color,
        )
    elif draw_center and center_items:
        live_clock = state == "in" and any(k == "time" for k, _ in center_items)
        live_rows = None
        if live_clock:
            clock_text = next((t for k, t in center_items if k == "time"), "")
            sit_text = next((t for k, t in center_items if k == "sit"), "")
            play_lines = [t for k, t in center_items if k == "play"]
            live_rows = _live_center_rows(
                h, time_m, sit_m, play_m, clock_text, sit_text, play_lines,
                max_play_lines=live_max_play_lines,
            )
        else:
            item_metrics = []
            for kind, text in center_items:
                if kind == "main":
                    fm = vs_m
                elif kind == "sub":
                    fm = pre_sub_m if pre_sub_m is not None else small_m
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
                if pre_sub_font is not None:
                    painter.setFont(pre_sub_font)
                    fm = pre_sub_m
                else:
                    painter.setFont(host.small_font)
                    fm = small_m
                fill = "#A0A0A0"
            elif kind == "sit":
                painter.setFont(sit_font)
                fm = sit_m
                fill = _sit_word_color(game, clock_blue, settings)
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
                    _play_slots = ("play", "play2", "play3")
                    slot = _play_slots[min(play_i, len(_play_slots) - 1)]
                    play_i += 1
                    top, vis_ascent = live_rows[slot]
                ty = top + vis_ascent
            else:
                vis_h, vis_ascent = item_metrics[i]
                ty = cy + vis_ascent
                cy += vis_h + center_gap
            tw = fm.horizontalAdvance(text)
            tx = center_left + (center_w - tw) // 2
            # Pixel Font7 smears under TextAntialiasing; disable for play
            # lines only, then restore whatever the card painter had set.
            _play_aa_prev = None
            if kind == "play":
                _play_aa_prev = painter.testRenderHint(
                    QtGui.QPainter.TextAntialiasing
                )
                painter.setRenderHint(QtGui.QPainter.TextAntialiasing, False)
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
            if _play_aa_prev is not None:
                painter.setRenderHint(
                    QtGui.QPainter.TextAntialiasing, _play_aa_prev
                )

    x += center_w
    if show_scores:
        x += gap_score_center
        if reserve_poss:
            _clear_slot(x, poss_icon_w)
            if paint_live and home_has_ball:
                _paint_poss(x)
            x += poss_icon_w + poss_pad
    else:
        x += gap_logo_center

    # Home score (live / final only)
    home_score_x = x
    if show_scores:
        if paint_live:
            _clear_slot(x, home_score_col)
            painter.setFont(score_font)
            score_draw_x = x + max(0, (home_score_col - home_score_w) // 2)
            if glow_all:
                _draw_text_glow(painter, score_draw_x, score_y, home_score, "#FFFFFF", "#FFFFFF")
            else:
                painter.setPen(QtGui.QColor("#FFFFFF"))
                painter.drawText(score_draw_x, score_y, home_score)
            _paint_timeouts(home_score_x, home_score_col, game.get("home_timeouts"), home_color)

        x += home_score_col + gap_logo_score
    else:
        x += 0  # already advanced logo-center gap above

    home_logo_x = x - home_lpad
    if paint_static and glow_all:
        _draw_logo_white_glow(painter, home_logo_x, home_logo_y, home_logo)
    elif paint_static:
        painter.drawImage(home_logo_x, home_logo_y, home_logo)
    x += home_vis_w + gap_name_logo - _text_left_slack(metrics, home_label)

    painter.setFont(host.main_font)
    if paint_static and (glow_names or glow_all):
        _gc = home_color if glow_names else QtGui.QColor(255, 255, 255)
        _draw_text_glow(painter, x, name_y, home_label, home_color, _gc)
    elif paint_static:
        painter.setPen(home_color)
        painter.drawText(x, name_y, home_label)
    if paint_static and home_rec:
        painter.setFont(host.tiny_font)
        rx = x + home_name_w + home_rec_gap
        if glow_all:
            _draw_text_glow(painter, rx, name_y, home_rec, "#BDBDBD", "#FFFFFF")
        else:
            painter.setPen(QtGui.QColor("#BDBDBD"))
            painter.drawText(rx, name_y, home_rec)
    if layer != "base" and qb_y is not None and home_qb_segs:
        _draw_mixed_text(
            painter, x, qb_y, home_qb_segs, bold_small, regular_small,
            "#BDBDBD", glow=glow_all, glow_color="#FFFFFF", faux_bold=qb_faux,
        )

    painter.end()
    return image


def stamp_live_card(base, host, game, qb_rotate_index=0):
    """Copy the cached base and paint only the live overlay onto it."""
    img = base.copy()
    return build_game_card(
        host, game, qb_rotate_index, layer="overlay", target=img,
    )


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------
class SettingsDialog(QtWidgets.QDialog):
    """Settings dialog with General, Team Colors, and Network tabs."""

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
        self.tabs.addTab(self._create_network_tab(), "Network")
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

        self.font_combo = QtWidgets.QComboBox()
        _fill_font_combo(self.font_combo, _ticker_font_request(settings))
        self.font_combo.setToolTip("Team names, scores, and clock. Bundled fonts/ faces.")
        layout.addRow("Ticker font:", self.font_combo)

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
        self.show_lp.setChecked(
            settings.get("show_last_play", True)
            and not settings.get("show_drive_summary", False)
        )
        self.show_lp.setToolTip("Bottom center row: the most recent play text")
        layout.addRow(self.show_lp)

        self.show_drive = QtWidgets.QCheckBox("Show drive summary")
        self.show_drive.setChecked(settings.get("show_drive_summary", False))
        self.show_drive.setToolTip("Bottom center row: current drive plays and yards")
        layout.addRow(self.show_drive)

        def _bottom_row_exclusive(checked, other):
            if checked and other.isChecked():
                other.blockSignals(True)
                other.setChecked(False)
                other.blockSignals(False)

        self.show_lp.toggled.connect(
            lambda checked: _bottom_row_exclusive(checked, self.show_drive)
        )
        self.show_drive.toggled.connect(
            lambda checked: _bottom_row_exclusive(checked, self.show_lp)
        )

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

        self.postgame = QtWidgets.QCheckBox("Include Post-Game Stats")
        self.postgame.setChecked(settings.get("include_postgame_stats", False))
        self.postgame.setToolTip(
            "After each final score, scroll away then home PASS/RUSH/REC/SACKS/TACKLE leaders"
        )
        layout.addRow(self.postgame)

        self.postgame_font_combo = QtWidgets.QComboBox()
        _fill_font_combo(self.postgame_font_combo, _postgame_font_request(settings))
        self.postgame_font_combo.setToolTip(
            "Typeface for the post-game PASS/RUSH/REC line. Size stays 2× the down line."
        )
        self.postgame_font_combo.setEnabled(self.postgame.isChecked())
        self.postgame.toggled.connect(self.postgame_font_combo.setEnabled)
        layout.addRow("Post-game stats font:", self.postgame_font_combo)

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

    def _create_network_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.setAlignment(QtCore.Qt.AlignTop)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(14)

        intro = QtWidgets.QLabel(
            "Proxy and certificate settings for ESPN fetches. Applied when you "
            "click OK. Enable a proxy for corporate networks; optionally pick a "
            ".pem/.crt if SSL inspection uses a private CA."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        proxy_group = QtWidgets.QGroupBox("Proxy")
        proxy_form = QtWidgets.QFormLayout(proxy_group)
        proxy_form.setContentsMargins(10, 12, 10, 10)
        proxy_form.setSpacing(8)

        self.use_proxy_check = QtWidgets.QCheckBox("Enable Proxy")
        self.use_proxy_check.setChecked(bool(self.settings.get("use_proxy", False)))
        proxy_form.addRow(self.use_proxy_check)

        self.proxy_url_edit = QtWidgets.QLineEdit(
            normalize_proxy_url(self.settings.get("proxy", ""))
        )
        self.proxy_url_edit.setPlaceholderText("http://proxy.example.com:8080")
        self.proxy_url_edit.setEnabled(self.use_proxy_check.isChecked())
        self.use_proxy_check.toggled.connect(self.proxy_url_edit.setEnabled)
        proxy_form.addRow("Proxy URL:", self.proxy_url_edit)
        layout.addWidget(proxy_group)

        cert_group = QtWidgets.QGroupBox("SSL Certificate (Optional)")
        cert_form = QtWidgets.QFormLayout(cert_group)
        cert_form.setContentsMargins(10, 12, 10, 10)
        cert_form.setSpacing(8)

        self.use_cert_check = QtWidgets.QCheckBox("Use Certificate File")
        self.use_cert_check.setChecked(bool(self.settings.get("use_cert", False)))
        cert_form.addRow(self.use_cert_check)

        cert_row = QtWidgets.QHBoxLayout()
        self.cert_file_edit = QtWidgets.QLineEdit(self.settings.get("cert_file", "") or "")
        self.cert_file_edit.setPlaceholderText("Path to .pem / .crt certificate file")
        self.cert_file_edit.setEnabled(self.use_cert_check.isChecked())
        self.use_cert_check.toggled.connect(self.cert_file_edit.setEnabled)
        browse_btn = QtWidgets.QPushButton("Browse…")
        browse_btn.setEnabled(self.use_cert_check.isChecked())
        self.use_cert_check.toggled.connect(browse_btn.setEnabled)
        browse_btn.clicked.connect(self.browse_cert_file)
        cert_row.addWidget(self.cert_file_edit)
        cert_row.addWidget(browse_btn)
        cert_form.addRow("Certificate File:", cert_row)
        layout.addWidget(cert_group)
        layout.addStretch()
        return widget

    def browse_cert_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select Certificate File",
            "",
            "Certificate Files (*.pem *.crt *.cer *.ca-bundle);;All Files (*)",
        )
        if path:
            self.cert_file_edit.setText(path)

    def apply(self):
        s = self.settings
        s["speed"] = self.speed.value()
        s["update_interval"] = self.update_iv.value()
        s["ticker_height"] = self.height.value()
        s["font"] = self.font_combo.currentText().strip() or "Ozone"
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
        s["show_drive_summary"] = self.show_drive.isChecked()
        s["show_qb_stats"] = self.show_qb.isChecked()
        s["show_ball_on"] = self.show_ball.isChecked()
        s["show_possession"] = self.show_poss.isChecked()
        s["glow_team_names"] = self.glow_names.isChecked()
        s["glow_all"] = self.glow_all.isChecked()
        s["include_final_games"] = self.finals.isChecked()
        s["include_postgame_stats"] = self.postgame.isChecked()
        s["postgame_font"] = (
            self.postgame_font_combo.currentText().strip() or "Gotham Black"
        )
        s["include_scheduled_games"] = self.scheduled.isChecked()
        s["live_games_only"] = self.live_only.isChecked()
        s["use_proxy"] = self.use_proxy_check.isChecked()
        s["proxy"] = self.proxy_url_edit.text().strip()
        s["use_cert"] = self.use_cert_check.isChecked()
        s["cert_file"] = self.cert_file_edit.text().strip()

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
        bool(s.get("show_drive_summary", False)),
        bool(s.get("show_ball_on", True)),
        bool(s.get("show_possession", True)),
        bool(s.get("glow_team_names", False)),
        bool(s.get("glow_all", False)),
        bool(s.get("use_city_abbreviations", False)),
        bool(s.get("show_city_only", False)),
        bool(s.get("show_team_cities", False)),
        bool(s.get("include_postgame_stats", False)),
        s.get("postgame_font", "Gotham Black"),
        round(float(dpr), 3),
    )


def _slate_fp(settings, games, qb_index, height, dpr):
    layout = _layout_tuple(settings, height, dpr)
    if not games:
        return ("empty", layout)
    jobs = _strip_card_jobs(games, qb_index, settings)
    return (
        tuple(job[0] for job in jobs),
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
    """Paint every card and compose the strip (GUI thread; loading / empty only)."""
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
        for _key, kind, game, side in _strip_card_jobs(
            games, host.qb_rotate_index, host.settings
        ):
            try:
                if kind == "post":
                    cards.append(build_postgame_stats_card(host, game, side))
                else:
                    cards.append(build_game_card(host, game, host.qb_rotate_index))
            except Exception as e:
                print(f"[CARD] {game.get('game_id')}: {e}")
    return _compose_strip_image(cards, host.settings, host.ticker_height, host.dpr)


class _SlateThread(QtCore.QThread):
    """Network fetch only. Never paint here — QPainter / scaled / blur stay on GUI."""

    result_ready = QtCore.pyqtSignal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        payload = self._fn()
        if payload is not None:
            self.result_ready.emit(payload)


class _FontHost:
    """Font/settings snapshot for one sliced GUI-thread card rebuild."""

    _FONT_ATTRS = (
        "main_font", "score_font", "small_font", "small_font_bold",
        "small_font_regular", "tiny_font", "vs_font", "time_font",
        "situation_font", "play_font", "linescore_font", "postgame_font",
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
        self._build_jobs = []
        self._build_order = []
        self._build_cards = []  # prior strip images by order index (partial swap)
        self._build_cache = {}
        self._build_host = None
        self._scroll_speed_px_per_ms = 0.0
        self._scroll_step_px = 0.0  # fixed px per timer tick (stable dx)
        self._last_frame_ms = 0.0
        self._slate_fp = None  # last composed visual fingerprint
        self._card_cache = {}
        self._card_base_cache = {}
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
        self._parking_appbar_window = False
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
        if not _cli_test:
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

        if _cli_test:
            self._test_tick = 0
            self.games = build_test_games()
            self._detect_scoring_alerts(self.games)
            self._publish_strip()
            print(f"[NFL-TCKR] {len(self.games)} test game(s) loaded")
            self._test_timer = QtCore.QTimer(self)
            self._test_timer.timeout.connect(self._advance_test_slate)
            self._test_timer.start(TEST_ADVANCE_MS)
        else:
            self.refresh_games()

    def _init_fonts(self):
        scale = self.settings.get("font_scale_percent", 160) / 100.0
        # MLB-TCKR formula for pitcher/batter / player-info text
        pscale = self.settings.get("player_font_scale_percent", 75) / 100.0
        ticker_request = _ticker_font_request(self.settings)
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

        # Down uses Ozone (ticker LED face, same as team names / scores /
        # quarter table); last play uses fonts/PixelFont7-G02A.ttf (Pixel
        # Font7, ~7px em). Integer size stays in the 8–14 band so glyphs
        # stay crisp on a ~72px bar; unscaled by font_scale_percent so
        # clock + down + up to three play lines fit. Down text is uppercased when
        # drawn (Ozone has no readable lowercase).
        situation_px = max(10, int(h * 0.20))
        # ~10px at h=72; clamp to 8–14 so the pixel face is not smeared.
        play_px = max(8, min(14, int(h * 0.14)))
        if play_px >= situation_px:
            play_px = max(8, situation_px - 2)
        self.situation_font = QtGui.QFont(family)
        self.situation_font.setPixelSize(situation_px)
        play_family, play_source = _load_bundled_font_file("PixelFont7-G02A.ttf")
        if not play_family:
            play_family = resolve_font_family("Pixel Font7", player_family)
            play_source = font_resolve_source("Pixel Font7", player_family)
        self.play_font = QtGui.QFont(play_family)
        self.play_font.setPixelSize(play_px)

        # Quarter / linescore grid: same LED face as team names and scores
        # (Ozone). Heading (FINAL / HALFTIME) stays on time_font.
        linescore_px = max(8, int(h * 0.125))
        self.linescore_font = QtGui.QFont(family)
        self.linescore_font.setPixelSize(linescore_px)

        post_request = _postgame_font_request(self.settings)
        post_family = resolve_font_family(post_request, player_family)
        post_source = font_resolve_source(post_request, player_family)
        self.postgame_font = QtGui.QFont(post_family)
        self.postgame_font.setPixelSize(max(2, situation_px * 2))

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
            f"font last play request 'PixelFont7-G02A.ttf' -> "
            f"{play_family} ({play_source})"
        )
        _dbg(
            f"font post-game stats request '{post_request}' -> "
            f"{post_family} ({post_source})"
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
            ("quarter table", self.linescore_font),
            ("post-game stats", self.postgame_font),
            ("center sub line", self.small_font),
            ("version watermark", self.tiny_font),
        ):
            _dbg(f"font {label}: {_font_debug_desc(font)}")
        _dbg(
            f"font quarter table: {_font_debug_desc(self.linescore_font)} "
            f"(sized down to fit the bar); heading uses time_font"
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
            apply_proxy_settings()
            self._fullscreen_override_exes = {
                x.lower()
                for x in self.settings.get("fullscreen_override_exes", [])
            }
            new_height = int(self.settings.get("ticker_height", 72))
            self.ticker_height = new_height
            geo = self._screen.geometry()
            reserved = getattr(self, "_appbar_reserved_phys", None)
            if reserved and self.settings.get("docked", True):
                dpr = float(self.dpr) or 1.0
                y = int(round(reserved[1] / dpr))
                self.setGeometry(geo.x(), y, geo.width(), self.ticker_height)
            else:
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
        if _cli_test:
            # Republish the fake slate; never hit ESPN in -test mode.
            self._publish_strip()
            return
        self._request_slate(True)

    def _advance_test_slate(self):
        """Tick fake live games, then rebuild cards via the normal strip path."""
        if not _cli_test or not self.games:
            return
        self._test_tick = getattr(self, "_test_tick", 0) + 1
        advance_test_games(self.games, self._test_tick)
        self._detect_scoring_alerts(self.games)
        self._publish_strip()

    def _request_slate(self, fetch):
        """Fetch off the UI thread. Card pixels are built on the UI thread."""
        if _cli_test:
            fetch = False
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
        """Queue changed cards on the GUI thread — one paint per event-loop turn."""
        layout = _layout_tuple(self.settings, self.ticker_height, self.dpr)
        if layout != self._card_cache_layout:
            self._card_cache = {}
            self._card_base_cache = {}
            self._card_cache_layout = layout
        live_ids = {g.get("game_id") for g in (self.games or [])}
        if self._card_base_cache:
            self._card_base_cache = {
                k: v for k, v in self._card_base_cache.items() if k[0] in live_ids
            }
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
            self._install_scroll_cards(cards, {}, fp, complete=True)
            return
        order = []
        misses = []
        cache = {}
        for key, kind, game, side in _strip_card_jobs(
            games, self.qb_rotate_index, self.settings
        ):
            order.append(key)
            img = self._card_cache.get(key)
            if img is None:
                misses.append((key, kind, game, side))
            else:
                cache[key] = img
        self._build_order = order
        self._build_cache = cache
        if not misses:
            self._install_scroll_cards(
                [cache[k] for k in order if k in cache], cache, fp, complete=True,
            )
            return
        if not self._scroll_entries and not self._intro_hold:
            self._show_loading_card()
        # Keep prior images by strip index so unfinished slots stay on screen.
        self._build_cards = [img for img, _w in (self._scroll_entries or [])[1:]]
        self._build_jobs = misses
        self._build_i = 0
        self._build_host = _FontHost(self)
        # Return to the event loop before the first QPainter so a pending
        # HighEventPriority VBlank wake can run first.
        QtCore.QTimer.singleShot(0, lambda g=gen: self._paint_one_build_card(g))

    def _paint_one_build_card(self, gen):
        """Paint exactly one missed card, install it, then yield to the event loop."""
        if gen != self._build_gen:
            return
        jobs = self._build_jobs
        i = self._build_i
        if i >= len(jobs):
            self._finish_card_build(gen)
            return
        key, kind, game, side = jobs[i]
        host = self._build_host or self
        try:
            if kind == "post":
                img = build_postgame_stats_card(host, game, side)
            elif (
                game.get("state") in ("in", "post")
                and not bool(self.settings.get("glow_all"))
            ):
                # Logos and names (including team-name glow) stay on a cached
                # base. Live ticks stamp the clock, down, play, score,
                # timeouts, and possession icon. Finals stamp the passer line
                # when it rotates. Glow-all is skipped: that halo is also
                # drawn on the scores and the center text, and it spills
                # outside the slots this stamp clears.
                bkey = _game_base_key(
                    game, host.qb_rotate_index, self.settings,
                )
                base = self._card_base_cache.get(bkey)
                if base is None:
                    base = build_game_card(
                        host, game, host.qb_rotate_index, layer="base",
                    )
                    if base is not None and not base.isNull():
                        self._card_base_cache[bkey] = base
                if base is not None and not base.isNull():
                    img = stamp_live_card(
                        base, host, game, host.qb_rotate_index,
                    )
                else:
                    img = build_game_card(host, game, host.qb_rotate_index)
            else:
                img = build_game_card(host, game, host.qb_rotate_index)
            if img is not None and not img.isNull():
                self._build_cache[key] = img
        except Exception as e:
            print(f"[CARD] {game.get('game_id')}: {e}")
        self._build_i = i + 1
        done = self._build_i >= len(jobs)
        self._install_scroll_cards(
            self._cards_for_build_install(),
            dict(self._build_cache),
            self._build_fp,
            complete=done,
        )
        if not done:
            # Normal-priority timer: VBlank HighEventPriority wakes run first.
            QtCore.QTimer.singleShot(0, lambda g=gen: self._paint_one_build_card(g))
        else:
            self._finish_card_build(gen)

    def _cards_for_build_install(self):
        """New/cache hits plus prior images for slots not painted yet this build."""
        order = self._build_order
        cache = self._build_cache
        prev = self._build_cards or []
        cards = []
        for idx, key in enumerate(order):
            img = cache.get(key)
            if img is None and idx < len(prev):
                img = prev[idx]
            if img is not None and not img.isNull():
                cards.append(img)
        return cards

    def _finish_card_build(self, gen):
        if gen != self._build_gen:
            return
        self._build_jobs = []
        self._build_host = None
        self._build_cards = []
        # _install_scroll_cards(complete=True) already cleared _build_fp / set _slate_fp
        if self._build_fp is not None:
            self._install_scroll_cards(
                self._cards_for_build_install(),
                dict(self._build_cache),
                self._build_fp,
                complete=True,
            )

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

    def _install_scroll_cards(self, cards, cache, fp, card_widths=None, complete=True):
        """Pointer-swap finished card images. No paint / scale / blur / compose."""
        h = int(self.ticker_height)
        space_pct = max(0, min(200, int(self.settings.get("game_spacing_percent", 100))))
        gap = max(29, int(round(h * 1.80 * (space_pct / 100.0))))
        dpr = float(self.dpr)
        entries = [self._loop_marker_entry()]
        for i, img in enumerate(cards):
            if img is None or img.isNull():
                continue
            if card_widths is not None and i < len(card_widths):
                entries.append((img, int(card_widths[i])))
            else:
                idpr = float(img.devicePixelRatio()) or dpr
                entries.append((img, max(1, int(round(img.width() / idpr)))))
        if not entries:
            if complete:
                self._build_fp = None
            return
        period = float(sum(w for _img, w in entries) + len(entries) * gap)
        old_period = self._strip_w
        self._card_cache = cache
        self._scroll_entries = entries
        self._card_gap = gap
        self._strip_w = period
        # Keep scroll_offset continuous — never reset on a content rebuild.
        if not self._intro_hold:
            if not self._scroll_primed:
                self._prime_scroll_from_right()
            elif period > 0 and self.scroll_offset >= 0:
                if old_period > 0 and abs(old_period - period) > 0.5:
                    self.scroll_offset = self.scroll_offset % period
                elif self.scroll_offset >= period:
                    self.scroll_offset = self.scroll_offset % period
        if complete:
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
                headline = format_scoring_alert_headline(p, nick)
                detail = format_scoring_alert_message(p, nick)
                color = get_team_color(team_full, self.settings)
                self._alert_queue.append({
                    "headline": headline,
                    "detail": detail,
                    "text": detail,
                    "team_color": color,
                    "game_id": gid,
                    "play_id": pid,
                })
                _dbg(f"SCORE FLASH queued: {headline} → {detail} ({key})")
        if self._current_alert is None and self._alert_queue:
            self._start_next_alert()

    def _start_next_alert(self):
        if not self._alert_queue:
            return
        self._current_alert = self._alert_queue.pop(0)
        self._alert_phase = "in"
        self._alert_phase_start = self._elapsed.nsecsElapsed() / 1_000_000.0
        self._alert_timer.start(self._scroll_timer_interval_ms)
        _dbg(
            f"SCORE FLASH showing: "
            f"{self._current_alert.get('headline')} → "
            f"{self._current_alert.get('detail')}"
        )
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

        # Hold: headline for SCORE_ALERT_HEADLINE_MS, then detail. Slide-in
        # shows headline; slide-out keeps the detail that was already up.
        if phase == "out" or (
            phase == "hold" and phase_elapsed_ms >= SCORE_ALERT_HEADLINE_MS
        ):
            text = alert.get("detail") or alert.get("text") or ""
        else:
            text = (
                alert.get("headline")
                or alert.get("detail")
                or alert.get("text")
                or ""
            )
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
        """Schedule a sliced GUI-thread card rebuild (one card per event-loop turn)."""
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
        if getattr(self, "_appbar_passive_dock", False):
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
        """Promote from passive dock only when the strip above us is gone."""
        if sys.platform != "win32":
            return
        if not self.settings.get("docked", True):
            return
        if not getattr(self, "_appbar_passive_dock", False):
            return
        user32 = ctypes.windll.user32
        hwnd = int(self.winId())
        if not hwnd:
            return

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
        if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(mi)):
            return
        foreign = int(getattr(self, "_appbar_stack_top_phys", 0) or 0)
        if foreign <= 0:
            self._promote_passive_dock_to_appbar()
            return
        if self._other_sister_visible_on_monitor():
            self._sister_gone_streak = 0
            self._park_window_at_reserved()
            return
        if self._window_occupies_top_inset(hmonitor, mi.rcMonitor.top, foreign):
            self._sister_gone_streak = 0
            self._park_window_at_reserved()
            return
        streak = getattr(self, "_sister_gone_streak", 0) + 1
        self._sister_gone_streak = streak
        if streak >= 2:
            self._promote_passive_dock_to_appbar()

    def _window_occupies_top_inset(self, hmonitor, phys_y, inset_h):
        """True if some other visible window still sits in the reserved top strip."""
        if inset_h <= 0:
            return False
        user32 = ctypes.windll.user32
        our = int(self.winId())
        found = False
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _enum_cb(hwnd, _lp):
            nonlocal found
            if found or hwnd == our or not user32.IsWindowVisible(hwnd):
                return True
            if user32.MonitorFromWindow(hwnd, 0x00000002) != hmonitor:
                return True
            rc = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rc)):
                return True
            if rc.bottom <= phys_y or rc.top >= phys_y + inset_h:
                return True
            found = True
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
        return found

    def _park_window_at_reserved(self):
        if sys.platform != "win32":
            return
        reserved = getattr(self, "_appbar_reserved_phys", None)
        hwnd = int(self.winId()) if self.winId() else 0
        if not reserved or not hwnd:
            return
        user32 = ctypes.windll.user32
        wr = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wr))
        want_x, want_y = int(reserved[0]), int(reserved[1])
        want_w = int(reserved[2] - reserved[0])
        want_h = int(reserved[3] - reserved[1])
        if wr.left == want_x and wr.top == want_y and wr.right - wr.left == want_w:
            return
        self._parking_appbar_window = True
        try:
            user32.SetWindowPos(
                hwnd, 0, want_x, want_y, want_w, want_h, 0x0004 | 0x0010
            )
        finally:
            self._parking_appbar_window = False

    def moveEvent(self, event):
        super().moveEvent(event)
        if getattr(self, "_parking_appbar_window", False):
            return
        if not self.settings.get("docked", True):
            return
        if getattr(self, "_appbar_reserved_phys", None):
            self._park_window_at_reserved()

    def _work_area_notify_allowed(self):
        return getattr(self, "_appbar_stack_top_phys", 0) == 0

    def _prepare_hwnd_for_appbar(self, hwnd):
        """Drop WS_EX_TOOLWINDOW so the shell includes this bar in the work area.

        Qt.Tool sets TOOLWINDOW (no taskbar button). Explorer often ignores those
        HWNDs when computing rcWork, so other windows open underneath the ticker.
        """
        if sys.platform != "win32" or not hwnd:
            return
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        new_style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
        if new_style != style:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
            # Style changes on a live HWND need a frame nudge.
            user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                0x0001 | 0x0002 | 0x0004 | 0x0020,  # NOMOVE NOSIZE NOZORDER FRAMECHANGED
            )
            print("[AppBar] Cleared WS_EX_TOOLWINDOW so Explorer reserves this strip")

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
        # Do not call SPI_SETWORKAREA with a NULL rect. Explorer rebuilds rcWork
        # from AppBars it recognizes and can drop this ticker (Qt Tool HWND).
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
            print("[AppBar] GetMonitorInfoW failed — cannot force work area")
            return False
        need = int(strip_bottom_phys)
        if mi.rcWork.top >= need:
            print(
                f"[AppBar] Work area already clear of strip "
                f"(rcWork.top={mi.rcWork.top} >= {need})"
            )
            return False
        rect = wintypes.RECT()
        rect.left = mi.rcWork.left
        rect.top = need
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
        else:
            print(
                f"[AppBar] SPI_SETWORKAREA FAILED err={ctypes.GetLastError()} "
                f"wanted top={rect.top} (was {mi.rcWork.top})"
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
            reserved = getattr(self, "_appbar_reserved_phys", None)
            hmonitor = getattr(self, "_appbar_hmonitor", None)
            if reserved and hmonitor:
                self._force_work_area_below_strip(hmonitor, reserved[3])
            if getattr(self, "_appbar_registered", False):
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

    def _apply_passive_top_dock(
        self, hwnd, phys_x, phys_y, phys_width, phys_height, prior_top, hmonitor,
    ):
        """Sit below an existing top reservation without stealing that AppBar slot.

        Still shrinks the work area so other windows start below NFL-TCKR.
        """
        user32 = ctypes.windll.user32
        top_phys = int(phys_y + max(0, prior_top))
        self._appbar_passive_dock = True
        self._appbar_registered = False
        self._appbar_stack_top_phys = int(max(0, prior_top))
        self._appbar_reserved_phys = (
            int(phys_x),
            top_phys,
            int(phys_x + phys_width),
            top_phys + int(phys_height),
        )
        self._appbar_hmonitor = hmonitor
        self._appbar_on_primary = self._is_primary_monitor_handle(hmonitor)
        self._parking_appbar_window = True
        try:
            user32.SetWindowPos(
                hwnd,
                0,
                int(phys_x),
                top_phys,
                int(phys_width),
                int(phys_height),
                0x0004 | 0x0010,  # SWP_NOZORDER | SWP_NOACTIVATE
            )
        finally:
            self._parking_appbar_window = False
        self._force_work_area_below_strip(hmonitor, top_phys + phys_height)
        self._notify_same_monitor_work_area_change()
        wr = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wr))
        print(
            f"[AppBar] Passive dock below {prior_top}px reservation — "
            f"window phys=({wr.left},{wr.top},{wr.right},{wr.bottom}), "
            f"work-area top target={top_phys + phys_height}"
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
                f"QUERYPOS will stack NFL-TCKR below it and still register"
            )

        self._appbar_passive_dock = False
        self._prepare_hwnd_for_appbar(hwnd)

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
        user32.GetMonitorInfoW(hmonitor, ctypes.byref(mi))
        print(
            f"[AppBar] After SETPOS rcWork.top={mi.rcWork.top} "
            f"strip=({abd.rc.left},{abd.rc.top},{abd.rc.right},{abd.rc.bottom})"
        )

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
        self._parking_appbar_window = True
        try:
            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                int(abd.rc.left),
                int(abd.rc.top),
                int(abd.rc.right - abd.rc.left),
                int(abd.rc.bottom - abd.rc.top),
                SWP_NOACTIVATE,
            )
        finally:
            self._parking_appbar_window = False
        wr = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wr))
        if wr.top != int(abd.rc.top):
            print(
                f"[AppBar] HWND snapped to y={wr.top}, re-parking at y={abd.rc.top}"
            )
            self._park_window_at_reserved()
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
            f"monitor phys=({phys_x},{phys_y},{phys_x + phys_width},{mi.rcMonitor.bottom}), "
            f"reserved phys=({abd.rc.left},{abd.rc.top},{abd.rc.right},{abd.rc.bottom}), "
            f"window phys=({wr.left},{wr.top},{wr.right},{wr.bottom}), "
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
    apply_proxy_settings()
    # High-DPI
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("NFL-TCKR")
    print(f"[NFL-TCKR] v{VERSION}", flush=True)
    register_all_font_files()
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
    if _cli_test:
        print("[NFL-TCKR] -test: fake live slate (no ESPN)", flush=True)
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
