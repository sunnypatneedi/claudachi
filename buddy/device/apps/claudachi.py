"""Claudachi — a kid-friendly virtual pet for the Cardputer-Adv.

A cozy, Tamagotchi-style companion that lives on the device. Persists
across power cycles via ESP32 NVS, runs entirely offline, and never
asks the kid for personal information.

### Design choices that matter

- **No death, ever.** Real Tamagotchi guilt-trips kids by killing
  pets that go unfed. Claudachi never dies — at low stats it just
  naps a bit harder and looks peaceful. A kid who puts the device
  down for a week should pick it up and find a sleepy friend, not
  a corpse. (See: `gamification-audit` skill — dark patterns are
  out of scope here.)
- **No streaks, no daily quests.** All UX is moment-to-moment.
  Care actions feel good in the moment; that's the entire loop.
- **Closed dialogue corpus.** Every line the pet ever speaks is in
  ``_TALK_LINES`` below — no LLM call, no network, no surprises.
  A future v2 could route prompts through the existing buddy_ble
  pipe to Claude Desktop; v1 stays offline. Lines are statements,
  not questions, so the pet never appears to ask a kid for
  personal info.
- **Gentle session-break nudge.** After ~15 minutes of continuous
  play the pet suggests a break. We only suggest — never lock.
- **All on-device.** No telemetry, no cloud, no anything that
  would make a parent worry. State lives in ESP32 NVS namespace
  ``claudachi``, separate from the buddy namespace.

### Layout (240x135)

  Header:    y=0..19    DARK band, "Claudachi" + name + level,
                        ORANGE hairline @ y=20
  Stats col: x=4..86,   y=24..112  4 stacked horizontal bars
  Pet area:  x=92..236, y=22..115  body sprite + speech text
  Hint:      y=117..134 DARK band, key legend

### Inputs

  F  feed     P  play     Z  sleep (toggle)    T  talk
  Enter is a synonym for talk. Q / ESC exits to launcher.

### Sprite encoding

The pet body is one shared 16x14-cell shape; per-state face overlays
swap eyes / mouth on top. Each cell renders as a `_CELL_PX` square.
That keeps the sprite data terse (one tuple of strings) and the
runtime cost low (~250 fillRect calls per redraw, only on bob /
mode change, not every poll).

### Conventions inherited from the app suite

- 240x135 three-zone chrome (header/content/hint)
- DejaVu9 size 1, ORANGE/CREAM palette, drawString + textWidth for
  centering — see hello_cardputer.py for the rationale.
- 40 ms poll loop with MatrixKeyboard.tick() each pass.
- Q / ESC quits via `machine.reset()` in the `finally` block —
  UIFlow 2.0 has no return-to-launcher API; soft-reboot is the
  established workaround.
"""

import sys

