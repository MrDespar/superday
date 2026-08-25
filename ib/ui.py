"""Terminal styling shared by the CLI and the mock interview."""
from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
import unicodedata

from . import config
from . import theme as theme_mod

W = 78

# Set by the full-screen shell to the width of its frame. Every render path
# asks width() for its column budget, so without this the panels inside the
# shell would be measured against the raw terminal and overflow the border.
WIDTH_OVERRIDE: int | None = None


def width() -> int:
    if WIDTH_OVERRIDE:
        return max(20, WIDTH_OVERRIDE)
    try:
        cols = shutil.get_terminal_size((W, 20)).columns
        return max(70, min(cols - 2, 96))
    except Exception:
        return W

BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"
UNDER = "\033[4m"
REVERSE = "\033[7m"
UNREVERSE = "\033[27m"       # turns reverse video back off without a full reset
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


# ---------------------------------------------------------------- palette
#
# One named palette, resolved once against what the terminal can actually
# draw. Everything visual in the tool picks from these names rather than
# reaching for a raw SGR code, which is what keeps the shell, the drill view
# and the dashboard looking like one program instead of three.
#
# The values come from `ib/theme.py` rather than living here, because a
# palette is only legible relative to the ground it is drawn on and the theme
# is what carries both. `settings theme` is the one thing that picks; the
# 256-colour stand-in for each token is computed from its hex rather than
# written down beside it. Terminals that advertise neither depth fall back to
# the eight basics above, which still reads -- it just loses the softness.

# The ansi-16 last resort, by token. Only these eight exist at depth 1, so
# several tokens necessarily share one and the quiet end gives up and uses
# the terminal's own default foreground: an eight-colour terminal has no
# grey to be quiet in, and picking the nearest of eight for `faint` renders
# chrome in a colour louder than the text it sits under.
_BASIC = {
    "accent": CYAN, "accent_dim": CYAN, "mauve": "\033[35m", "sky": CYAN,
    "mint": CYAN, "gold": YELLOW, "coral": RED,
    "text": "", "muted": DIM, "faint": DIM, "line": DIM, "hover": "",
    "ground": "",
}

_THEME: theme_mod.Theme | None = None
_PALETTE: dict[str, tuple[str, int, str]] = {}


def _build(t: theme_mod.Theme) -> dict[str, tuple[str, int, str]]:
    pal = dict(t.tokens())
    pal["hover"] = t.hover
    # `ground` is the theme's own background as a *foreground* colour, which
    # is what a chip needs: text knocked out of a filled pill. A theme that
    # inherits the terminal's background has none to knock out of, so it
    # falls back to the end of the ramp its text is not at.
    pal["ground"] = t.bg or ("000000" if t.dark else "ffffff")
    return {k: (v, theme_mod.xterm256(v), _BASIC.get(k, "")) for k, v in pal.items()}


def active() -> theme_mod.Theme:
    """The theme in force, read from config the first time it is asked for.

    Read lazily rather than bound at import for the reason every accessor in
    `llm.py` is: bound as a module constant it would be captured before
    `config.local.json` had ever been opened, and the setting would be dead
    while still reporting itself as set.
    """
    global _THEME, _PALETTE
    if _THEME is None:
        try:
            name = config.load().get("theme")
        except Exception:
            name = None
        _THEME = theme_mod.get(name)
        _PALETTE = _build(_THEME)
    return _THEME


def set_theme(name: str) -> theme_mod.Theme:
    """Switch theme in place. The shell repaints; one-shot output just changes."""
    global _THEME, _PALETTE
    _THEME = theme_mod.get(name)
    _PALETTE = _build(_THEME)
    return _THEME


def background() -> str | None:
    """The hex the shell paints its frame on, or None to inherit the terminal's.

    None is what the tool used to do unconditionally, and on any terminal
    running transparency it meant the palette was drawn on a backdrop it had
    never been measured against.
    """
    return active().bg


def _depth() -> int:
    """3 = 24-bit, 2 = 256 colour, 1 = the eight basics, 0 = no colour."""
    if not colors_enabled():
        return 0
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return 3
    term = os.environ.get("TERM", "")
    if "256" in term or "direct" in term:
        return 2
    if term in ("", "dumb"):
        return 0
    return 1


