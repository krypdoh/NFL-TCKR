# NFL-TCKR

Professional NFL live-score ticker for Windows — sibling to MLB-TCKR.

Displays a scrolling top-of-screen bar with team logos, colored names, scores,
down & distance, last play, QB stats, ball-on field position, and a possession
football icon.

**Version:** 0.1.24  
**Data:** ESPN public site API ([Public-ESPN-API](https://github.com/pseudo-r/Public-ESPN-API))

---

## How to run

From the repo (or this folder), with the same Python env as MLB-TCKR:

```powershell
cd C:\Users\prc\Dropbox\github\MLB-TCKR\NFL-TCKR
python NFL-TCKR.py
```

Dependencies (already in the parent `requirements.txt`): `PyQt5`, `requests`.

### Desktop docking (Windows AppBar)

With **Docked** enabled (default), the ticker registers as a top-edge AppBar so the
desktop work area shrinks and other windows do not draw under it. If MLB-TCKR or
another sister ticker already owns the top strip, NFL docks passively below it.
Fullscreen games/video hide the ticker (opacity); it returns when fullscreen ends.
Uncheck Docked in Settings for a floating always-on-top bar with no reservation.

### Scroll smoothness debug

Scroll judder profiling is **OFF by default**. Enable with `--debug`:

```powershell
python NFL-TCKR.py --debug
```

Or set `$env:NFL_TCKR_SCROLL_DEBUG="1"` (`"0"` forces off even with `--debug`).

Console lines tagged `[SCROLL]`:
- **Summary (~1s):** `tick dt` / `paint dt` / `jitter` / `spikes` / `dx` / `offset` / `clock=VBlank|QTimer`
- **SPIKE:** tick or paint gap >1.5× expected
- **STRIP rebuild / skip:** pixmap recompose vs fingerprint skip

## Controls

| Action | How |
|--------|-----|
| Settings | Right-click tray / bar → Settings…, or press **S** |
| Refresh | Tray → Refresh, or press **R** |
| Pause | Hover the bar, or press **Space** |
| Quit | Tray → Quit, or press **Q** |

Settings are stored in `%APPDATA%\NFL-TCKR\NFL-TCKR.Settings.json`.

- **General:** speed, refresh, height, docked AppBar, background/content transparency,
  glow options, name display, filters
- **Team Colors:** per-team Primary / Secondary / Tertiary / Custom (names + scoring flash)

## Layout (live games)

```
[AWAY NAME] [logo] [score]  2nd & 4   [score] [logo] [HOME NAME]
 QB: …stats…              Ball on NYG 10              QB: …stats…
                          last play text…
                              🏈 under possessor
```

- **Team name display:** nickname / city+team / city / abbreviation (Settings)
- **QB rotation:** if a team has 2+ QBs with ≥4 pass attempts, stats toggle every 5s
- **Possession icon:** `images/football-icon.jpg`

## Assets

| Path | Purpose |
|------|---------|
| `NFL-TCKR.py` | Entry point |
| `logos/*.png` | Team logos |
| `images/football-icon.jpg` | Possession indicator |

Fonts reuse the parent repo’s `fonts/Ozone-xRRO.ttf` when available.
