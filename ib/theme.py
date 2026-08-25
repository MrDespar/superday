"""The colour themes, defined once.

Imports nothing, for the same reason `topics.py` imports nothing: `ui.py` is
the bottom of the styling stack and everything in the tool draws through it,
so the table it reads cannot pull a dependency in behind it.

A theme is the whole look -- eleven foreground tokens, the surface behind a
hovered row, and the background the shell paints its frame on. Those travel
together because they are only legible together: the quiet end of any palette
is picked relative to the ground it sits on, and a palette drawn on someone
else's background is the bug this module exists to fix.

**The background is the theme's, not the terminal's.** superday used to inherit
whatever was behind the window, and on a terminal running any transparency the
real backdrop was a mid grey the palette had never been tuned against -- `faint`
came out at 1.04:1, which is not "hard to read", it is the background. A theme
whose `bg` is None is opting back into that deliberately; every shipped theme
names its own.

Every token here is verified rather than trusted. The upstream hexes for the
ported themes are the editors' own, but an editor draws them on syntax spans
rather than on a list's third column, and several arrived under the floor a
table needs -- so each one was lifted along its own hue until it cleared, and
`tests.py` re-checks all of them on every run. `FLOORS` is that contract.
"""
from __future__ import annotations

from dataclasses import dataclass

# What each token has to clear, as a WCAG contrast ratio against the theme's
# own background. Two tiers on the quiet end rather than three, because
# `faint` was carrying real content -- the topic column, the due date, the
# rubric points -- at a contrast picked for chrome.
#
#   text        the question, the answer prose: read for minutes at a time
#   muted       information you read: topic, due, meta, rubric points
#   faint       chrome you should be able to find: hints, the keymap, counts
#   line        borders and rules: seen rather than read
#
# The accents sit at 4.0 rather than 4.5 on purpose. They are never body text
# -- a header, a one-word verdict, a five-cell pip -- and 4.5 on a saturated
# hue costs enough lightness to wash the hue out, which loses the thing the
# colour was carrying.
FLOORS: dict[str, float] = {
    "text": 9.0, "muted": 5.0, "faint": 3.0, "line": 1.8,
    "accent": 4.0, "accent_dim": 3.5, "mauve": 4.0,
    "sky": 4.0, "mint": 4.0, "gold": 4.0, "coral": 4.0,
}

# A floor is a token measured against the background. It says nothing about
# two tokens measured against *each other*, and a palette can clear every
# floor while saying two opposite things in one colour: `github-light` and
# `monokai` both had `accent` and `coral` on the same hex, so `ui.head` and
# `ui.bad` emitted identical bytes and every section heading on every screen
# read as an error state -- `BY STATUS` in exactly the red of the `rejected`
# count underneath it.
#
# Only this one pair is held apart. The others that share a hex (`accent` with
# `mauve`, `accent_dim` with `mauve`) are structural tokens that mean roughly
# the same thing anyway, and `accent_dim` draws the input box border, which is
# never simultaneously an error. Heading-versus-error is the pair that
# actually appears side by side carrying opposite meanings.
OPPOSED: tuple[tuple[str, str], ...] = (("accent", "coral"),)

# A hovered row is a surface, not a second cursor bar: the bar says what a
# keystroke would take, the wash says what a click would. Enough lift to read
# as a change, not enough to compete.
HOVER_LIFT = (1.15, 1.60)


@dataclass(frozen=True)
class Theme:
    name: str
    dark: bool
    bg: str | None      # the frame's own ground; None inherits the terminal's
    hover: str
    accent: str
    accent_dim: str
    mauve: str
    sky: str
    mint: str
    gold: str
    coral: str
    text: str
    muted: str
    faint: str
    line: str

    def tokens(self) -> dict[str, str]:
        """The foreground tokens, by name. `bg` and `hover` are backgrounds
        and are asked for separately -- a caller painting text with the
        background colour is a caller drawing nothing."""
        return {k: getattr(self, k) for k in FLOORS}


def rgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))    # type: ignore[return-value]