# Match claude_buddy's sys.path prepend so any future shared peer
# modules (e.g. a future buddy_chat that bridges to Claude over BLE)
# resolve from /flash. Snake / hello don't need this — they're
# stand-alone — but we want the option to grow into that later
# without a second install touching sys.path.
for _p in ("/flash", "/flash/apps"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import time
import random

import M5
import machine
from hardware import MatrixKeyboard


# ---- NVS persistence (with dev-machine stub) -----------------------
#
# Buddy uses namespace "buddy"; we use "claudachi" so reflashing one
# never disturbs the other. Same pattern as buddy_state.py.
try:
    import esp32

    _NVS = esp32.NVS("claudachi")
except ImportError:
    _NVS = None  # off-device stub — apps still import cleanly


def _nvs_get_i(key, default):
    if _NVS is None:
        return default
    try:
        return _NVS.get_i32(key)
    except Exception:
        return default


def _nvs_set_i(key, value):
    if _NVS is None:
        return
    try:
        _NVS.set_i32(key, int(value))
        _NVS.commit()
    except Exception as e:
        print("claudachi: nvs set", key, "warning:", e)


def _nvs_get_s(key, default):
    if _NVS is None:
        return default
    try:
        buf = bytearray(64)
        n = _NVS.get_blob(key, buf)
        return bytes(buf[:n]).decode("utf-8", errors="replace")
    except Exception:
        return default


# ---- palette (inlined from ui_theme; matches the rest of the suite)
_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY = 0x777777
_BAR_BG = 0x303030
_BAR_OK = 0x9CCC9A   # soft green for healthy stats
_BAR_MID = 0xE8B05C  # warm yellow for medium
_BAR_LOW = 0xCC785C  # brand orange for low (still warm — not alarming)
_PINK = 0xE8A0A0     # cheek blush

_LCD = M5.Lcd
_W = 240
_H = 135


# ---- layout --------------------------------------------------------
_HEADER_H = 20
_HINT_H = 18
_CONTENT_TOP = _HEADER_H + 1   # leave the orange hairline at y=20 visible
_CONTENT_BOT = _H - _HINT_H

_STATS_X = 4
_STATS_W = 82
_STATS_ROW_DY = 22  # vertical step between stat rows

_PET_X0 = _STATS_X + _STATS_W + 6   # 92
_PET_W = _W - _PET_X0 - 4           # 144

# Pet sprite is _SPRITE_COLS x _SPRITE_ROWS cells, each `_CELL_PX`
# pixels square. 16*5 = 80 px wide, 14*5 = 70 px tall — fits the
# 144x91 pet area with margin for bob and a speech caption below.
_SPRITE_COLS = 16
_SPRITE_ROWS = 14
_CELL_PX = 5
_PET_SPRITE_W = _SPRITE_COLS * _CELL_PX
_PET_SPRITE_H = _SPRITE_ROWS * _CELL_PX
_PET_X = _PET_X0 + (_PET_W - _PET_SPRITE_W) // 2  # 124
_PET_Y_BASE = _CONTENT_TOP + 6                    # 27

# Speech caption row sits between the sprite and the hint strip.
_SPEECH_Y = _CONTENT_BOT - 12   # 105
_SPEECH_H = 12


# ---- sprite (shared body + per-state face overlays) ----------------
#
# Body codes: '.' transparent, 'O' orange, 'C' cream.
# Face overlays are tuples of (row, col, color) — drawn after the
# body so they overwrite. We pick face cells inside body cells, so
# transparency is never an issue for the overlay itself.
_BODY = (
    "....OOOOOOOO....",
    "..OOOOOOOOOOOO..",
    ".OOOOOOOOOOOOOO.",
    "OOOOOOOOOOOOOOOO",
    "OOOOOOOOOOOOOOOO",
    "OOOOOOOOOOOOOOOO",
    "OOOOOOOOOOOOOOOO",
    "OOCCCCCCCCCCCCOO",
    "OCCCCCCCCCCCCCCO",
    "OCCCCCCCCCCCCCCO",
    "OCCCCCCCCCCCCCCO",
    "OOCCCCCCCCCCCCOO",
    ".OOOOOOOOOOOOOO.",
    "..OOOOOOOOOOOO..",
)
assert len(_BODY) == _SPRITE_ROWS

# Face overlays. (row, col, color). Coords in sprite-cell space.
_FACE_IDLE = (
    # eyes — two 2x2 dark blocks
    (4, 4, _DARK), (4, 5, _DARK), (5, 4, _DARK), (5, 5, _DARK),
    (4, 10, _DARK), (4, 11, _DARK), (5, 10, _DARK), (5, 11, _DARK),
    # cheeks
    (6, 3, _PINK), (6, 12, _PINK),
    # tiny smile
    (10, 6, _DARK), (10, 7, _DARK), (10, 8, _DARK), (10, 9, _DARK),
    (9, 5, _DARK), (9, 10, _DARK),
)

_FACE_HAPPY = (
    # squinty ^^ eyes (lower-left and upper-right cells filled)
    (5, 4, _DARK), (5, 5, _DARK), (4, 5, _DARK),
    (5, 10, _DARK), (5, 11, _DARK), (4, 10, _DARK),
    (6, 3, _PINK), (6, 12, _PINK),
    # big open smile (cream interior = teeth-ish, dark border)
    (9, 5, _DARK), (9, 6, _DARK), (9, 7, _DARK), (9, 8, _DARK), (9, 9, _DARK), (9, 10, _DARK),
    (10, 5, _DARK), (10, 10, _DARK),
    (10, 6, _CREAM), (10, 7, _CREAM), (10, 8, _CREAM), (10, 9, _CREAM),
    (11, 6, _DARK), (11, 7, _DARK), (11, 8, _DARK), (11, 9, _DARK),
)

_FACE_EATING = (
    # round wide eyes — same as idle
    (4, 4, _DARK), (4, 5, _DARK), (5, 4, _DARK), (5, 5, _DARK),
    (4, 10, _DARK), (4, 11, _DARK), (5, 10, _DARK), (5, 11, _DARK),
    # open chomping mouth
    (9, 6, _DARK), (9, 7, _DARK), (9, 8, _DARK), (9, 9, _DARK),
    (10, 6, _DARK), (10, 7, _DARK), (10, 8, _DARK), (10, 9, _DARK),
    (11, 7, _DARK), (11, 8, _DARK),
)

_FACE_SLEEPING = (
    # closed eyes — single horizontal line per eye
    (5, 4, _DARK), (5, 5, _DARK),
    (5, 10, _DARK), (5, 11, _DARK),
    # peaceful tiny smile
    (10, 7, _DARK), (10, 8, _DARK),
)

_FACE_TALKING = (
    # idle eyes
    (4, 4, _DARK), (4, 5, _DARK), (5, 4, _DARK), (5, 5, _DARK),
    (4, 10, _DARK), (4, 11, _DARK), (5, 10, _DARK), (5, 11, _DARK),
    (6, 3, _PINK), (6, 12, _PINK),
    # mouth slightly open (talking)
    (9, 6, _DARK), (9, 7, _DARK), (9, 8, _DARK), (9, 9, _DARK),
    (10, 6, _DARK), (10, 9, _DARK),
    (10, 7, _CREAM), (10, 8, _CREAM),
)


# ---- talk lines (kid-safe; statements only, never questions) -------
#
# Every line ships in this file. Curated to be:
#   - Positive / encouraging (no scary or sad content)
#   - Statements, never questions (so the pet never appears to be
#     soliciting personal info from a child — important for COPPA)
#   - Short enough to fit on one DejaVu9 line in the 144 px caption
#     row (~24 chars with margin)
#   - A mix of affection, fun facts, and play prompts
_TALK_LINES = (
    "I love you, friend!",
    "Stars are big and bright.",
    "Butterflies taste w/ feet!",
    "Sharing is the best.",
    "I'm thinking of clouds.",
    "Books are tiny adventures.",
    "Let's pretend together!",
    "I dreamed of dancing fish.",
    "Hugs are warm.",
    "Bees help flowers grow.",
    "Naps are good for you.",
    "I made up a song!",
    "Tomorrow will be fun!",
    "You're a good friend.",
    "Curiosity is super cool.",
    "Be kind to grumpy bugs.",
    "Apples are crunchy.",
    "I drew a happy thought.",
    "Laughing is my favorite.",
    "I'm proud of you.",
    "Cats purr at 25 hz!",
    "Rainbows have 7 colors.",
    "Octopi have 3 hearts!",
    "Counting is fun.",
    "Reading takes you places.",
    "Stretching feels great!",
    "Penguins gift each other.",
    "Whales sing love songs.",
    "Snails carry their home.",
    "Daisies follow the sun.",
)


# ---- stat / pacing constants ---------------------------------------

_STAT_MAX = 100
_STAT_DEFAULT = 70
_SMARTS_DEFAULT = 10

# Decay ticks every _DECAY_INTERVAL_POLLS polls. ~40 ms/poll.
# 750 polls ≈ 30 s. Tuned so an hour of unattended runtime drops
# stats to ~35-50 (sleepy but not dire) — enough for the face to
# read as "tired" without crossing any guilt threshold.
_DECAY_INTERVAL_POLLS = 750
_SLEEP_TICK_POLLS = 375  # decay/recover at half-interval while napping

_DECAY_HUNGER_IDLE = -2
_DECAY_ENERGY_IDLE = -1
_DECAY_JOY_IDLE = -1

_DECAY_HUNGER_SLEEP = -1
_RECOVER_ENERGY_SLEEP = +6

# Per-care-action stat deltas. Care always feels rewarding; even
# "overfeeding" past 100 just clamps, never penalizes.
_CARE_FEED_HUNGER = +25
_CARE_FEED_JOY = +5
_CARE_PLAY_JOY = +18
_CARE_PLAY_ENERGY = -10
_CARE_PLAY_HUNGER = -5
_CARE_TALK_JOY = +5
_CARE_TALK_SMARTS = +3

# Mode dwell times (in 40 ms polls).
_MODE_EAT_POLLS = 50    # ~2.0 s
_MODE_PLAY_POLLS = 75   # ~3.0 s
_MODE_TALK_POLLS = 90   # ~3.6 s — long enough to read a full line

# Bob animation: alternate between bob_y=0 and bob_y=2 every period.
# 12 polls ≈ 480 ms — slow enough not to feel jittery, fast enough
# that the pet visibly "breathes."
_BOB_PERIOD_POLLS = 12

# Break reminder. 22500 polls ≈ 15 min of continuous play. Toast
# stays for ~4 s, then auto-clears. Schedule another in another
# 15 min. We never lock the device — this is a suggestion, not a
# barrier.
_BREAK_REMINDER_POLLS = 22500
_BREAK_TOAST_POLLS = 100


# ---- audio cues ----------------------------------------------------
#
# Tone-based "voice" — short tonal motifs per action. Tone() is
# wrapped so a firmware variant without M5.Speaker just silently
# does nothing rather than crashing the app, which is how the rest
# of the suite handles optional hardware (battery reader stub etc.).
#
# Each cue is a sequence of (freq_hz, duration_ms) notes. The
# whole cue blocks the main loop for the sum of its durations —
# kept under ~250 ms total so a kid-spamming-keys session still
# feels responsive. M5.Speaker.tone is non-blocking on the chip
# (it queues to the audio HW), so we sleep for each note's
# duration to actually space them out into a melody.

# High-low "yum" — descending major-third for satisfaction.
_CUE_FEED = ((1200, 80), (800, 100))

# Ascending arpeggio — C-E-G-C, "wheee!"
_CUE_PLAY = ((523, 60), (659, 60), (784, 60), (1047, 80))

# Descending lullaby — soft, longer notes.
_CUE_SLEEP = ((880, 100), (698, 110), (523, 160))

# Quick "boop boop" — same pitch, twice.
_CUE_TALK = ((900, 60), (900, 60))

# First-launch "hatched!" rising third + grace note.
_CUE_HATCH = ((659, 70), (784, 70), (988, 110))

# Generic "hello" on relaunch — single warm chirp.
_CUE_HI = ((784, 90),)


_SPEAKER_DIAG_DONE = [False]


def _init_speaker():
    """One-shot speaker bring-up. UIFlow 2.0 boots the Cardputer-Adv
    speaker at volume=0 — `M5.Speaker.tone()` runs cleanly but plays
    nothing. We try the three commonly-supported volume APIs in
    order and print which ones the firmware accepted, so a future
    debug session can see exactly what worked. Failures are
    swallowed because the M5.Speaker surface varies by firmware
    revision and we'd rather have silence than a crashed app."""
    found = []
    for name, fn in (
        ("begin", lambda: M5.Speaker.begin()),
        ("setVolumePercentage", lambda: M5.Speaker.setVolumePercentage(0.7)),
        ("setVolume", lambda: M5.Speaker.setVolume(200)),
    ):
        try:
            fn()
            found.append(name)
        except Exception as e:
            print("claudachi: speaker", name, "skipped:", e)
    print("claudachi: speaker init OK:", found)


def _tone(freq, ms):
    """One tone, errors swallowed after the first.

    The first failure prints once for diagnostic visibility; later
    failures are dropped so a missing speaker API can't flood the
    serial console at every keypress.
    """
    try:
        M5.Speaker.tone(freq, ms)
    except Exception as e:
        if not _SPEAKER_DIAG_DONE[0]:
            print("claudachi: M5.Speaker.tone failed:", e)
            _SPEAKER_DIAG_DONE[0] = True


def _play_cue(notes):
    """Block the main loop for the duration of the cue. Cues are
    intentionally short (≤ ~250 ms total) so the pause doesn't
    register as input lag."""
    for freq, ms in notes:
        _tone(freq, ms)
        time.sleep_ms(ms)


# ---- pet model -----------------------------------------------------

class Pet:
    """Stats + persistence. No drawing concerns live here."""

    def __init__(self):
        self.name = _nvs_get_s("name", "Cloo")
        self.hunger = _clamp(_nvs_get_i("hunger", _STAT_DEFAULT))
        self.energy = _clamp(_nvs_get_i("energy", _STAT_DEFAULT))
        self.joy = _clamp(_nvs_get_i("joy", _STAT_DEFAULT))
        self.smarts = _clamp(_nvs_get_i("smarts", _SMARTS_DEFAULT))
        # care_actions monotonically increases; level is its sqrt.
        # Same toy formula buddy_state.py uses for the desktop badge.
        self.care_actions = max(0, _nvs_get_i("care", 0))

    def save(self):
        _nvs_set_i("hunger", self.hunger)
        _nvs_set_i("energy", self.energy)
        _nvs_set_i("joy", self.joy)
        _nvs_set_i("smarts", self.smarts)
        _nvs_set_i("care", self.care_actions)

    def feed(self):
        self.hunger = _clamp(self.hunger + _CARE_FEED_HUNGER)
        self.joy = _clamp(self.joy + _CARE_FEED_JOY)
        self.care_actions += 1
        self.save()

    def play(self):
        self.joy = _clamp(self.joy + _CARE_PLAY_JOY)
        self.energy = _clamp(self.energy + _CARE_PLAY_ENERGY)
        self.hunger = _clamp(self.hunger + _CARE_PLAY_HUNGER)
        self.care_actions += 1
        self.save()

    def talk(self):
        self.joy = _clamp(self.joy + _CARE_TALK_JOY)
        self.smarts = _clamp(self.smarts + _CARE_TALK_SMARTS)
        self.care_actions += 1
        self.save()

    def decay(self, sleeping):
        if sleeping:
            self.hunger = _clamp(self.hunger + _DECAY_HUNGER_SLEEP)
            self.energy = _clamp(self.energy + _RECOVER_ENERGY_SLEEP)
        else:
            self.hunger = _clamp(self.hunger + _DECAY_HUNGER_IDLE)
            self.energy = _clamp(self.energy + _DECAY_ENERGY_IDLE)
            self.joy = _clamp(self.joy + _DECAY_JOY_IDLE)
        # We deliberately don't NVS-write per decay tick — it would
        # add ~50 ms of flash latency every 30 s and rack up wear
        # cycles fast on a device meant to run for years. Care
        # actions write through; decay between writes is fine to
        # lose on power-cycle (forgiving for kids, by design).

    def avg_wellbeing(self):
        return (self.hunger + self.energy + self.joy) // 3

    def level(self):
        # Integer floor(sqrt(care_actions)), floor at 1. No math
        # module on this build, so iterate.
        n = self.care_actions
        if n <= 0:
            return 1
        i = 1
        while (i + 1) * (i + 1) <= n:
            i += 1
        return i


def _clamp(v):
    if v < 0:
        return 0
    if v > _STAT_MAX:
        return _STAT_MAX
    return v


# ---- drawing -------------------------------------------------------

def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        # Fall back to whatever the firmware default is — same
        # pattern hello_cardputer.py uses.
        print("claudachi: setFont fallback:", e)


def _draw_chrome():
    """One-shot static chrome: background + header bg + hairline + hint bg.

    Per-frame redraws are scoped to changed regions only; this just
    seeds the canvas so subsequent partial repaints don't leave
    uninitialized SPI memory artifacts behind.
    """
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, _HEADER_H, _DARK)
    _LCD.fillRect(0, _HEADER_H, _W, 1, _ORANGE)
    _LCD.fillRect(0, _H - _HINT_H, _W, _HINT_H, _DARK)


