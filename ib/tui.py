"""The full-screen shell superday runs in when it owns a terminal.

Why this exists
---------------
The old REPL was `input()` in a loop. That shape has a ceiling: the prompt
sits wherever the last `print()` left the cursor, a list of results is inert
text you can only re-read by scrolling, and there is no way to move a
selection because nothing owns the keyboard. Every complaint about the UI
was really a complaint about that ceiling.

So this module takes the terminal: alternate screen, raw mode, one frame
composed and diffed per event. The transcript scrolls in the upper region,
the input box is pinned to the bottom, and a command may hand back a *view*
-- a live, navigable object drawn beneath the transcript -- instead of a
wall of text.

Why not curses
--------------
`curses` wants to own colour allocation and it renders through terminfo,
which flattens the 24-bit palette the rest of the tool is built on. It also
brings a window/pad model that fights an append-only transcript. Everything
here is ANSI written to a file object, which is the same thing `ui.py`
already does -- one styling story, not two.

The one rule that makes it all work
-----------------------------------
`Shell.prompt()` has `input()`'s exact signature and exception behaviour
(EOFError on ^D, KeyboardInterrupt on ^C). While a command runs, `input` is
bound to it and `sys.stdout` to the transcript. That means every existing
`cmd_*` -- drill, review, edit, the mock scorecard -- runs unchanged inside
the shell and gets the pinned input box for free.
"""
from __future__ import annotations

import builtins
import contextlib
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, NamedTuple

from . import llm, ui
from .ui import RESET, dim, faint, head, ok, paint, pad, truncate, vlen

# ---------------------------------------------------------------- keys


class Completion(NamedTuple):
    """One suggestion: what gets inserted, and what it is for."""
    value: str
    hint: str = ""


class Key(NamedTuple):
    """One decoded keystroke. `ch` is set only when name == 'char'."""
    name: str
    ch: str = ""

    def __str__(self) -> str:
        return self.ch if self.name == "char" else self.name


_CSI_FINAL = {"A": "up", "B": "down", "C": "right", "D": "left",
              "H": "home", "F": "end", "Z": "btab"}
_CSI_TILDE = {"1": "home", "2": "ins", "3": "del", "4": "end",
              "5": "pgup", "6": "pgdn", "7": "home", "8": "end"}
# xterm encodes modifiers as a second CSI parameter; 1 is "none".
_MODS = {2: "shift-", 3: "alt-", 4: "shift-alt-", 5: "ctrl-",
         6: "shift-ctrl-", 7: "alt-ctrl-", 8: "shift-alt-ctrl-"}

# ESC is both a key and the first byte of every arrow key. Nothing can tell
# them apart except how fast the rest arrives, so a lone ESC costs this long.
_ESC_TIMEOUT = 0.06


class MouseEvent(NamedTuple):
    kind: str        # "press" | "drag" | "release" | "wheel-up" | "wheel-down"
    row: int         # 0-based screen row
    col: int         # 0-based screen column


class Reader:
    """Bytes off a raw terminal, out as Key or MouseEvent."""

    def __init__(self, fd: int):
        self.fd = fd
        self._buf = b""

    def _pull(self, timeout: float | None) -> bool:
        try:
            r, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            return False
        if not r:
            return False
        try:
            chunk = os.read(self.fd, 1024)
        except OSError:
            return False
        if not chunk:
            return False
        self._buf += chunk
        return True

    def _byte(self, timeout: float | None) -> int | None:
        if not self._buf and not self._pull(timeout):
            return None
        b, self._buf = self._buf[0], self._buf[1:]
        return b

    def read(self, timeout: float | None = None):
        """Next event, or None if `timeout` elapsed with nothing to report."""
        b = self._byte(timeout)
        if b is None:
            return None
        if b == 0x1b:
            return self._escape()
        if b in (0x0d, 0x0a):
            return Key("enter")
        if b == 0x09:
            return Key("tab")
        if b == 0x7f or b == 0x08:
            return Key("bs")
        if b == 0x00:
            return Key("ctrl-space")
        if b < 0x20:
            return Key("ctrl-" + chr(b + 0x60))
        return Key("char", self._utf8(b))

    def _utf8(self, first: int) -> str:
        """Finish a multi-byte character the terminal split across reads."""
        need = (4 if first >= 0xf0 else 3 if first >= 0xe0
                else 2 if first >= 0xc0 else 1)
        raw = bytes([first])
        for _ in range(need - 1):
            nxt = self._byte(_ESC_TIMEOUT)
            if nxt is None:
                break
            raw += bytes([nxt])
        return raw.decode("utf-8", "replace")

    def _escape(self):
        nxt = self._byte(_ESC_TIMEOUT)
        if nxt is None:
            return Key("esc")
        if nxt == 0x1b:
            # ESC ESC: an alt-escape, or a doubled press. Treat as escape.
            return Key("esc")
        c = chr(nxt)
        if c == "[":
            return self._csi()
        if c == "O":
            # Application cursor mode: ESC O A .. D, and ESC O P.. for F1-F4.
            f = self._byte(_ESC_TIMEOUT)
            if f is None:
                return Key("esc")
            return Key(_CSI_FINAL.get(chr(f), "unknown"))
        if nxt == 0x7f:
            return Key("alt-bs")
        if nxt < 0x20:
            return Key("alt-ctrl-" + chr(nxt + 0x60))
        return Key("alt-" + self._utf8(nxt))

    def _csi(self):
        params = ""
        private = ""
        while True:
            b = self._byte(_ESC_TIMEOUT)
            if b is None:
                return Key("esc")
            c = chr(b)
            if c == "<" and not params:
                private = "<"
                continue
            if c.isdigit() or c == ";":
                params += c
                continue
            return self._csi_final(private, params, c)

    def _csi_final(self, private: str, params: str, final: str):
        if private == "<":
            return self._mouse(params, final)
        parts = params.split(";") if params else []
        mod = ""
        if len(parts) >= 2 and parts[1].isdigit():
            mod = _MODS.get(int(parts[1]), "")
        if final == "~":
            base = _CSI_TILDE.get(parts[0] if parts else "", "unknown")
            return Key(mod + base if base != "unknown" else "unknown")
        base = _CSI_FINAL.get(final)
        if base is None:
            return Key("unknown")
        return Key(mod + base)

    def _mouse(self, params: str, final: str):
        """SGR mouse reports: CSI < button ; col ; row (M press | m release)."""
        parts = params.split(";")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            return Key("unknown")
        btn, col, row = int(parts[0]), int(parts[1]), int(parts[2])
        if btn & 64:
            return MouseEvent("wheel-up" if btn & 1 == 0 else "wheel-down",
                              row - 1, col - 1)
        if btn & 32:
            # Motion. The low two bits say which button is down, and 3 means
            # none of them -- that is a hover rather than a drag, and treating
            # it as a drag would start a text selection from wherever the
            # pointer happened to enter the window.
            kind = "move" if (btn & 3) == 3 else "drag"
            return MouseEvent(kind, row - 1, col - 1)
        kind = "release" if final == "m" else "press"
        return MouseEvent(kind, row - 1, col - 1)


# ---------------------------------------------------------------- screen

ALT_ON = "\033[?1049h"
ALT_OFF = "\033[?1049l"
CURSOR_HIDE = "\033[?25l"
CURSOR_SHOW = "\033[?25h"
# 1000 = press/release + wheel, 1002 = motion while a button is held
# (drag-select), 1003 = every motion, which is what makes hover possible.
# 1003 was left off for a long time because it fires on every pixel of every
# mouse move and there was nothing to hover; now the row under the pointer
# lights up, and the cost is paid for by refusing to repaint when the row it
# resolves to has not changed (`Shell._hover`).
MOUSE_ON = "\033[?1000h\033[?1002h\033[?1003h\033[?1006h"
MOUSE_OFF = "\033[?1006l\033[?1003l\033[?1002l\033[?1000l"