_DEPTH: int | None = None


def depth() -> int:
    global _DEPTH
    if _DEPTH is None:
        _DEPTH = _depth()
    return _DEPTH


def reset_depth() -> None:
    """Forget the cached probe. Only the tests need this."""
    global _DEPTH
    _DEPTH = None


def colour(name: str, *, bg: bool = False) -> str:
    """The SGR prefix for a palette name, at whatever depth we have."""
    active()
    entry = _PALETTE.get(name)
    if entry is None:
        return ""
    d = depth()
    if d == 0:
        return ""
    hexv, c256, basic = entry
    if d == 3:
        r, g, b = (int(hexv[i:i + 2], 16) for i in (0, 2, 4))
        return f"\033[{48 if bg else 38};2;{r};{g};{b}m"
    if d == 2:
        return f"\033[{48 if bg else 38};5;{c256}m"
    if bg:
        return ""
    return basic


def paint(text: str, name: str, *codes: str) -> str:
    """Colour by palette name, with optional extra attributes.

    Checks the depth rather than only the colour: at depth 0 the palette
    resolves to nothing but the attribute codes would still be emitted, which
    is how bold ended up in piped output.
    """
    if depth() == 0:
        return text
    c = colour(name)
    if not c and not codes:
        return text
    return "".join(codes) + c + text + RESET

def colors_enabled() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def style(text: str, *codes: str) -> str:
    if not codes or not colors_enabled():
        return text
    return "".join(codes) + text + RESET


def head(text: str) -> str:
    return paint(text, "accent", BOLD)


def ok(text: str) -> str:
    return paint(text, "mint")


def bad(text: str) -> str:
    return paint(text, "coral")


def warn(text: str) -> str:
    return paint(text, "gold")


# Neither of these passes DIM any more, and that is the whole of a bug that
# had every quiet thing on screen at roughly half the contrast it was designed
# for. The palette hex *is* the darkening -- `muted` and `faint` are picked
# against the theme's own background to land on a floor `theme.FLOORS` names.
# Passing SGR 2 on top asks the terminal to darken an already-darkened colour
# a second time, and a terminal implements that as a blend toward the
# background: on Ghostty it halves what is left. The attribute is a leftover
# from before the palette existed, when `dim()` was `style(text, DIM)` and the
# attribute was the entire effect; when the hex arrived it should have come
# off with it. It survived because it is invisible in a diff and because at
# depth 1 -- where there is no hex -- it is still the only thing there is.


def dim(text: str) -> str:
    """Information you read, one step under the body: labels, meta, counts."""
    return paint(text, "muted") if depth() >= 2 else style(text, DIM)


def faint(text: str) -> str:
    """Quieter than dim: chrome the eye should slide over."""
    return paint(text, "faint") if depth() >= 2 else style(text, DIM)


def accent(text: str) -> str:
    return paint(text, "accent")


def sky(text: str) -> str:
    return paint(text, "sky")


def mauve(text: str) -> str:
    return paint(text, "mauve")


_VERDICT_PALETTE = {
    "strong": "mint", "good": "mint", "kept": "mint",
    "adequate": "gold", "hard": "gold", "fixed": "gold", "weak": "gold",
    "again": "coral", "wrong": "coral", "rejected": "coral", "reject": "coral",
}


def verdict(text: str) -> str:
    name = _VERDICT_PALETTE.get(text.lower())
    return paint(text, name) if name else text


def rule(ch: str = "─") -> str:
    return dim(ch * width())


def section(title: str, ch: str = "─") -> str:
    w = max(10, width() - len(title) - 4)
    return f"\n  {head(title)} {dim(ch * w)}"


def wrap(text: str, indent: str = "", w: int | None = None) -> str:
    w = w or width()
    out = []
    for para in (text or "").split("\n"):
        out.append(textwrap.fill(para, w, initial_indent=indent,
                                 subsequent_indent=indent) if para.strip() else "")
    return "\n".join(out)


# ---------------------------------------------------------------- prose

