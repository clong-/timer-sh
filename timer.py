#!/usr/bin/env python3
"""A terminal countdown timer with big Unicode box-drawing digits.

Usage:
    ./timer.py 1h30m
    ./timer.py 25m
    ./timer.py 45s
    ./timer.py 5m30s

When the countdown reaches zero, the whole terminal flashes (via reverse
video) and the terminal bell rings repeatedly until Ctrl+C is pressed.
"""

import argparse
import math
import re
import shutil
import signal
import sys
import time

# ---------------------------------------------------------------------------
# Duration parsing: accepts any combination of "XhYmZs" (each part optional,
# but at least one required), e.g. "1h30m", "90s", "5m", "1h5m30s".
# ---------------------------------------------------------------------------

DURATION_RE = re.compile(
    r"^(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$"
)


def parse_duration(value: str) -> int:
    match = DURATION_RE.match(value.strip())
    if not match or not any(match.groups()):
        raise argparse.ArgumentTypeError(
            f"invalid duration {value!r}; expected e.g. '1h30m', '90s', '5m'"
        )
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


# ---------------------------------------------------------------------------
# Big-digit font: each digit is a classic 7-segment display, composed from
# Unicode box-drawing characters so corners/junctions connect cleanly.
# ---------------------------------------------------------------------------

# Segment layout: a=top, b=top-right, c=bottom-right, d=bottom,
#                 e=bottom-left, f=top-left, g=middle
SEGMENTS = {
    "0": "abcdef",
    "1": "bc",
    "2": "abdeg",
    "3": "abcdg",
    "4": "bcfg",
    "5": "acdfg",
    "6": "acdefg",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcdfg",
}

DIGIT_WIDTH = 5
COLON_WIDTH = 3
GLYPH_HEIGHT = 5


def _corner(up: bool, down: bool, left: bool, right: bool) -> str:
    """Pick the box-drawing character that joins the given directions."""
    if up and down and left and right:
        return "╋"
    if up and down and right and not left:
        return "┣"
    if up and down and left and not right:
        return "┫"
    if down and left and right and not up:
        return "┳"
    if up and left and right and not down:
        return "┻"
    if down and right and not up and not left:
        return "┏"
    if down and left and not up and not right:
        return "┓"
    if up and right and not down and not left:
        return "┗"
    if up and left and not down and not right:
        return "┛"
    if up and down and not left and not right:
        return "┃"
    if left and right and not up and not down:
        return "━"
    if up and not down and not left and not right:
        return "╹"
    if down and not up and not left and not right:
        return "╻"
    if left and not right and not up and not down:
        return "╸"
    if right and not left and not up and not down:
        return "╺"
    return " "


def _digit_rows(ch: str):
    if ch == ":":
        return ["   ", " ● ", "   ", " ● ", "   "]

    segs = SEGMENTS[ch]
    a, b, c, d, e, f, g = (s in segs for s in "abcdefg")

    row0 = _corner(False, f, False, a) + ("━━━" if a else "   ") + _corner(False, b, a, False)
    row1 = ("┃" if f else " ") + "   " + ("┃" if b else " ")
    row2 = _corner(f, e, False, g) + ("━━━" if g else "   ") + _corner(b, c, g, False)
    row3 = ("┃" if e else " ") + "   " + ("┃" if c else " ")
    row4 = _corner(e, False, False, d) + ("━━━" if d else "   ") + _corner(c, False, d, False)
    return [row0, row1, row2, row3, row4]


def render_lines(text: str):
    """Render a string of digits/colons into GLYPH_HEIGHT lines of big text."""
    glyphs = [_digit_rows(ch) for ch in text]
    return [" ".join(glyph[row] for glyph in glyphs) for row in range(GLYPH_HEIGHT)]


def format_time(total_seconds: float) -> str:
    total_seconds = max(0, int(round(total_seconds)))
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