class Screen:
    """A frame buffer that only writes the rows that actually changed.

    Repainting the whole screen on every keystroke makes the cursor stutter
    and the borders shimmer on a slow connection. Diffing per row means a
    cursor move down a list costs two lines of output.
    """

    def __init__(self, out=None):
        self.out = out or sys.__stdout__
        self._prev: list[str] = []
        self._active = False

    def size(self) -> tuple[int, int]:
        try:
            sz = os.get_terminal_size(self.out.fileno())
            return max(20, sz.columns), max(6, sz.lines)
        except Exception:
            return 80, 24

    def begin(self, mouse: bool = True) -> None:
        self.out.write(ALT_ON + CURSOR_HIDE + (MOUSE_ON if mouse else ""))
        self.out.flush()
        self._prev = []
        self._active = True

    def end(self) -> None:
        if not self._active:
            return
        # RESET before leaving: the theme's background is an SGR attribute
        # rather than a property of the alt screen, so without it the colour
        # follows us back out and every prompt after quitting is drawn on it.
        self.out.write(RESET + MOUSE_OFF + CURSOR_SHOW + ALT_OFF)
        self.out.flush()
        self._active = False

    def invalidate(self) -> None:
        self._prev = []

    def render(self, lines: list[str], rows: int,
               cursor: tuple[int, int] | None = None) -> None:
        buf = [CURSOR_HIDE]
        # The theme's own ground, or "" when it inherits the terminal's. It is
        # emitted before the erase as well as inside the line, because
        # `\033[K` clears with whatever background is current -- without it
        # the erase paints the terminal's background and the theme is a
        # foreground palette floating on someone else's colour, which is the
        # state that made `faint` unreadable in the first place.
        bg = ui.colour("ground", bg=True)
        cols = self.size()[0]
        for i in range(rows):
            want = lines[i] if i < len(lines) else ""
            had = self._prev[i] if i < len(self._prev) else None
            if want == had:
                continue
            buf.append(f"\033[{i + 1};1H" + bg + "\033[K")
            if want:
                buf.append(ui.ground(want, cols) if bg else want)
            elif bg:
                buf.append(ui.RESET)
        self._prev = list(lines[:rows]) + [""] * max(0, rows - len(lines))
        if cursor:
            buf.append(f"\033[{cursor[0] + 1};{cursor[1] + 1}H" + CURSOR_SHOW)
        self.out.write("".join(buf))
        self.out.flush()


def selection_text(lines: list[str], start: tuple[int, int],
                   end: tuple[int, int]) -> str:
    """Plain text spanned by two (line index, visible column) points.

    Whole lines in between are taken in full; the end points are clipped to
    their own line. Points may arrive in either order -- a drag can go
    either direction.
    """
    (si, sc), (ei, ec) = (start, end) if start <= end else (end, start)
    parts: list[str] = []
    for idx in range(si, ei + 1):
        if idx < 0:
            continue
        if idx >= len(lines):
            break
        plain = ui.strip(lines[idx])
        col0 = sc if idx == si else 0
        col1 = ec if idx == ei else len(plain)
        parts.append(plain[col0:col1])
    return "\n".join(parts)


def copy_to_clipboard(text: str) -> int:
    """Best effort: pbcopy, so a drag-select lands on the system clipboard.

    Silent no-op off a Mac -- there is no second clipboard story to fall
    back to, and a copy that quietly does nothing is better than one that
    crashes the shell.
    """
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
    except OSError:
        return 0
    return len(text)


@contextlib.contextmanager
def raw_mode(fd: int):
    import termios
    import tty
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        # Keep ^C reaching us as a byte rather than a signal: the shell wants
        # to cancel the current line, not tear the process down.
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


# ---------------------------------------------------------------- editor


class Editor:
    """One line of text with a cursor, history and completion.

    Bindings follow readline because that is what the fingers already know:
    ^A/^E, ^B/^F, ^W, ^U, ^K, alt-b/alt-f, up/down for history.
    """

    def __init__(self, history: list[str] | None = None,
                 completer: Callable[[str, int], tuple[list, int]] | None = None):
        self.buf = ""
        self.pos = 0
        self.history = history if history is not None else []
        self.hist_idx: int | None = None
        self.stash = ""
        self.completer = completer
        self.menu: list[Completion] = []
        self.menu_idx = 0
        self.menu_start = 0
        self.menu_dismissed = False

    # -- text ops
    def clear(self) -> None:
        self.buf, self.pos = "", 0
        self.hist_idx = None
        self.close_menu()

    def close_menu(self) -> None:
        self.menu = []
        self.menu_idx = 0

    def insert(self, text: str) -> None:
        self.buf = self.buf[:self.pos] + text + self.buf[self.pos:]
        self.pos += len(text)

    def _word_left(self) -> int:
        i = self.pos
        while i > 0 and self.buf[i - 1].isspace():
            i -= 1
        while i > 0 and not self.buf[i - 1].isspace():
            i -= 1
        return i

    def _word_right(self) -> int:
        i = self.pos
        n = len(self.buf)
        while i < n and self.buf[i].isspace():
            i += 1
        while i < n and not self.buf[i].isspace():
            i += 1
        return i

    # -- history
    def _history_move(self, delta: int) -> None:
        if not self.history:
            return
        if self.hist_idx is None:
            if delta > 0:
                return
            self.stash = self.buf
            self.hist_idx = len(self.history)
        idx = self.hist_idx + delta
        if idx >= len(self.history):
            self.hist_idx = None
            self.buf, self.pos = self.stash, len(self.stash)
            return
        idx = max(0, idx)
        self.hist_idx = idx
        self.buf = self.history[idx]
        self.pos = len(self.buf)

    def remember(self, line: str) -> None:
        if line and (not self.history or self.history[-1] != line):
            self.history.append(line)
        del self.history[:-1000]

    # -- completion

    def _lookup(self) -> tuple[list[Completion], int]:
        if not self.completer:
            return [], self.pos
        cands, start = self.completer(self.buf, self.pos)
        out = [c if isinstance(c, Completion) else Completion(str(c))
               for c in cands]
        return out, start

    def word(self) -> tuple[str, int]:
        """The word under the cursor and where it starts."""
        i = self.pos
        while i > 0 and not self.buf[i - 1].isspace():
            i -= 1
        return self.buf[i:self.pos], i

    def refresh_menu(self) -> None:
        """Recompute the suggestion list after the text changed.

        There is no separate rule for when a menu is appropriate: the
        completer only knows bounded vocabularies -- command names, flags,
        a flag's allowed values, paths -- and returns nothing anywhere else.
        A search phrase or a spoken drill answer therefore opens nothing on
        its own, without the editor having to guess at intent.
        """
        cur, _ = self.word()
        if self.menu_dismissed or not cur:
            self.close_menu()
            return
        cands, start = self._lookup()
        cur, _ = self.word()
        # A single suggestion identical to what is typed is not a suggestion.
        if len(cands) == 1 and cands[0].value == cur:
            self.close_menu()
            return
        self.menu, self.menu_start = cands[:50], start
        self.menu_idx = min(self.menu_idx, max(0, len(self.menu) - 1))

    def accept(self) -> bool:
        """Take the highlighted suggestion. False if there was nothing to take."""
        if not self.menu:
            return False
        chosen = self.menu[self.menu_idx].value
        tail = self.buf[self.pos:]
        # A trailing space, unless the tail already starts with one: accepting
        # a suggestion mid-line must not leave a double space behind.
        sep = "" if tail[:1].isspace() or chosen.endswith("/") else " "
        self.buf = self.buf[:self.menu_start] + chosen + sep + tail
        self.pos = self.menu_start + len(chosen) + len(sep)
        self.close_menu()
        self.menu_dismissed = False
        return True

    def complete(self) -> None:
        """Tab. Opens the menu if closed, otherwise takes the highlighted row."""
        self.menu_dismissed = False
        if self.menu:
            self.accept()
            return
        cands, start = self._lookup()
        if not cands:
            return
        cur = self.buf[start:self.pos]
        if len(cands) == 1:
            self.menu, self.menu_start, self.menu_idx = cands, start, 0
            self.accept()
            return
        # Fill in as far as every candidate agrees, then show the rest.
        prefix = os.path.commonprefix([c.value for c in cands])
        if len(prefix) > len(cur):
            self.buf = self.buf[:start] + prefix + self.buf[self.pos:]
            self.pos = start + len(prefix)
        self.menu, self.menu_start, self.menu_idx = cands[:50], start, 0

    def menu_move(self, delta: int) -> None:
        if self.menu:
            self.menu_idx = (self.menu_idx + delta) % len(self.menu)

    _TEXT_KEYS = {"char", "bs", "ctrl-h", "del", "ctrl-d", "ctrl-w", "alt-bs",
                  "ctrl-u", "ctrl-k", "tab"}

    def handle(self, key: Key) -> str | None:
        """Apply a keystroke. Returns the line when Enter commits it."""
        n = key.name
        if n == "char":
            self.insert(key.ch)
            self.menu_dismissed = False
        elif n == "enter":
            line = self.buf
            self.clear()
            return line
        elif n == "tab":
            self.complete()
            return None
        elif n in ("bs", "ctrl-h"):
            if self.pos:
                self.buf = self.buf[:self.pos - 1] + self.buf[self.pos:]
                self.pos -= 1
        elif n in ("del", "ctrl-d"):
            self.buf = self.buf[:self.pos] + self.buf[self.pos + 1:]
        elif n in ("left", "ctrl-b"):
            self.pos = max(0, self.pos - 1)
        elif n in ("right", "ctrl-f"):
            self.pos = min(len(self.buf), self.pos + 1)
        elif n in ("home", "ctrl-a"):
            self.pos = 0
        elif n in ("end", "ctrl-e"):
            self.pos = len(self.buf)
        elif n in ("alt-b", "ctrl-left", "alt-left"):
            self.pos = self._word_left()
        elif n in ("alt-f", "ctrl-right", "alt-right"):
            self.pos = self._word_right()
        elif n in ("ctrl-w", "alt-bs"):
            i = self._word_left()
            self.buf = self.buf[:i] + self.buf[self.pos:]
            self.pos = i
        elif n == "ctrl-u":
            self.buf = self.buf[self.pos:]
            self.pos = 0
        elif n == "ctrl-k":
            self.buf = self.buf[:self.pos]
        elif n in ("up", "ctrl-p"):
            self._history_move(-1)
        elif n in ("down", "ctrl-n"):
            self._history_move(1)
        if n in self._TEXT_KEYS:
            self.refresh_menu()
        else:
            self.close_menu()
        return None