_BULLET = re.compile(r"^\s*(?:[-*\u2022\u00b7]|\(?\d{1,2}[.)]|[a-z][.)])\s+")
_LABEL = re.compile(r"^\s*[A-Z][A-Za-z /&'-]{2,38}:\s*$")


PROSE_MAX = 108


def body(text: str, indent: str = "  ", w: int | None = None) -> str:
    """Render an answer or explanation as readable prose rather than one blob.

    Extraction leaves answers as a soup of hard-wrapped lines, inline dashes and
    stray numbering: textwrap.fill runs it all together, and a plain print keeps
    whatever column the source PDF happened to wrap at. This reflows to the
    terminal width, keeps list structure, and hangs a bullet's continuation
    lines under its text instead of under its dash.
    """
    if not text or not text.strip():
        return dim(f"{indent}(nothing on file)")

    # A measure longer than about a hundred columns stops being readable, so a
    # very wide terminal gets whitespace rather than a wider paragraph.
    w = min(w or width(), PROSE_MAX)
    out: list[str] = []
    para: list[str] = []
    marker: str | None = None   # set while we are inside a bullet

    def flush() -> None:
        nonlocal marker
        if para:
            if marker is None:
                out.append(wrap(" ".join(para), indent, w))
            else:
                pad = indent + " " * (len(marker) + 1)
                out.append(textwrap.fill(" ".join(para), w,
                                         initial_indent=f"{indent}{marker} ",
                                         subsequent_indent=pad))
        para.clear()
        marker = None

    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            flush()
            if out and out[-1] != "":
                out.append("")
            continue
        if _LABEL.match(raw.rstrip()):
            flush()
            out.append(f"{indent}{head(line)}")
            continue
        m = _BULLET.match(raw)
        if m:
            flush()
            marker = m.group(0).strip()
            para.append(raw[m.end():].strip())
            continue
        # Anything else continues whatever came before, bullet or paragraph.
        para.append(line)
    flush()

    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def question(text: str, indent: str = "") -> str:
    """The question itself, set apart from the metadata around it."""
    return style(wrap((text or "").strip(), indent), BOLD)


def kv(label: str, value: str, width: int = 12) -> str:
    return f"  {dim(label.ljust(width))} {value}"


def progress(current: int, total: int, width: int = 10) -> str:
    """Render a compact progress indicator: [████░░░░] 4/10

    Drawn with `meter`'s glyphs rather than a second set of its own. This is
    the bar in the drill header -- the most-looked-at line in the tool -- and
    it was the only one of fifteen bar sites spelling itself `[===-------]`
    while the sitting-complete card two screens later, the dashboard, the tag
    list and the mock scorecard all used blocks. It is not colour-coded by
    fraction the way `meter` is: how far through a sitting you are is not
    good news or bad news, and painting it red at the start said it was.
    """
    if total <= 0:
        return ""
    frac = max(0.0, min(1.0, current / total))
    filled = round(frac * width)
    return f"[{accent('█' * filled)}{faint('░' * (width - filled))}] {current}/{total}"


def badge(text: str, color_fn=ok) -> str:
    """Render a styled badge/pill: [ ACCOUNTING ]"""
    return color_fn(f"[{text.upper()}]")


# ---------------------------------------------------------------- measurement

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def cells(ch: str) -> int:
    """How many columns one character occupies.

    Wide and Fullwidth take two, combining marks take none, everything else
    takes one. Ambiguous is treated as one, which is what a terminal running
    a Latin font does and covers every box-drawing glyph in `ui.SQUARE`.
    """
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def vlen(s: str) -> int:
    """Visible width: what the terminal draws, not what len() counts.

    Escape bytes do not count, and neither does the assumption that one
    character is one column. This used to be a bare `len()` on the stripped
    string, which is right for Latin text and wrong by one column per
    character for CJK and emoji -- and every boxed layout in the tool is
    measured through here, so being wrong here tears the frame two panels away
    from the mistake.

    The ASCII fast path is not an optimisation detail: this is called for
    every cell of every frame, and the per-character walk below is about
    twenty times slower.
    """
    plain = _ANSI_RE.sub("", s or "")
    if plain.isascii():
        return len(plain)
    return sum(cells(ch) for ch in plain)