# ---------------------------------------------------------------------------
# Terminal control helpers
# ---------------------------------------------------------------------------

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
CLEAR_SCREEN = "\x1b[2J"
CURSOR_HOME = "\x1b[H"
CLEAR_TO_END = "\x1b[J"
REVERSE_VIDEO_ON = "\x1b[?5h"
REVERSE_VIDEO_OFF = "\x1b[?5l"
RESET_ATTRS = "\x1b[0m"
ENTER_ALT_SCREEN = "\x1b[?1049h"
EXIT_ALT_SCREEN = "\x1b[?1049l"
BELL = "\a"


def draw(time_str: str, footer: str = None, full_clear: bool = False) -> None:
    cols, rows = shutil.get_terminal_size(fallback=(80, 24))
    lines = render_lines(time_str)
    width = max(len(line) for line in lines)

    if footer:
        width = max(width, len(footer))
        lines = lines + ["", footer.center(width)]

    lines = [line.ljust(width) for line in lines]

    top_pad = max(0, (rows - len(lines)) // 2)
    left_pad = max(0, (cols - width) // 2)

    frame_lines = [""] * top_pad + [" " * left_pad + line for line in lines]
    frame = "\n".join(frame_lines)

    # A resize can shift where the block lands (or shrink/grow the terminal
    # itself), so a plain "clear to end of screen" after the cursor's final
    # position can leave stale characters from the previous layout sitting
    # above/beside the new one. Do a full clear in that case instead of the
    # normal cheap redraw.
    prefix = CLEAR_SCREEN + CURSOR_HOME if full_clear else CURSOR_HOME
    sys.stdout.write(prefix + frame + CLEAR_TO_END)
    sys.stdout.flush()


def restore_and_exit(signum=None, frame=None) -> None:
    sys.stdout.write(
        REVERSE_VIDEO_OFF
        + SHOW_CURSOR
        + RESET_ATTRS
        + CLEAR_SCREEN
        + CURSOR_HOME
        + EXIT_ALT_SCREEN
    )
    sys.stdout.flush()
    sys.exit(0)


# Set by _on_resize (SIGWINCH) so the running loops know to re-center on the
# next redraw, however far along the countdown/alarm they currently are.
_resized = False


def _on_resize(signum=None, frame=None) -> None:
    global _resized
    _resized = True


# ---------------------------------------------------------------------------
# Main loops
# ---------------------------------------------------------------------------


def run_countdown(total_seconds: int) -> None:
    global _resized
    deadline = time.monotonic() + total_seconds
    last_shown = None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        secs_to_show = math.ceil(remaining)
        if secs_to_show != last_shown or _resized:
            draw(format_time(secs_to_show), full_clear=_resized)
            last_shown = secs_to_show
            _resized = False
        time.sleep(0.05)


def run_alarm() -> None:
    global _resized

    def redraw(full_clear=False):
        draw(format_time(0), footer="TIME'S UP!", full_clear=full_clear)

    redraw()
    _resized = False

    while True:
        if _resized:
            redraw(full_clear=True)
            _resized = False
        sys.stdout.write(REVERSE_VIDEO_ON + BELL)
        sys.stdout.flush()
        time.sleep(0.4)

        if _resized:
            redraw(full_clear=True)
            _resized = False
        sys.stdout.write(REVERSE_VIDEO_OFF)
        sys.stdout.flush()
        time.sleep(0.4)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="timer.py",
        description="A terminal countdown timer with big block digits.",
    )
    parser.add_argument(
        "duration",
        type=parse_duration,
        help="Countdown duration, e.g. '1h30m', '25m', '45s', '5m30s'",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    signal.signal(signal.SIGINT, restore_and_exit)
    if hasattr(signal, "SIGWINCH"):
        signal.signal(signal.SIGWINCH, _on_resize)

    sys.stdout.write(ENTER_ALT_SCREEN + HIDE_CURSOR + CLEAR_SCREEN)
    sys.stdout.flush()

    run_countdown(args.duration)
    run_alarm()


if __name__ == "__main__":
    main()