# ---------------------------------------------------------------- transcript


class Transcript:
    """Append-only scrollback, wrapped lazily and cached per width.

    Commands emit logical lines; the shell has to draw them inside a frame
    that can change width under a resize. Re-wrapping everything on every
    keystroke is wasteful, so the wrap is cached and only invalidated when
    the width actually moves.
    """

    def __init__(self, limit: int = 20000):
        self.lines: list[str] = []
        self.limit = limit
        self._wrapped: list[str] = []
        self._width = -1
        self._done = 0        # how many logical lines are already wrapped

    def append(self, line: str) -> None:
        self.lines.append(line)
        if len(self.lines) > self.limit:
            drop = len(self.lines) - self.limit
            del self.lines[:drop]
            self._width = -1          # positions shifted; rebuild
        if self._width >= 0:
            self._wrapped.extend(ui.hard_wrap(line, self._width))
            self._done = len(self.lines)

    def extend(self, lines: list[str]) -> None:
        for line in lines:
            self.append(line)

    def clear(self) -> None:
        self.lines.clear()
        self._wrapped.clear()
        self._done = 0

    def mark(self) -> int:
        return len(self.lines)

    def rewind(self, mark: int) -> None:
        """Drop everything written since `mark`.

        The one exception to append-only, and it exists for one thing: a drill
        prints a question, a rubric and a grade -- twenty-odd lines -- and once
        you have rated it, all twenty are scrollback you scroll past to get to
        the next question. Rewinding to the mark and emitting one line in its
        place keeps the sitting readable; `recap` is where the twenty lines go.

        The wrap cache is dropped rather than trimmed: it is a flat list with
        no record of which logical line each row came from, so the honest move
        is to rebuild it. That costs a few milliseconds, once per question.
        """
        if mark < 0 or mark >= len(self.lines):
            return
        del self.lines[mark:]
        self._width = -1
        self._wrapped = []
        self._done = 0

    def wrapped(self, width: int) -> list[str]:
        if width != self._width:
            self._width = width
            self._wrapped = []
            for line in self.lines:
                self._wrapped.extend(ui.hard_wrap(line, width))
            self._done = len(self.lines)
        elif self._done < len(self.lines):
            for line in self.lines[self._done:]:
                self._wrapped.extend(ui.hard_wrap(line, width))
            self._done = len(self.lines)
        return self._wrapped


# ---------------------------------------------------------------- views


class View:
    """A live region drawn under the transcript that owns the arrow keys.

    Subclasses fill `owner` during `render()`: one entry per emitted line,
    holding the index of the item that line belongs to, or -1 for chrome.
    That is what lets a mouse click land on the right row.
    """

    title = ""

    def __init__(self) -> None:
        self.owner: list[int] = []
        self.viewport = 20      # rows the shell can spare; set before render

    def render(self, width: int) -> list[str]:
        raise NotImplementedError

    def handle(self, key: Key, shell: "Shell") -> bool:
        return False

    def click(self, item: int, shell: "Shell") -> bool:
        return False

    def hover_at(self, item: int | None, col: int) -> bool:
        """The pointer moved onto `item` (None = off the view's rows).

        Return True when something changed and the frame has to be redrawn.
        Returning False for a move that lands on the row already lit is what
        makes motion reporting affordable.
        """
        return False

    def click_at(self, item: int, col: int, shell: "Shell") -> bool:
        """A click, with the column it landed on.

        Rows are a whole line each, so most views only care which row was hit
        and answer through `click`. A tab bar is the exception: five tabs
        share one line, and which one you meant is entirely a column.
        """
        return self.click(item, shell)

    def scroll_by(self, delta: int) -> bool:
        """Move by a wheel notch. False when the view is already at that end,
        which is what hands the scroll back to the transcript."""
        return False

    def on_resume(self) -> None:
        """A command the view started has finished and the view is coming back.

        Anything derived from the database is now one command out of date --
        the whole reason a view runs a command is to change something it is
        showing. Doing nothing is the right default for a view that reads
        nothing, but the hook has to exist on the base class: `run_now` calls
        it on whatever it parked, and only `ResultsView` had one, so a `drill`
        started from the `tags` list took the shell down on its way back.
        """

    def footer(self) -> str:
        return ""

    def flatten(self, width: int) -> list[str]:
        """The whole thing as plain text, for a caller that has no shell.

        A piped `find` and a one-shot `browse` print this. It is deliberately
        *not* what dismissing a view leaves behind -- see `Shell.detach`.
        """
        return self.render(width)