def strip(s: str) -> str:
    """Plain text with every escape sequence removed."""
    return _ANSI_RE.sub("", s or "")


def highlight_range(s: str, start: int, end: int) -> str:
    """Reverse-video the visible columns in [start, end), leaving every other
    style on the line untouched.

    Used for the drag-select highlight in the TUI: toggling SGR 7/27 instead
    of resetting means the line's own colour survives on both sides of the
    selection.
    """
    if end <= start:
        return s
    out: list[str] = []
    col = 0
    i = 0
    in_hl = False
    while i < len(s):
        m = _ANSI_RE.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        want = start <= col < end
        if want and not in_hl:
            out.append(REVERSE)
            in_hl = True
        elif not want and in_hl:
            out.append(UNREVERSE)
            in_hl = False
        out.append(s[i])
        col += 1
        i += 1
    if in_hl:
        out.append(UNREVERSE)
    return "".join(out)


def wash(s: str, name: str, width: int) -> str:
    """A background colour behind a whole row, surviving the row's own resets.

    Every helper in here ends its output with RESET, which clears background
    as well as foreground, so a background set once at the front of a styled
    line survives exactly as far as the first coloured word. Re-emitting it
    after every escape sequence is what makes the wash continuous, and padding
    to `width` is what stops it stopping halfway across the row.

    Terminals with no background colour to give get the line back untouched:
    no highlight is better than a highlight drawn as a colour change nobody
    asked for.
    """
    bg = colour(name, bg=True)
    if not bg:
        return s
    out = [bg]
    i = 0
    while i < len(s):
        m = _ANSI_RE.match(s, i)
        if m:
            out.append(m.group(0))
            out.append(bg)
            i = m.end()
            continue
        out.append(s[i])
        i += 1
    out.append(" " * max(0, width - vlen(s)))
    out.append(RESET)
    return "".join(out)


def ground(s: str, width: int) -> str:
    """Lay a line on the theme's own background.

    The same problem `wash` solves, one layer out and with one difference that
    matters: this re-emits the backdrop after a **reset** rather than after
    every escape sequence. A hovered row arrives here already washed, and
    `wash` re-emits its own background straight after each reset -- so
    inserting the backdrop at the same point puts it *before* the hover's,
    and the nearer one wins. Re-emitting after every escape instead would
    paint the backdrop over the hover it is supposed to sit under.

    Returns the line untouched when the theme has no background of its own,
    which is how a theme opts back into the terminal's.
    """
    bg = colour("ground", bg=True)
    if not bg:
        return s
    out = [bg]
    i = 0
    while i < len(s):
        m = _ANSI_RE.match(s, i)
        if m:
            out.append(m.group(0))
            if m.group(0) in ("\033[0m", "\033[m"):
                out.append(bg)
            i = m.end()
            continue
        out.append(s[i])
        i += 1
    out.append(" " * max(0, width - vlen(s)))
    out.append(RESET)
    return "".join(out)


def pad(s: str, n: int) -> str:
    """ljust that counts visible characters, not escape bytes."""
    return s + " " * max(0, n - vlen(s))


def truncate(s: str, n: int, ellipsis: str = "\u2026") -> str:
    """Cut to n visible columns, keeping escape sequences balanced.

    Styled text is cut mid-sequence by a naive slice, which leaks colour into
    the rest of the line and, worse, makes the line wider than it looks. Every
    boxed layout depends on this being exact.
    """
    if vlen(s) <= n:
        return s
    if n <= 0:
        return ""
    keep = max(0, n - vlen(ellipsis))
    out: list[str] = []
    seen = 0
    i = 0
    while i < len(s) and seen < keep:
        m = _ANSI_RE.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        # Checked before appending, not after. A two-column character taken
        # because there was one column left is a character that overruns the
        # box by one -- which is exactly the tear this function exists to
        # prevent, just one cell wide instead of a whole style run.
        w = cells(s[i])
        if seen + w > keep:
            break
        out.append(s[i])
        seen += w
        i += 1
    cut = "".join(out)
    # A cut that lands inside a styled run leaves the colour open; close it so
    # the style cannot bleed into the border drawn after it.
    if _ANSI_RE.search(cut) and not cut.endswith(RESET):
        cut += RESET
    return cut + ellipsis