def _draw_header(pet):
    """Title left, name + level right. Repaints on care actions."""
    _LCD.fillRect(0, 0, _W, _HEADER_H, _DARK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Claudachi", 6, 5)
    _LCD.setTextColor(_CREAM, _DARK)
    right = "{} . lvl {}".format(pet.name[:10], pet.level())
    rw = _LCD.textWidth(right)
    _LCD.drawString(right, _W - 6 - rw, 5)


def _bar_color(value):
    if value >= 60:
        return _BAR_OK
    if value >= 25:
        return _BAR_MID
    return _BAR_LOW


def _draw_stat_row(label, value, y):
    """Label on top, bar below. Wipes the row first to handle decreases."""
    _LCD.fillRect(_STATS_X, y, _STATS_W, 18, _BLACK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY, _BLACK)
    _LCD.drawString(label, _STATS_X, y)
    bar_x = _STATS_X
    bar_y = y + 11
    bar_w = _STATS_W
    bar_h = 5
    _LCD.fillRect(bar_x, bar_y, bar_w, bar_h, _BAR_BG)
    fill = (bar_w * value) // _STAT_MAX
    if fill > 0:
        _LCD.fillRect(bar_x, bar_y, fill, bar_h, _bar_color(value))


def _draw_stats(pet):
    base_y = _CONTENT_TOP + 3   # 24
    _draw_stat_row("hunger", pet.hunger, base_y + 0 * _STATS_ROW_DY)
    _draw_stat_row("energy", pet.energy, base_y + 1 * _STATS_ROW_DY)
    _draw_stat_row("joy",    pet.joy,    base_y + 2 * _STATS_ROW_DY)
    _draw_stat_row("smart",  pet.smarts, base_y + 3 * _STATS_ROW_DY)


def _color_for_body_cell(ch):
    if ch == "O":
        return _ORANGE
    if ch == "C":
        return _CREAM
    return None  # transparent


def _draw_pet(face, bob_y, mode):
    """Render body + face overlay at base_y + bob_y. Wipes a band
    slightly larger than the sprite footprint so a 2 px bob shift
    doesn't leave a residual row of orange behind."""
    sprite_top = _PET_Y_BASE + bob_y
    band_top = _PET_Y_BASE - 4
    band_h = _PET_SPRITE_H + 8
    _LCD.fillRect(_PET_X, band_top, _PET_SPRITE_W, band_h, _BLACK)
    # Body
    for r in range(_SPRITE_ROWS):
        row = _BODY[r]
        for c in range(_SPRITE_COLS):
            color = _color_for_body_cell(row[c])
            if color is None:
                continue
            _LCD.fillRect(
                _PET_X + c * _CELL_PX,
                sprite_top + r * _CELL_PX,
                _CELL_PX, _CELL_PX, color,
            )
    # Face overlay
    for (r, c, color) in face:
        _LCD.fillRect(
            _PET_X + c * _CELL_PX,
            sprite_top + r * _CELL_PX,
            _CELL_PX, _CELL_PX, color,
        )
    # Sleeping Z's float beside the head — only state where extra
    # decoration extends outside the sprite cells.
    if mode == "sleeping":
        _LCD.setTextSize(1)
        _LCD.setTextColor(_CREAM, _BLACK)
        _LCD.drawString("z", _PET_X + _PET_SPRITE_W + 2, sprite_top + 4)
        _LCD.setTextSize(2)
        _LCD.drawString("Z", _PET_X + _PET_SPRITE_W + 8, sprite_top - 8)


def _draw_speech(text):
    """Caption row between the sprite and the hint strip. Empty text
    just wipes the row — used to clear an expired bubble."""
    _LCD.fillRect(_PET_X0, _SPEECH_Y - 2, _PET_W, _SPEECH_H, _BLACK)
    if not text:
        return
    _LCD.setTextSize(1)
    _LCD.setTextColor(_CREAM, _BLACK)
    tw = _LCD.textWidth(text)
    bx = _PET_X0 + (_PET_W - tw) // 2
    if bx < _PET_X0 + 2:
        bx = _PET_X0 + 2   # left-align if text is wider than the column
    _LCD.drawString(text, bx, _SPEECH_Y)


def _draw_hint():
    """One-shot at startup. Static legend; never repaints."""
    _LCD.fillRect(0, _H - _HINT_H, _W, _HINT_H, _DARK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY, _DARK)
    legend = "F feed  P play  Z nap  T talk  Q exit"
    tw = _LCD.textWidth(legend)
    _LCD.drawString(legend, max(2, (_W - tw) // 2), _H - _HINT_H + 4)


# ---- input mapping -------------------------------------------------

def _intent(k):
    """Normalize a MatrixKeyboard key into one of:
    feed / play / sleep / talk / exit / None.

    MatrixKeyboard returns ints (ASCII) for printable keys on this
    UIFlow build, and special codes (0x1B Escape, 0x0A or 0x0D Enter)
    for navigation keys. We accept both ints and one-char strings —
    snake.py and claude_buddy.py have the same dual-form handler and
    we copy the shape so a future firmware revision that flips one
    form back doesn't silently break this app.
    """
    if k is None:
        return None
    if isinstance(k, int):
        if k == 0x1B:
            return "exit"
        if k in (0x0A, 0x0D):
            return "talk"  # Enter as easy-reach synonym for talk
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return None
    if isinstance(k, (bytes, bytearray)) and len(k) == 1:
        k = chr(k[0])
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    if ch == "f":
        return "feed"
    if ch == "p":
        return "play"
    if ch == "z":
        return "sleep"
    if ch == "t":
        return "talk"
    if ch == "q":
        return "exit"
    return None


# ---- face selection ------------------------------------------------

def _face_for(mode, pet):
    if mode == "eating":
        return _FACE_EATING
    if mode == "playing":
        return _FACE_HAPPY
    if mode == "sleeping":
        return _FACE_SLEEPING
    if mode == "talking":
        return _FACE_TALKING
    # Idle — peek at wellbeing. Low stats show the sleeping face
    # (peaceful, not sad) instead of a frown. We never want a kid
    # to see their pet looking unhappy at them.
    if pet.avg_wellbeing() < 30:
        return _FACE_SLEEPING
    return _FACE_IDLE


# ---- main loop -----------------------------------------------------

def run():
    print("claudachi: run() start")
    _set_font()
    _init_speaker()
    _draw_chrome()

    pet = Pet()
    _draw_header(pet)
    _draw_stats(pet)
    _draw_hint()

    kb = MatrixKeyboard()
    # Same 400 ms launcher-keypress debounce hello/snake use, so the
    # key that picked Claudachi from App List doesn't immediately
    # register as a care action.
    time.sleep_ms(400)

    mode = "idle"
    mode_polls_left = 0

    speech_text = ""
    speech_polls_left = 0
    prev_rendered_speech = None

    poll = 0
    bob_y = 0
    last_render_pet_state = None
    last_decay_poll = 0
    next_break_poll = _BREAK_REMINDER_POLLS

    # First-launch detection: care_actions == 0 means the kid has
    # never interacted before. A small flavor difference in the
    # opening line — the pet "hatches" rather than "wakes up."
    if pet.care_actions == 0:
        speech_text = "Hi! I just hatched!"
        _play_cue(_CUE_HATCH)
    else:
        speech_text = "Hi! I'm {}.".format(pet.name[:12])
        _play_cue(_CUE_HI)
    speech_polls_left = 90  # ~3.6 s greeting

    try:
        while True:
            kb.tick()
            intent = _intent(kb.get_key())

            # Exit always wins — save first so a power-off after
            # quitting via Q preserves the latest stats.
            if intent == "exit":
                pet.save()
                return

            # Sleeping: any input wakes the pet. If the input was
            # the sleep-toggle (Z), waking is the entire response.
            # Anything else falls through to be processed in idle.
            if mode == "sleeping" and intent:
                mode = "idle"
                if intent == "sleep":
                    speech_text = "Yawn!"
                    speech_polls_left = 50
                    intent = None
                else:
                    # leave intent for the idle handler below
                    speech_text = "*yawn*"
                    speech_polls_left = 30

            # Care actions land from any non-sleeping mode. Letting
            # them interrupt eating/playing/talking is a deliberate
            # responsiveness choice — kids don't want to wait two
            # seconds to feed-then-play.
            if intent and mode != "sleeping":
                stats_changed = True
                if intent == "feed":
                    pet.feed()
                    mode = "eating"
                    mode_polls_left = _MODE_EAT_POLLS
                    speech_text = "Yum!"
                    speech_polls_left = _MODE_EAT_POLLS
                    _play_cue(_CUE_FEED)
                elif intent == "play":
                    pet.play()
                    mode = "playing"
                    mode_polls_left = _MODE_PLAY_POLLS
                    speech_text = "Wheee!"
                    speech_polls_left = _MODE_PLAY_POLLS
                    _play_cue(_CUE_PLAY)
                elif intent == "sleep":
                    mode = "sleeping"
                    mode_polls_left = 0   # sticky until input
                    speech_text = "Zzz..."
                    speech_polls_left = 60
                    stats_changed = False
                    _play_cue(_CUE_SLEEP)
                elif intent == "talk":
                    pet.talk()
                    mode = "talking"
                    mode_polls_left = _MODE_TALK_POLLS
                    speech_text = random.choice(_TALK_LINES)
                    speech_polls_left = _MODE_TALK_POLLS
                    _play_cue(_CUE_TALK)
                else:
                    stats_changed = False
                if stats_changed:
                    _draw_stats(pet)
                    _draw_header(pet)

            # Mode timeout — return to idle when the dwell counter
            # expires, except for sleeping which is sticky.
            if mode_polls_left > 0:
                mode_polls_left -= 1
                if mode_polls_left == 0 and mode != "sleeping":
                    mode = "idle"

            # Decay tick. Sleeping uses a faster cadence so energy
            # visibly recovers in a reasonable wait — otherwise a
            # kid would set the pet to nap and not see the bar
            # move for 30 s, which reads as "broken."
            interval = _SLEEP_TICK_POLLS if mode == "sleeping" else _DECAY_INTERVAL_POLLS
            if poll - last_decay_poll >= interval:
                pet.decay(sleeping=(mode == "sleeping"))
                last_decay_poll = poll
                _draw_stats(pet)

            # Bob: still pet while sleeping (peaceful), bob otherwise.
            if mode == "sleeping":
                bob_y = 0
            else:
                bob_y = ((poll // _BOB_PERIOD_POLLS) % 2) * 2

            # Pet repaint — only when the visible state changes.
            face = _face_for(mode, pet)
            render_state = (id(face), bob_y, mode)
            if render_state != last_render_pet_state:
                _draw_pet(face, bob_y, mode)
                last_render_pet_state = render_state

            # Speech bubble — repaint only on text transitions to
            # avoid burning SPI bandwidth on a row that hasn't
            # changed. When the dwell timer hits zero we snap to
            # empty text, which triggers exactly one wipe.
            display_speech = speech_text if speech_polls_left > 0 else ""
            if display_speech != prev_rendered_speech:
                _draw_speech(display_speech)
                prev_rendered_speech = display_speech
            if speech_polls_left > 0:
                speech_polls_left -= 1

            # Gentle break suggestion. We never block input or hide
            # the pet — just surface a short caption and reschedule
            # for the next 15-min window.
            if poll >= next_break_poll:
                speech_text = "Take a break?"
                speech_polls_left = _BREAK_TOAST_POLLS
                next_break_poll = poll + _BREAK_REMINDER_POLLS

            poll += 1
            time.sleep_ms(40)
    finally:
        # Save before tearing the screen down so a crash inside
        # fillScreen doesn't lose the last few care actions.
        try:
            pet.save()
        except Exception as e:
            print("claudachi: save warning:", e)
        try:
            _LCD.fillScreen(_BLACK)
        except Exception as e:
            print("claudachi: clear warning:", e)
        # Mirror snake / hello / claude_buddy: brief pause so any
        # trailing log line flushes, then soft-reboot back to the
        # launcher (UIFlow has no return-to-launcher API).
        time.sleep_ms(200)
        machine.reset()


# ---- v2 ideas (not implemented; here so the next reader sees the
# trajectory before adding anything that fights it) -----------------
#
# - **Real Claude conversation.** Wire `talk` through buddy_ble so
#   pressing T sends the kid's last few interactions as context to
#   Claude Desktop, which replies through the existing protocol.
#   The desktop side needs a new message type and a kid-safe
#   system prompt; the device side is mostly a buddy_protocol
#   subscription plus a longer speech caption (probably 2 lines).
# - **Hatch animation.** First-launch egg crack → reveal pet.
# - **Naming.** Inline rename UI (typing a name on the QWERTY).
#   We intentionally avoid asking the *kid's* name; only the pet's.
# - **Multiple personalities.** Different sprite color / face
#   variations seeded at hatch by `os.urandom(1)`.


# UIFlow's App List has been observed to invoke apps both as
# __main__ and via import. Same bare-call shape the other apps in
# this bundle use.
run()