# ---------------------------------------------------------------- shell


class _TranscriptOut:
    """A stdout that lands in the transcript instead of on the terminal.

    Reports as a tty on purpose: `ui.colors_enabled()` asks stdout whether it
    is one, and the answer has to stay yes or every command loses its colour
    the moment it runs inside the shell.
    """

    def __init__(self, shell: "Shell", style: str = ""):
        self.shell = shell
        self.style = style
        self._pending = ""
        self._last_paint = 0.0

    def write(self, s: str) -> int:
        if not s:
            return 0
        text = self._pending + s
        # A \r-terminated progress line overwrites the one before it rather
        # than stacking twenty copies of "extracting 14/200" in scrollback.
        while True:
            nl = text.find("\n")
            cr = text.find("\r")
            if nl < 0 and cr < 0:
                break
            if nl >= 0 and (cr < 0 or nl < cr):
                self.shell.emit(self._dress(text[:nl]))
                text = text[nl + 1:]
            else:
                self.shell.emit(self._dress(text[:cr]), transient=True)
                text = text[cr + 1:]
        self._pending = text
        now = time.monotonic()
        if now - self._last_paint > 0.04:
            self._last_paint = now
            self.shell.paint()
        return len(s)

    def _dress(self, line: str) -> str:
        """Anything a command sends to stderr is marked as such."""
        if not self.style or not line.strip():
            return line
        return ui.bad("  " + line.strip())

    def flush(self) -> None:
        if self._pending:
            self.shell.emit(self._dress(self._pending))
            self._pending = ""
        self.shell.paint()

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return sys.__stdout__.fileno()

    @property
    def encoding(self) -> str:
        return "utf-8"