def _lin(c: int) -> float:
    x = c / 255
    return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4


def luminance(h: str) -> float:
    r, g, b = (_lin(c) for c in rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG relative contrast between two hex colours, 1.0 to 21.0."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# xterm's 216-colour cube plus its 24 greys. Computed rather than tabulated
# per theme: a hand-written 256 index next to every hex is twelve numbers per
# theme that nothing checks and that drift the moment a hex is retuned.
_CUBE = (0, 95, 135, 175, 215, 255)


def xterm256(h: str) -> int:
    """The closest xterm-256 index, for terminals without 24-bit colour."""
    r, g, b = rgb(h)

    def near(v: int) -> tuple[int, int]:
        best = min(range(6), key=lambda i: abs(_CUBE[i] - v))
        return best, _CUBE[best]

    ri, rv = near(r)
    gi, gv = near(g)
    bi, bv = near(b)
    cube_err = (rv - r) ** 2 + (gv - g) ** 2 + (bv - b) ** 2
    cube_idx = 16 + 36 * ri + 6 * gi + bi

    # The grey ramp resolves a near-neutral far better than the cube does,
    # which matters here: `text`, `muted`, `faint` and `line` are all greys
    # in most of these themes, and rounding them into the cube collapses two
    # of the four onto one index.
    grey = min(range(24), key=lambda i: abs((8 + i * 10) - (r + g + b) / 3))
    gval = 8 + grey * 10
    grey_err = (gval - r) ** 2 + (gval - g) ** 2 + (gval - b) ** 2
    return 232 + grey if grey_err <= cube_err else cube_idx


# ---------------------------------------------------------------- the themes
#
# Ported from the editors' own palettes, then lifted where a hue arrived under
# its floor. The ports are deliberately faithful about hue and deliberately
# not faithful about lightness: an editor colours a keyword inside a line of
# code the eye is already resting on, while these have to carry a table
# column read at a glance.
#
# `superday` stays the default and stays what it was. Only `faint` moved
# (#5a5661 -> #686370), because the fix for the rest of it was giving the
# palette the background it had always assumed.

THEMES = {
    "superday": Theme(
        name="superday", dark=True, bg="17151b", hover="2d2935",
        accent="ff5f9e", accent_dim="c2477a", mauve="b48ead", sky="7cc4f0",
        mint="7dd3c0", gold="e0976a", coral="ef6f6f", text="d8d4dc",
        muted="8b8794", faint="686370", line="46424e"
    ),
    "superday-soft": Theme(
        name="superday-soft", dark=True, bg="1c1a21", hover="312d39",
        accent="f07dab", accent_dim="c86f96", mauve="b99cb4", sky="93c9ea",
        mint="93d2c4", gold="dda680", coral="e88a8a", text="cec9d3",
        muted="9a95a3", faint="6d6875", line="55505e"
    ),
    "superday-high": Theme(
        name="superday-high", dark=True, bg="0d0c10", hover="272430",
        accent="ff9ac4", accent_dim="ff7fb0", mauve="d7b8d1", sky="a9dcff",
        mint="a6ecdc", gold="f5bd93", coral="ffa3a3", text="f2eff5",
        muted="c0bac9", faint="8e8899", line="6a6478"
    ),
    "night-owl": Theme(
        name="night-owl", dark=True, bg="011627", hover="022c4e",
        accent="c792ea", accent_dim="ff6e96", mauve="c792ea", sky="82aaff",
        mint="addb67", gold="ecc48d", coral="ef5350", text="d6deeb",
        muted="8badc1", faint="5f7e97", line="234763"
    ),
    "one-dark": Theme(
        name="one-dark", dark=True, bg="282c34", hover="373d48",
        accent="c678dd", accent_dim="e06c75", mauve="c678dd", sky="61afef",
        mint="98c379", gold="e5c07b", coral="e06c75", text="cfd3db",
        muted="979eaa", faint="7f848e", line="4e5566"
    ),
    "dracula": Theme(
        name="dracula", dark=True, bg="282a36", hover="383b4b",
        accent="ff79c6", accent_dim="bd93f9", mauve="bd93f9", sky="8be9fd",
        mint="50fa7b", gold="f1fa8c", coral="ff5555", text="f8f8f2",
        muted="b3b8cc", faint="6272a4", line="4f5369"
    ),
    "tokyo-night": Theme(
        name="tokyo-night", dark=True, bg="1a1b26", hover="2c2e41",
        accent="bb9af7", accent_dim="f7768e", mauve="bb9af7", sky="7aa2f7",
        mint="9ece6a", gold="e0af68", coral="f7768e", text="c0caf5",
        muted="9aa5ce", faint="5d6794", line="3f4768"
    ),
    "nord": Theme(
        name="nord", dark=True, bg="2e3440", hover="3d4554",
        accent="88c0d0", accent_dim="81a1c1", mauve="b48ead", sky="81a1c1",
        mint="a3be8c", gold="ebcb8b", coral="cc8087", text="eceff4",
        muted="d8dee9", faint="7b88a1", line="525c72"
    ),
    "gruvbox": Theme(
        name="gruvbox", dark=True, bg="282828", hover="3a3a3a",
        accent="d3869b", accent_dim="fb4934", mauve="d3869b", sky="83a598",
        mint="b8bb26", gold="fabd2f", coral="fb4934", text="ebdbb2",
        muted="bdae93", faint="928374", line="58504c"
    ),
    "catppuccin": Theme(
        name="catppuccin", dark=True, bg="1e1e2e", hover="303049",
        accent="f5c2e7", accent_dim="cba6f7", mauve="cba6f7", sky="89b4fa",
        mint="a6e3a1", gold="fab387", coral="f38ba8", text="cdd6f4",
        muted="a6adc8", faint="6c7086", line="474a5d"
    ),
    "monokai": Theme(
        name="monokai", dark=True, bg="272822", hover="383a31",
        accent="f9377d", accent_dim="ae81ff", mauve="ae81ff", sky="66d9ef",
        mint="a6e22e", gold="e6db74", coral="ff5c57", text="f8f8f2",
        muted="c0c1b5", faint="75715e", line="535146"
    ),
    "solarized-dark": Theme(
        name="solarized-dark", dark=True, bg="002b36", hover="003e4e",
        accent="da5897", accent_dim="7176c6", mauve="7c80ca", sky="268bd2",
        mint="859900", gold="b58900", coral="e35957", text="c6cece",
        muted="8b9b9d", faint="657b83", line="0b586b"
    ),
    "mono": Theme(
        name="mono", dark=True, bg="141414", hover="2a2a2a",
        accent="e0e0e0", accent_dim="b8b8b8", mauve="a8a8a8", sky="c4c4c4",
        mint="cccccc", gold="b0b0b0", coral="d4d4d4", text="ededed",
        muted="a5a5a5", faint="767676", line="4a4a4a"
    ),
    "github-light": Theme(
        name="github-light", dark=False, bg="ffffff", hover="e3e3e3",
        accent="0550ae", accent_dim="8250df", mauve="8250df", sky="0969da",
        mint="1a7f37", gold="9a6700", coral="cf222e", text="1f2328",
        muted="59636e", faint="818b98", line="b2bfcb"
    ),
    "solarized-light": Theme(
        name="solarized-light", dark=False, bg="fdf6e3", hover="f7d988",
        accent="d33682", accent_dim="6c71c4", mauve="6c71c4", sky="227cbc",
        mint="6e7f00", gold="967100", coral="dc322f", text="073642",
        muted="566b72", faint="7e8f91", line="c9b579"
    ),
}

DEFAULT = "superday"


def get(name: str | None) -> Theme:
    """The named theme, or the default when it is unset or unknown.

    Unknown falls back rather than raising: the name comes out of a config
    file a person edits, and a typo there should cost the look of the shell
    for one launch, not the shell.
    """
    return THEMES.get((name or "").strip().lower(), THEMES[DEFAULT])