def columns(left: list[str], right: list[str], gap: int = 4,
            left_w: int | None = None) -> list[str]:
    """Two stacked lists rendered side by side, ANSI-aware."""
    left_w = left_w if left_w is not None else max([vlen(x) for x in left] or [0])
    out = []
    for i in range(max(len(left), len(right))):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        out.append(pad(truncate(l, left_w), left_w) + " " * gap + r)
    return out


# ---------------------------------------------------------------- window / cards

def window(title: str, lines: list[str], footer: str | None = None,
           max_w: int | None = None) -> str:
    """Content inside a box that is actually rectangular.

    The right border has to land in the same column on every row, so each row
    is padded *and* truncated against the same inner width, measured with vlen.
    A row that is one styled character too long used to push the border out and
    ruin the whole frame.
    """
    w = max(60, min(max_w or width(), width()))
    inner_w = w - 6                       # border + 2 spaces of padding, both sides

    def row(content: str) -> str:
        return "\u2502  " + pad(truncate(content, inner_w), inner_w) + "  \u2502"

    bar_len = max(0, w - vlen(title) - 5)
    out = [f"\u256d\u2500 {head(title)} " + dim("\u2500" * bar_len) + "\u256e"]
    out.extend(row(line) for line in lines)
    if footer:
        out.append("\u251c" + dim("\u2500" * (w - 2)) + "\u2524")
        out.append(row(footer))
    out.append("\u2570" + dim("\u2500" * (w - 2)) + "\u256f")
    return "\n".join(out)