class Shell:
    """The event loop: one frame per keystroke, input pinned to the bottom."""

    # One blank column down each side, and everything else is the frame. An
    # earlier version capped this at 100 columns, which left a wide terminal
    # with the box floating in the left two thirds of the window.
    GUTTER = 1

    def __init__(self, *, on_submit: Callable[["Shell", str], None],
                 completer=None, history: list[str] | None = None,
                 status: Callable[[], str] | None = None,
                 hints: Callable[["Shell"], str] | None = None,
                 header: Callable[["Shell"], list[str]] | None = None,
                 on_ready: Callable[["Shell"], None] | None = None,
                 on_clear: Callable[["Shell"], None] | None = None,
                 redact: Callable[[str], str] | None = None,
                 prompt_label: str = "›"):
        self.screen = Screen()
        self.reader = Reader(sys.__stdin__.fileno())
        self.transcript = Transcript()
        self.editor = Editor(history=history if history is not None else [],
                             completer=completer)
        self.on_submit = on_submit
        self.status = status
        self.hints = hints
        self.header = header
        self.on_ready = on_ready
        self.on_clear = on_clear
        # What a typed line looks like once it is allowed to persist. Only
        # the parser sees the line itself: an API key you type must not end up
        # echoed in the transcript, one ↑ away in the history, or written to
        # the history file in $HOME. See `cli.redact`.
        self.redact = redact or (lambda line: line)
        self.prompt_label = prompt_label
        self.view: View | None = None
        self.scroll = 0            # wrapped lines above the bottom
        self.running = False
        self.tick = 0
        self.busy = ""
        self._transient: str | None = None
        self._resized = False
        self._label = ""
        self._cols, self._rows = self.screen.size()
        self._painting = False
        self._view_rows: dict[int, int] = {}
        self._screen_top = 0
        self._header_rows = 0
        self._injected: str | None = None
        self._resume_view: View | None = None
        self._view_stack: list[View] = []
        self._view_echo: tuple[int, int] | None = None
        self._cmd_echo_mark: int | None = None
        self._compose_lines: list[str] = []
        self._sel_start: tuple[int, int] | None = None
        self._sel_end: tuple[int, int] | None = None
        self._selecting = False
        self._toast: str | None = None
        self._in_command = False
        self._from_view = False
        self._repaint = True

    # -- geometry
    @property
    def width(self) -> int:
        return max(40, self._cols - self.GUTTER * 2)

    def _measure(self) -> None:
        self._cols, self._rows = self.screen.size()
        # Every render path asks ui.width() for its budget, including ones
        # that run while a command is mid-flight, so it tracks the frame here
        # rather than only at the top of the loop.
        ui.WIDTH_OVERRIDE = self.width

    # -- transcript
    def emit(self, line: str, transient: bool = False) -> None:
        if transient:
            self._transient = line
            return
        if self._transient is not None:
            self._transient = None
        for part in line.split("\n"):
            self.transcript.append(part)
        self.scroll = 0

    def say(self, *lines: str) -> None:
        for line in lines:
            self.emit(line)

    def mark(self) -> int:
        """Where the transcript is now, so a command can fold back to here."""
        with contextlib.suppress(Exception):
            sys.stdout.flush()
        return self.transcript.mark()

    def collapse(self, mark: int, lines: list[str]) -> None:
        """Replace everything since `mark` with `lines`."""
        with contextlib.suppress(Exception):
            sys.stdout.flush()
        self._transient = None
        self.transcript.rewind(mark)
        for line in lines:
            self.emit(line)
        self.scroll = 0
        self.screen.invalidate()

    def clear(self) -> None:
        """Empty the screen: transcript, view, scroll position and selection."""
        self.transcript.clear()
        self.detach()
        self._view_stack.clear()
        self.scroll = 0
        self._transient = None
        self.screen.invalidate()
        self._clear_selection()
        if self.on_clear:
            self.on_clear(self)

    def _clear_selection(self) -> None:
        self._sel_start = self._sel_end = None
        self._selecting = False

    # -- views
    def attach(self, view: View) -> None:
        self.detach()
        self.view = view
        self.scroll = 0
        self._clear_selection()

    def detach(self) -> None:
        """Put the view down. It leaves nothing behind.

        It used to print itself into the transcript on the way out. For a
        `find` with eight hits that looked tidy; for a `browse` of the whole
        bank it meant a thousand rows landing in scrollback -- and not only on
        esc, but every time you opened a second view or started a drill from a
        row, because both of those park the current view first. Three
        different gestures, one wall of dead text above the prompt, and no way
        to get rid of it short of `clear`.

        The list is still one keystroke away: the command that drew it is in
        the history. What is on screen is the live thing, and putting it down
        gets you the screen back.

        The `›  browse` line that opened it goes down with it, for the same
        reason: it heralds a screen that no longer exists, and left behind it
        is one more line of "browse" in the scrollback every time you open
        it again, forever, with nothing to tell two visits apart.

        It takes the echo and nothing else, which is why the span is checked
        rather than just rewound to. A rewind to "before the echo" deletes
        whatever was written after it too, and plenty is: run `check 5` while
        a `find` is up and its six lines of output sit past that mark, so
        pressing esc on the list threw the output of an unrelated command
        away and left the `› find` line it was aiming at. If anything at all
        has landed since, the echo has become a heading for real text and
        stays.

        That costs one line in the case where a second view replaces the
        first without an esc in between: the first echo is no longer the tail
        by then, so it stays. Deliberate -- lifting it out of the middle
        would shift every mark taken above it, including the ones `show` and
        the drill fold are holding, and a stale mark is how this went wrong
        in the first place. A command you ran is a fair thing to leave in the
        scrollback; a command's output is not a fair thing to delete.
        """
        if self.view is None:
            return
        self.view = None
        self._clear_selection()
        if self._view_echo is not None:
            start, end = self._view_echo
            if self.transcript.mark() == end:
                self.transcript.rewind(start)
            self._view_echo = None

    # ------------------------------------------------------------ frame

    def _input_panel(self, label: str) -> tuple[list[str], tuple[int, int]]:
        """The bottom box, plus where the cursor sits inside it."""
        w = self.width
        inner = w - 4
        lead = (paint(label, "accent", ui.BOLD) + " ") if label else ""
        lead_w = vlen(lead)
        text = self.editor.buf
        avail = max(8, inner - lead_w)

        # Scroll the text horizontally rather than growing the box: a pasted
        # paragraph should not eat the transcript.
        start = 0
        if self.editor.pos > avail - 1:
            start = self.editor.pos - (avail - 1)
        shown = text[start:start + avail]
        cur_col = self.editor.pos - start

        cursor_ch = shown[cur_col] if cur_col < len(shown) else " "
        body = (ui.paint(shown[:cur_col], "text")
                + ui.style(cursor_ch, ui.REVERSE)
                + ui.paint(shown[cur_col + 1:], "text"))
        row = lead + body
        box = ui.ROUND
        top = paint(box["tl"] + box["h"] * (w - 2) + box["tr"], "accent_dim")
        bottom = paint(box["bl"] + box["h"] * (w - 2) + box["br"], "accent_dim")
        mid = (paint(box["v"], "accent_dim") + " " + pad(row, inner) + " "
               + paint(box["v"], "accent_dim"))
        return [top, mid, bottom], (0, 2 + lead_w + cur_col)

    MENU_ROWS = 8

    def _menu_rows(self) -> list[str]:
        """The suggestion list, drawn directly above the input box.

        It sits above rather than below because the box is pinned to the
        bottom of the terminal -- there is no below. The window slides with
        the highlight so a long list stays one keypress deep.
        """
        menu = self.editor.menu
        if not menu:
            return []
        w = self.width
        typed, _ = self.editor.word()
        name_w = min(24, max(len(c.value) for c in menu) + 2)

        total = len(menu)
        show = min(self.MENU_ROWS, total)
        top = max(0, min(self.editor.menu_idx - show // 2, total - show))
        rows: list[str] = []
        for i in range(top, top + show):
            c = menu[i]
            chosen = i == self.editor.menu_idx
            # The part you already typed is lit so the list reads as a
            # continuation of the line rather than a separate thing.
            lit = len(typed) if c.value.lower().startswith(typed.lower()) else 0
            name = (paint(c.value[:lit], "accent", ui.BOLD)
                    + (ui.style(c.value[lit:], ui.BOLD) if chosen
                       else ui.paint(c.value[lit:], "text")))
            body = pad(name, name_w) + faint(truncate(c.hint, max(0, w - name_w - 6)))
            marker = paint("▎", "accent") if chosen else " "
            rows.append(" " + marker + " " + pad(body, w - 4))
        if total > show:
            rows.append("   " + faint(f"{self.editor.menu_idx + 1} of {total}"
                                      "   ↑↓ to move · ⇥ or ⏎ to take it"))
        return rows

    def _hint_row(self) -> str:
        w = self.width
        left = ""
        if self.busy:
            left = ui.spinner_frame(self.tick) + " " + paint(self.busy, "muted")
        elif self.view is not None and self.view.footer():
            left = self.view.footer()
        elif self._in_command:
            # A command that owns the prompt prints its own hint line right
            # above the input box -- `drill` offers "Enter reveals / s skip /
            # q quit", `review` and `edit` their own letters. Drawing the
            # shell's global keys underneath that put two keymaps on screen at
            # once, and the bottom one was the wrong one: `d drill` and
            # `g dashboard` do not fire while a drill is reading your answer,
            # they go into the answer. This is the same rule `TabsView(footer
            # =False)` already applies for `show`; the difference is only that
            # a command driving `input()` has no view to hang it on.
            left = ""
        elif self.hints:
            left = self.hints(self)
        right = faint(self._toast) if self._toast else (self.status() if self.status else "")
        gap = w - vlen(left) - vlen(right) - 2
        if gap < 1:
            return "  " + truncate(left, w - 2)
        return "  " + left + " " * gap + right

    def compose(self) -> tuple[list[str], tuple[int, int]]:
        self._measure()
        w, h = self.width, self._rows
        cand = self._menu_rows()
        panel, (crow, ccol) = self._input_panel(self._label or self.prompt_label)
        header_lines = self.header(self) if self.header else []
        self._header_rows = len(header_lines)
        chrome = len(cand) + len(panel) + 1 + self._header_rows  # +1 hint row
        body_h = max(1, h - chrome)

        lines = list(self.transcript.wrapped(w))
        self._view_rows = {}
        if self.view is not None:
            # The view windows itself rather than emitting 400 rows and
            # letting the transcript swallow the top of its own header.
            self.view.viewport = max(4, body_h - 4)
            vlines = self.view.render(w)
            owner = list(self.view.owner) + [-1] * max(0, len(vlines) - len(self.view.owner))
            base = len(lines)
            for i, item in enumerate(owner[:len(vlines)]):
                if item >= 0:
                    self._view_rows[base + i] = item
            lines.extend(vlines)
        self._compose_lines = lines

        total = len(lines)
        max_scroll = max(0, total - body_h)
        self.scroll = max(0, min(self.scroll, max_scroll))
        # Scrolled up, the last row goes to the "more below" marker, so the
        # window has to give it up rather than have it painted over -- that
        # cost a line of content on every scrolled frame.
        view_h = max(1, body_h - 1) if self.scroll > 0 else body_h
        top = max(0, total - view_h - self.scroll)
        window = lines[top:top + view_h]
        bounds = self._selection_bounds()
        if bounds is not None:
            (si, sc), (ei, ec) = bounds
            window = [
                line if (idx := top + i) < si or idx > ei else
                ui.highlight_range(line, sc if idx == si else 0,
                                   ec if idx == ei else vlen(line))
                for i, line in enumerate(window)
            ]
        if self._transient is not None and self.scroll == 0:
            window = (window + [self._transient])[-body_h:]
            top = max(0, total + 1 - body_h)
        # Push short content to the bottom so the input box never floats in
        # the middle of an empty screen.
        blanks = view_h - len(window)
        out = [""] * blanks + window
        self._screen_top = top - blanks

        if self.scroll > 0:
            out.append(faint(f"  ↓ {self.scroll} more below · esc to jump back"))

        out.extend(cand)
        out.extend(panel)
        out.append(self._hint_row())
        out = header_lines + out
        gut = " " * self.GUTTER
        out = [gut + line if line else "" for line in out]
        cursor = (self._header_rows + body_h + len(cand) + crow + 1, ccol + self.GUTTER)
        return out, cursor

    def paint(self) -> None:
        if self._painting:
            return
        self._painting = True
        try:
            lines, cursor = self.compose()
            self.screen.render(lines, self._rows, cursor)
        finally:
            self._painting = False

    # ------------------------------------------------------------ input

    def _scroll_by(self, delta: int) -> None:
        self.scroll = max(0, self.scroll + delta)

    def _page(self) -> int:
        return max(1, self._rows - 8)

    def _dispatch_nav(self, key: Key) -> bool:
        """Keys the shell itself always owns, whatever has focus."""
        n = key.name
        if n in ("pgup", "pgdn") and self.view is not None and not self.editor.buf:
            # A page key is about whatever you are reading, and with a view up
            # that is the list, not the transcript behind it. Walking a
            # thousand rows one ↓ at a time was the only way through.
            return False
        if n in ("pgup", "shift-up", "alt-up"):
            self._scroll_by(self._page() if n == "pgup" else 1)
            return True
        if n in ("pgdn", "shift-down", "alt-down"):
            self._scroll_by(-(self._page() if n == "pgdn" else 1))
            return True
        if n == "ctrl-l":
            # ^L is "clear the screen" in every shell there is, and answering
            # it with a repaint of the same thousand rows is not that. Inside
            # a running command it stays a repaint: the question you are being
            # asked is in the transcript, and wiping it mid-answer is not what
            # anyone means by clear.
            if self._in_command:
                self.screen.invalidate()
            else:
                self.clear()
            return True
        return False

    def _abs_pos(self, row: int, col: int) -> tuple[int, int]:
        """Screen (row, col) -> (line index, visible column) into
        `_compose_lines`, undoing the scroll offset, the pinned header and
        the gutter.

        A row inside the pinned header has no transcript line at all -- it
        used to alias to the body's top row, which meant a drag started on
        the banner silently selected whatever the transcript happened to
        have there. -1 is never a valid index, so `selection_text` just
        skips it instead.
        """
        if row < self._header_rows:
            return (-1, max(0, col - self.GUTTER))
        return (self._screen_top + row - self._header_rows, max(0, col - self.GUTTER))

    def _selection_bounds(self):
        if self._sel_start is None or self._sel_end is None:
            return None
        return ((self._sel_start, self._sel_end) if self._sel_start <= self._sel_end
                else (self._sel_end, self._sel_start))

    def _copy_selection(self) -> None:
        if self._sel_start is None or self._sel_end is None:
            return
        text = selection_text(self._compose_lines, self._sel_start, self._sel_end)
        if not text:
            return
        n = copy_to_clipboard(text)
        if n:
            self._toast = f"copied {n} chars to clipboard"

    # One row per notch. Three was chosen for a notched wheel and is wrong for
    # everything else: a trackpad sends a burst of reports per swipe, so three
    # rows each threw the cursor most of a screen for a gesture that meant
    # "down a bit".
    WHEEL = 1

    def _on_mouse(self, ev: MouseEvent) -> None:
        if ev.kind == "move":
            self._hover(ev)
            return
        if ev.kind in ("wheel-up", "wheel-down"):
            delta = -self.WHEEL if ev.kind == "wheel-up" else self.WHEEL
            # The list gets the wheel first and hands it back at its own top,
            # so one gesture runs the list and then keeps going into the
            # transcript above it. Scrolling a list that could not move used
            # to reveal the transcript instead, which reads as the list having
            # no more rows when it has nine hundred.
            #
            # Only while the view is actually on screen, though -- `self.scroll`
            # already above zero means the frame has climbed past it into the
            # transcript, and the view is not what the pointer is over any
            # more. Handing wheel-down to it there moved its cursor instead of
            # the frame, invisibly, and only gave the frame back once that
            # cursor had walked every row down to the list's own last one --
            # on a `browse` of the whole bank, hundreds of notches to scroll
            # back down one screen.
            if (self.scroll == 0 and self.view is not None
                    and self.view.scroll_by(delta)):
                return
            self._scroll_by(-delta)
            return
        if ev.kind == "press":
            # Any new press retires the previous selection's highlight and
            # toast; whether this becomes a click or a drag is decided by
            # what arrives next.
            self._sel_start = self._sel_end = self._abs_pos(ev.row, ev.col)
            self._selecting = False
            self._toast = None
            return
        if ev.kind == "drag":
            if self._sel_start is None:
                self._sel_start = self._abs_pos(ev.row, ev.col)
            self._sel_end = self._abs_pos(ev.row, ev.col)
            self._selecting = True
            return
        if ev.kind != "release":
            return
        if self._selecting:
            self._sel_end = self._abs_pos(ev.row, ev.col)
            self._copy_selection()
            self._selecting = False
            return
        # No drag happened: a plain click, same as before drag-select existed.
        self._sel_start = self._sel_end = None
        if self.view is None:
            return
        idx = self._row_at(ev.row)
        if idx is not None:
            self.view.click_at(idx, max(0, ev.col - self.GUTTER), self)

    def _row_at(self, row: int) -> int | None:
        """Which of the view's rows a screen row is, or None for anywhere else.

        The pinned header is nobody's row. Clamping into the body the way
        this used to made every row of the banner an alias for the body's
        top row, so a click on the countdown fired whatever the list happened
        to have up there -- on a full screen with a long list, a real row.
        `_abs_pos` already refuses to fold the header into the body for the
        same reason.
        """
        if row < self._header_rows:
            return None
        return self._view_rows.get(self._screen_top + row - self._header_rows)

    def _hover(self, ev: MouseEvent) -> None:
        """Light the row the pointer is over, and nothing else.

        Motion reporting fires on every pixel, so the one rule here is that a
        move which resolves to the row that was already lit costs nothing: it
        does not repaint, which is what keeps a mouse crossing the window off
        the CPU.
        """
        if self.view is None:
            self._repaint = False
            return
        idx = self._row_at(ev.row)
        if not self.view.hover_at(idx, max(0, ev.col - self.GUTTER)):
            self._repaint = False

    def _read_line(self, label: str) -> str:
        """One line of input. Shared by the shell prompt and `prompt()`."""
        prev_label, self._label = self._label, label
        try:
            while True:
                if self._repaint:
                    self.paint()
                self._repaint = True
                ev = self.reader.read(0.1 if self.busy else 0.2)
                if ev is None:
                    if self._resized:
                        self._resized = False
                        self.screen.invalidate()
                        ui.WIDTH_OVERRIDE = self.width
                        self._clear_selection()
                    elif self.busy:
                        self.tick += 1
                    else:
                        # Nothing changed, so do not repaint -- which is what
                        # this branch always claimed and never did: it fell
                        # through to the top of the loop, which painted
                        # unconditionally. Five frames a second of composing
                        # a screen nobody was looking at.
                        self._repaint = False
                    continue
                if isinstance(ev, MouseEvent):
                    self._on_mouse(ev)
                    continue
                self._toast = None
                if ev.name == "ctrl-c":
                    if self.editor.buf:
                        self.editor.clear()
                        continue
                    raise KeyboardInterrupt
                if ev.name == "ctrl-d" and not self.editor.buf:
                    raise EOFError
                if self.editor.menu and self._menu_key(ev):
                    continue
                if self._dispatch_nav(ev):
                    continue
                if ev.name == "esc":
                    if self._sel_start is not None:
                        self._clear_selection()
                    elif self.scroll:
                        self.scroll = 0
                    elif self._in_command and self.view is not None:
                        # This isn't the top-level "waiting for a command"
                        # read -- it's a command like `show` blocked on its
                        # own `input()` call for the next n/p/d/e/t, with a
                        # card drawn alongside it. Detaching and looping back
                        # here left the card gone but that same `input()`
                        # still open, so whatever you typed next -- a real
                        # command -- was consumed as its answer instead of
                        # ever reaching the dispatcher. Escape now closes the
                        # prompt the same way a blank Enter does: it answers
                        # with nothing, which is what sends `show` back to
                        # the real shell.
                        self.detach()
                        self.editor.clear()
                        return ""
                    elif self._in_command:
                        # No view either -- this is `drill` or `mock` blocked
                        # on "your answer" or the rating prompt, with nothing
                        # on screen to fall back to. Every one of those
                        # prompts already treats ^D as "quit and save the
                        # rest of the sitting"; esc raising the same
                        # `EOFError` reuses that path instead of teaching
                        # each command a second way to be told to stop.
                        raise EOFError
                    elif not self._esc_back():
                        self.detach()
                    continue
                if self.view is not None and self._view_claims(ev):
                    if self.view.handle(ev, self):
                        if self._injected is not None:
                            line, self._injected = self._injected, None
                            self._from_view = True
                            return line
                        continue
                line = self.editor.handle(ev)
                if line is not None:
                    return line
        finally:
            self._label = prev_label

    def _menu_key(self, key: Key) -> bool:
        """While suggestions are up they get the arrows, Tab, Enter and Esc.

        Nothing else is reachable from those four keys at that moment anyway,
        and having Enter run a half-typed command instead of taking the
        highlighted one is the single most annoying way to get this wrong.
        """
        n = key.name
        if n in ("up", "ctrl-p"):
            self.editor.menu_move(-1)
        elif n in ("down", "ctrl-n"):
            self.editor.menu_move(1)
        elif n in ("tab", "enter", "right"):
            self.editor.accept()
        elif n == "esc":
            self.editor.close_menu()
            self.editor.menu_dismissed = True
        else:
            return False
        return True

    _VIEW_KEYS = {"up", "down", "left", "right", "home", "end", "enter",
                  "pgup", "pgdn",
                  "btab", "alt-g", "alt-a", "alt-d", "alt-r",
                  # browse: narrow, drop a filter, mock the set, re-tag a row.
                  # All alt-modified because a bare letter belongs to the input
                  # line -- `d` has to start the word `drill`, not drill.
                  "alt-n", "alt-x", "alt-m", "alt-t"}

    def _view_claims(self, key: Key) -> bool:
        """A view never steals a printable character.

        Typing `d` has to start the word `drill` even while a result list is
        on screen, so the view only sees navigation keys, and only while the
        input line is empty. The one deliberate exception is a bare space:
        no command starts with a leading space, so `browse` can bind it as a
        multi-select toggle without weakening the rule that actually matters
        here -- letters stay the input line's, unconditionally.
        """
        is_space = key.name == "char" and key.ch == " "
        if key.name not in self._VIEW_KEYS and not is_space:
            return False
        if key.name in ("btab", "alt-g", "alt-a"):
            return True
        return not self.editor.buf

    def prompt(self, text: str = "") -> str:
        """`input()`, but drawn in the pinned box. Same exceptions, on purpose.

        Suggestions are switched off for the duration: a command asking a
        question wants an answer, not a list of commands, and typing "d" at
        the start of a spoken answer should not offer to run `drill`.
        """
        label = _clean_label(str(text))
        if label:
            self.emit(faint("  " + label))
        saved = self.editor.completer
        self.editor.completer = None
        self.editor.close_menu()
        try:
            line = self._read_line(self.prompt_label)
        finally:
            self.editor.completer = saved
        self.transcript.append(paint("  " + (line or ""), "sky"))
        return line

    def confirm(self, text: str) -> bool:
        return self.prompt(text + " [y/N]").strip().lower().startswith("y")

    # ------------------------------------------------------------ jobs

    def run_job(self, label: str, fn):
        """Run a blocking call on a worker thread while the frame keeps moving.

        This exists because a graded drill answer is a network round trip. On
        the main thread it froze the whole shell -- no spinner, no elapsed
        time, no way out -- and a rate-limited provider turned that into
        minutes of a screen that looked crashed.

        The worker must not touch the database; only the network calls in
        llm.py are routed through here, and those hold no connection.
        """
        box: dict = {}

        def work() -> None:
            try:
                box["value"] = fn()
            except BaseException as exc:      # noqa: BLE001 - re-raised below
                box["error"] = exc

        thread = threading.Thread(target=work, daemon=True, name="superday-job")
        started = time.monotonic()
        prev_busy = self.busy
        thread.start()
        try:
            while thread.is_alive():
                waited = time.monotonic() - started
                self.busy = (label + faint(f"   {waited:.0f}s")
                             + faint("   esc to give up"))
                self.tick += 1
                self.paint()
                ev = self.reader.read(0.09)
                if isinstance(ev, Key) and ev.name in ("esc", "ctrl-c"):
                    raise KeyboardInterrupt
                if isinstance(ev, MouseEvent) and ev.kind.startswith("wheel"):
                    # Scrolling while you wait is fine; clicking a row is not,
                    # because the command it would launch cannot start until
                    # this one finishes.
                    self._on_mouse(ev)
        finally:
            self.busy = prev_busy
        if "error" in box:
            raise box["error"]
        return box.get("value")

    # ------------------------------------------------------------ run

    def run(self) -> int:
        fd = sys.__stdin__.fileno()
        prev_winch = None
        try:
            prev_winch = signal.signal(signal.SIGWINCH, self._winch)
        except (ValueError, AttributeError, OSError):
            pass
        # Mouse reporting makes rows clickable but takes over drag-select in
        # most terminals (hold alt to select anyway). SUPERDAY_NO_MOUSE hands
        # it back for anyone who would rather have the selection.
        self.screen.begin(mouse=not os.environ.get("SUPERDAY_NO_MOUSE"))
        self.running = True
        old_stdout, old_stderr = sys.stdout, sys.stderr
        old_input = builtins.input
        old_width = ui.WIDTH_OVERRIDE
        old_runner = llm.RUNNER
        global CURRENT
        prev_shell, CURRENT = CURRENT, self
        try:
            with raw_mode(fd):
                sys.stdout = _TranscriptOut(self)
                sys.stderr = _TranscriptOut(self, style="bad")
                builtins.input = self.prompt
                llm.RUNNER = self.run_job
                ui.WIDTH_OVERRIDE = self.width
                if self.on_ready:
                    self.on_ready(self)
                while self.running:
                    ui.WIDTH_OVERRIDE = self.width
                    try:
                        line = self._read_line(self.prompt_label)
                    except KeyboardInterrupt:
                        self.emit("")
                        self.emit(faint("  ^C   type exit to leave"))
                        continue
                    except EOFError:
                        break
                    from_view, self._from_view = self._from_view, False
                    if not line.strip():
                        continue
                    # A line a view injected is not something you typed, and
                    # `drill --ids` off a browse carries four kilobytes of
                    # comma-separated ids. In the history that is one ↑ away
                    # from filling the box with numbers, and it gets written
                    # to the history file on the way out.
                    if not from_view:
                        self.editor.remember(self.redact(line))
                    self._run_one(line, from_view)
                    self._after_run(from_view)
        finally:
            CURRENT = prev_shell
            sys.stdout, sys.stderr = old_stdout, old_stderr
            builtins.input = old_input
            llm.RUNNER = old_runner
            ui.WIDTH_OVERRIDE = old_width
            self.screen.end()
            if prev_winch is not None:
                with contextlib.suppress(Exception):
                    signal.signal(signal.SIGWINCH, prev_winch)
        return 0

    def _run_one(self, line: str, from_view: bool = False) -> None:
        self._in_command = True
        if not from_view:
            # A line you typed yourself is a fresh place in the tool, not a
            # continuation of whatever screen happened to be up. It used to
            # leave that screen attached: a plain-print command like
            # `settings` never called `attach`, so its output landed
            # underneath a still-live `browse` instead of replacing it, and
            # the two rendered on top of each other. Putting the old view
            # down first, before this line's echo even prints, is what keeps
            # "one tab open at a time" true for every command, not just the
            # ones that happen to open a view of their own -- `attach` still
            # calls `detach` too, so a command that does open one is unaffected.
            self.detach()
        was_up = self.view
        echo_mark = self.mark()
        # Live for exactly as long as this command runs, including any nested
        # `input()` prompts inside it -- `show`'s n/p/d/e/t loop reads this to
        # know where its own echo sits, so it can fold the whole visit away
        # on a clean exit the same way `browse` and `dashboard` already do.
        self._cmd_echo_mark = echo_mark
        self.emit("")
        # The echo is one line. `drill --ids` off a browse of the whole bank
        # carries four kilobytes of comma-separated ids, and printing that in
        # full buried the drill under twenty-five lines of numbers.
        self.emit(paint("› ", "accent_dim")
                  + ui.truncate(ui.paint(self.redact(line), "sky", ui.BOLD),
                                self.width - 4))
        echo_end = self.transcript.mark()
        try:
            self.on_submit(self, line)
        except KeyboardInterrupt:
            self.emit(faint("  cancelled"))
        except SystemExit:
            pass
        except Exception as e:
            # A command blowing up must not take the shell down with it, and
            # the transcript is not the place for a traceback -- IB_DEBUG is.
            self.emit(ui.bad(f"  {line.split()[0]} failed")
                      + faint(f" - {type(e).__name__}: {e}"))
            if os.environ.get("IB_DEBUG"):
                import traceback
                for tb_line in traceback.format_exc().rstrip().split("\n"):
                    self.emit(faint("  " + tb_line))
            else:
                self.emit(faint("  IB_DEBUG=1 for the traceback"))
        finally:
            self._in_command = False
            self.busy = ""
            with contextlib.suppress(Exception):
                sys.stdout.flush()
            self.scroll = 0
            # A view this command put up owns this echo: it goes down with
            # the view in `detach`, not before. While the view is up the echo
            # still shows above it, same as always.
            #
            # Only a view this command put up, though. A command that printed
            # its output underneath a list someone else opened has not earned
            # that list's echo, and claiming it meant `detach` folding away
            # the wrong command's echo -- and, with it, every line printed
            # since.
            if self.view is None:
                self._view_echo = None
            elif self.view is not was_up:
                self._view_echo = (echo_mark, echo_end)
            self._cmd_echo_mark = None

    def _winch(self, *_a) -> None:
        self._resized = True
        self.screen.invalidate()

    def _after_run(self, from_view: bool) -> None:
        """What a finished command means for the view that was on screen.

        Two cases, and they are not the same case. A command a view ran that
        reports back and attaches nothing (`run_now("drill --ids ...")`)
        gets its parked view back untouched. One that opens a view of its own
        (`dupes` opening a compare, `find` opening `show`) is a drill-down,
        not a swap, so the view it was launched from is kept rather than
        dropped -- that is what makes esc a back button instead of a full
        exit. A line you typed yourself, rather than one a view ran on your
        behalf, is a fresh place in the tool: `_run_one` has already put
        down whatever was open before running it, so there is nothing parked
        to restore and no back-history esc should still be able to walk to.
        """
        if self._resume_view is not None:
            if self.view is None:
                self._resume_view.on_resume()
                self.attach(self._resume_view)
            elif self.view is not self._resume_view:
                self._view_stack.append(self._resume_view)
            self._resume_view = None
        elif not from_view:
            self._view_stack.clear()

    def _esc_back(self) -> bool:
        """Pop to the view esc came from, if there is one. False if not.

        `esc` used to mean only "close whatever is open" -- fine for a list
        opened directly, wrong for one reached by drilling into another:
        closing the compare a `dupes` row opened lost the list it was opened
        from as well, dumping you back at a bare prompt. A view reached that
        way is parked rather than dropped, and this is what hands it back.
        """
        if not self._view_stack:
            return False
        prev = self._view_stack.pop()
        prev.on_resume()
        self.attach(prev)
        return True

    def run_now(self, line: str) -> None:
        """Let a view run a command as if you had typed it.

        The view is mid-keystroke when it asks, so the line is stashed and the
        input loop returns it instead of recursing into the dispatcher from
        inside a key handler.

        The view also steps off the screen for the duration. It used to stay
        attached, which meant a drill launched from `browse` asked its
        question into the transcript *behind* the frame: the list was still
        drawn, the question was not, and every keystroke went to a sitting you
        could not see. The view comes back afterwards unless the command
        attached one of its own.
        """
        self._injected = line
        self._resume_view = self.view
        self.detach()

    def prefill(self, line: str) -> None:
        """Put a half-written command in the box and leave the cursor at the end.

        The difference from `run_now` matters: this is for commands the view
        can only start, not finish. `tag 774 ` needs tag names that only you
        know, so the view types the part it is sure about and hands you the
        cursor rather than guessing the rest.
        """
        self.editor.buf = line
        self.editor.pos = len(line)
        self.editor.close_menu()
        self.editor.menu_dismissed = False

    def stop(self) -> None:
        self.running = False


# A prompt like "  [n] next \u00b7 [Enter] done  > " arrives with its styling
# still attached, so the trailing chevron sits behind a reset code and a plain
# rstrip cannot see it.
_LABEL_TRIM = re.compile(r"(?:\x1b\[[0-9;]*m|[\s>\u00b7])+$")


def _clean_label(text: str) -> str:
    """`input()` prompts in this codebase end in ' > '. The box draws its own."""
    trimmed = _LABEL_TRIM.sub("", text.strip())
    return trimmed + RESET if "\x1b" in trimmed else trimmed


# The shell a command is running inside, if any. A `cmd_*` uses this to hand
# back a navigable view instead of printing a list; when it is None -- one-shot
# invocation, a pipe, a test -- the same command prints and nothing changes.
CURRENT: "Shell | None" = None


def active() -> bool:
    return CURRENT is not None


def attach(view: View) -> bool:
    if CURRENT is None:
        return False
    CURRENT.attach(view)
    return True


def mark() -> int | None:
    """A point in the transcript to fold back to later, or None with no shell."""
    return None if CURRENT is None else CURRENT.mark()


def echo_mark() -> int | None:
    """Where the currently running command's own `›  ...` echo started.

    For a command like `show` that loops on its own nested `input()` calls
    behind a view: if nothing else has landed in the transcript since, the
    whole visit can fold away on a clean exit, echo included, the same way
    `browse` and `dashboard` already do when their view is put down.
    """
    return None if CURRENT is None else CURRENT._cmd_echo_mark


def collapse(mark: int | None, lines: list[str]) -> bool:
    """Fold everything printed since `mark` down to `lines`.

    False when there is no shell to fold: a pipe and the line-at-a-time REPL
    have already sent those bytes to a terminal that cannot take them back, so
    the caller keeps whatever it printed and nothing behaves differently.
    """
    if CURRENT is None or mark is None:
        return False
    CURRENT.collapse(mark, lines)
    return True


def repaint() -> bool:
    """Throw away the frame diff so the next render redraws every row.

    False when there is no shell, which is not a failure: outside the
    full-screen shell there is no retained frame to be out of date, and the
    next line printed already uses the new palette.
    """
    if CURRENT is None:
        return False
    CURRENT.screen.invalidate()
    CURRENT.paint()
    return True


def dismiss() -> None:
    """Put down whatever view is up, if there is a shell at all.

    For a command that drew its own screen and is about to run something
    else: the new thing needs the terminal, and a frame left drawn over the
    bottom of it is a frame the next question gets asked behind.
    """
    if CURRENT is not None:
        CURRENT.detach()


def available() -> bool:
    """Whether we can take the terminal at all."""
    if os.environ.get("SUPERDAY_NO_TUI"):
        return False
    try:
        return (sys.__stdin__ is not None and sys.__stdout__ is not None
                and sys.__stdin__.isatty() and sys.__stdout__.isatty()
                and os.environ.get("TERM", "") not in ("", "dumb"))
    except Exception:
        return False