_SPARK = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def sparkline(values: list[float], color_fn=None) -> str:
    """Compact trend glyphs. Flat data draws a flat line, not noise."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo
    out = []
    for v in values:
        idx = 0 if span <= 0 else min(len(_SPARK) - 1, int((v - lo) / span * (len(_SPARK) - 1)))
        out.append(_SPARK[idx])
    s = "".join(out)
    return color_fn(s) if color_fn else s


_HEAT = "\u2591\u2592\u2593\u2588"


def heat(frac: float | None) -> str:
    """One cell of a heatmap.

    None means never drilled, which is a different thing from drilled and
    failed -- so it gets its own glyph rather than the darkest shade.
    """
    if frac is None:
        return dim("\u00b7")
    frac = max(0.0, min(1.0, frac))
    ch = _HEAT[min(len(_HEAT) - 1, int(frac * len(_HEAT)))]
    color = ok if frac >= 0.66 else warn if frac >= 0.33 else bad
    return color(ch)


def meter(frac: float | None, width_: int = 14, empty: str = "\u00b7") -> str:
    """A filled bar using block glyphs, so it reads at a glance."""
    if frac is None:
        return dim(empty * width_)
    frac = max(0.0, min(1.0, frac))
    # Any progress at all gets a cell. Rounding 1% down to nothing draws the
    # same bar as 0% and reads as a broken widget rather than an early start.
    filled = max(1, round(frac * width_)) if frac > 0 else 0
    color = ok if frac >= 0.66 else warn if frac >= 0.33 else bad
    head_ = color("\u2588" * filled) if filled else ""
    return head_ + faint("\u2591" * (width_ - filled))


# ---------------------------------------------------------------- chrome
#
# The shapes the full-screen shell draws with. Kept here rather than in
# tui.py so a panel rendered inside the shell and the same panel printed to a
# plain pipe come out of one set of glyphs.

ROUND = {"tl": "\u256d", "tr": "\u256e", "bl": "\u2570", "br": "\u256f",
         "h": "\u2500", "v": "\u2502", "lt": "\u251c", "rt": "\u2524"}
SQUARE = {"tl": "\u250c", "tr": "\u2510", "bl": "\u2514", "br": "\u2518",
          "h": "\u2500", "v": "\u2502", "lt": "\u251c", "rt": "\u2524"}

# Charm's spinner, which is just a braille dot walking a circle. Reads as
# motion at 12fps without redrawing anything but one cell.
SPINNER = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
DOTS = "\u25cf\u25cf\u25cf"


def spinner_frame(tick: int) -> str:
    return accent(SPINNER[tick % len(SPINNER)])


def dot(name: str = "accent") -> str:
    return paint("\u25cf", name)


def chip(text: str, name: str = "accent") -> str:
    """A soft pill. Reverse video where the terminal can do a background,
    bracketed where it cannot -- both stay legible, neither leaks colour."""
    if depth() >= 2:
        # The text sits on the theme's own ground rather than on a hardcoded
        # black. That was the last raw SGR left in a render path, and it was
        # wrong twice: colour 16 is whatever the terminal's palette says it is
        # rather than reliably black, and on a light theme black-on-accent is
        # the one combination that cannot work.
        return colour(name, bg=True) + colour("ground") + f" {text} " + RESET
    return style(f"[{text}]", BOLD)


def gradient(text: str, start: str = "accent", end: str = "mauve") -> str:
    """Blend one palette colour into another across the text.

    Only at 24-bit depth: interpolating into a 256-colour cube produces
    banding that looks like a rendering bug rather than a gradient, so the
    shallower terminals get the flat accent instead.
    """
    if depth() < 3 or not text:
        return paint(text, start, BOLD)
    a = _PALETTE[start][0]
    b = _PALETTE[end][0]
    ar, ag, ab = (int(a[i:i + 2], 16) for i in (0, 2, 4))
    br, bg_, bb = (int(b[i:i + 2], 16) for i in (0, 2, 4))
    n = max(1, len(text) - 1)
    out = [BOLD]
    for i, ch in enumerate(text):
        t = i / n
        r = round(ar + (br - ar) * t)
        g = round(ag + (bg_ - ag) * t)
        bl = round(ab + (bb - ab) * t)
        out.append(f"\033[38;2;{r};{g};{bl}m{ch}")
    out.append(RESET)
    return "".join(out)


def hairline(w: int, name: str = "line") -> str:
    return paint("\u2500" * max(0, w), name)


def panel(lines: list[str], w: int, *, title: str = "", footer: str = "",
          box: dict | None = None, accent_title: bool = True) -> list[str]:
    """A rectangular frame whose right border lands in one column on every row.

    Returns lines rather than a string because the shell composes panels into
    a frame it measures; window() above is the print-to-stdout equivalent and
    stays for the one-shot path.
    """
    b = box or ROUND
    inner = max(4, w - 4)
    out: list[str] = []
    if title:
        t = head(title) if accent_title else title
        fill = max(0, inner - vlen(t) - 1)
        out.append(paint(b["tl"] + b["h"], "line") + " " + t + " "
                   + paint(b["h"] * fill, "line") + paint(b["tr"], "line"))
    else:
        out.append(paint(b["tl"] + b["h"] * (w - 2) + b["tr"], "line"))
    for line in lines:
        out.append(paint(b["v"], "line") + " " + pad(truncate(line, inner), inner)
                   + " " + paint(b["v"], "line"))
    if footer:
        out.append(paint(b["lt"] + b["h"] * (w - 2) + b["rt"], "line"))
        out.append(paint(b["v"], "line") + " " + pad(truncate(footer, inner), inner)
                   + " " + paint(b["v"], "line"))
    out.append(paint(b["bl"] + b["h"] * (w - 2) + b["br"], "line"))
    return out


def hard_wrap(text: str, w: int) -> list[str]:
    """Break a styled line to fit a width without losing colour or characters.

    textwrap cannot be used on styled text -- it counts escape bytes as
    columns. The shell needs every transcript line to be no wider than the
    frame or the frame tears, so this walks visible cells instead.
    """
    if w <= 0:
        return [text]
    if vlen(text) <= w:
        return [text]
    out: list[str] = []
    rest = text
    while vlen(rest) > w:
        out.append(truncate(rest, w, ellipsis=""))
        # Re-slice by visible position rather than by index into the string.
        seen = 0
        i = 0
        while i < len(rest) and seen < w:
            m = _ANSI_RE.match(rest, i)
            if m:
                i = m.end()
                continue
            seen += 1
            i += 1
        rest = rest[i:]
    if rest:
        out.append(rest)
    return out


# ---------------------------------------------------------------- answer card

# A rubric point is written to *score* an answer, not to be read as one: a
# fifth of them open with a grader's verb and the word `that`. Read aloud that
# voice is noise, and it is the same six words at the head of line after line,
# which is exactly where the eye needs the difference to be.
#
# Only `<verb> that|why|how` is stripped, and deliberately not a bare verb.
# "Explains that the Discount Rate affects PV" has a whole clause after the
# `that` and loses nothing; "Identifies the Discount Rate and Terminal Value
# as the biggest assumptions" does not, and taking the verb off it leaves a
# fragment starting mid-sentence. 721 of 3,617 points match the safe form.
# The stored point is never modified -- this is a rendering decision.
_VOICE = re.compile(
    r"^(?:The candidate |A good answer |Candidate )?"
    r"(?:States|Explains|Names|Mentions|Notes|Defines|Identifies|Gives|"
    r"Describes|Recognises|Recognizes|Understands|Shows|Says|Points out)"
    r"\s+(?:that|why|how)\s+",
    re.I)


def claim(point: str) -> str:
    """A rubric point as the thing you would actually say out loud."""
    text = (point or "").strip()
    stripped = _VOICE.sub("", text, count=1).strip()
    if stripped == text or len(stripped) < 12:
        return text
    return stripped[0].upper() + stripped[1:]


def answer_card(points: list[str], *, hits: list[bool] | None = None,
                traps: list[str] | None = None, w: int | None = None,
                indent: str = "  ") -> str:
    """The rubric as the answer, one claim a line.

    This is the reveal. The prose `answer_key` is deliberately not in it: two
    thirds of the ones on file are a single unbroken paragraph averaging 674
    characters, and printing that under a checklist of the same content meant
    reading the answer twice, the second time as a wall. The paragraph is one
    keystroke away instead of unavoidable.

    `hits` is what the grader found, one bool per point in the same order --
    exactly the shape `grade.grade()` already returns. Given it, each claim
    carries whether you made it; without it every claim is neutral, because a
    self-rated sitting has no per-point judgement and inventing a tick for it
    would be a mark that means nothing.
    """
    w = min(w or width(), PROSE_MAX)
    points = [p for p in (points or []) if p and p.strip()]
    if not points:
        return ""

    n = len(points)
    hit: list = list(hits or [])[:n]
    hit += [None] * (n - len(hit))
    scored = hits is not None

    out: list[str] = []
    title = head("A GOOD ANSWER HITS")
    if scored:
        got = sum(1 for h in hit if h)
        tally = (ok if got == n else warn if got * 2 >= n else bad)(f"{got} of {n}")
        gap = max(1, w - len(indent) - vlen(title) - vlen(tally))
        out.append(f"{indent}{title}{' ' * gap}{tally}")
    else:
        out.append(f"{indent}{title}")
    out.append(indent + hairline(w - len(indent)))

    for i, point in enumerate(points, 1):
        mark = dim("·") if not scored else ok("✓") if hit[i - 1] else bad("✗")
        lead = f"{indent}{mark} {dim(str(i))}  "
        # Continuation hangs under the claim, not under the number: a wrapped
        # point that restarts at the tick reads as a second point. Measured
        # with vlen(strip()) because the mark and the number are styled.
        col = vlen(strip(lead))
        lines = textwrap.wrap(claim(point), max(20, w - col)) or [""]
        out.append(lead + lines[0])
        out.extend(" " * col + line for line in lines[1:])

    traps = [t for t in (traps or []) if t and t.strip()]
    if traps:
        out.append("")
        out.append(f"{indent}{warn('common mistakes')}")
        for t in traps[:4]:
            lines = textwrap.wrap(t.strip(), max(20, w - len(indent) - 4)) or [""]
            out.append(f"{indent}  {dim('-')} {lines[0]}")
            out.extend(" " * (len(indent) + 4) + line for line in lines[1:])
    return "\n".join(out)
