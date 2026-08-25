"""Navigable views: the parts of the UI that answer to the arrow keys.

A command that returns a list used to print it and forget it. Everything
after that -- reading one in full, comparing two, jumping to the next -- meant
typing another command against an ID you had to scroll back to find.

A view keeps the list alive: it holds the rows, tracks a selection, and
expands the selected row in place. It renders into the shell's frame while it
is up and leaves nothing behind when you put it down -- the command that drew
it is one keystroke back in the history, and a thousand rows of dead text
above the prompt is not scrollback anyone reads.

`flatten()` is the other half of that: the same view, rendered whole, for a
caller with no shell to attach to. One definition of the columns and the
ordering, two ways of showing it.

Views are also the only place in the tool that decides result *order*. Search
hands back whatever BM25 ranked; the view is what turns that into something
with a shape you can scan.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Callable, NamedTuple, Sequence

from . import analytics, browse, crossaudit, dupes, llm, scheduler, tagging, topics, ui
from . import tui
from .tui import Key, View
from .ui import BOLD, DIM, faint, pad, paint, truncate, vlen

# ---------------------------------------------------------------- atoms

STATUS_DOT = {"active": "mint", "needs_review": "gold", "rejected": "coral"}


def status_dot(status: str) -> str:
    return paint("●", STATUS_DOT.get(status, "faint"))


def difficulty_pips(level: int | None) -> str:
    """Five cells, filled to the question's difficulty. Unset reads as unset."""
    if not level:
        return faint("·····")
    level = max(1, min(5, int(level)))
    name = "mint" if level <= 2 else "gold" if level == 3 else "coral"
    return paint("▆" * level, name) + faint("·" * (5 - level))


def due_label(due_at: str | None) -> str:
    if not due_at:
        return faint("unseen")
    try:
        when = datetime.fromisoformat(due_at)
    except ValueError:
        return faint("-")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    left = (when - datetime.now(timezone.utc)).total_seconds()
    if left <= 0:
        return ui.warn("due")
    # `.days` truncates, so a card coming round in ten minutes read as "due"
    # while the scheduler was still holding it back -- a list saying due next
    # to a question `drill` refuses to ask.
    if left < 86400:
        return faint("<1d")
    # Rounded up, not truncated: something 2.9 days out is three days away in
    # every sense that matters, and reading it as 2d makes the column lie by a
    # day for most of every interval.
    return faint(f"{math.ceil(left / 86400)}d")


def next_due_phrase(due_at: str | None) -> str:
    """`due_label` in a sentence. "next due due" is what you get without it."""
    if not due_at:
        return "never scheduled"
    try:
        when = datetime.fromisoformat(due_at)
    except ValueError:
        return "scheduled"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    left = (when - datetime.now(timezone.utc)).total_seconds()
    if left <= 0:
        return "due again now"
    if left < 3600:
        return f"due again in {max(1, int(left // 60))} minutes"
    if left < 86400:
        return f"due again in {int(left // 3600)} hours"
    days = math.ceil(left / 86400)
    return "due again tomorrow" if days == 1 else f"due again in {days} days"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def group_rule(name: str, width: int) -> str:
    """A group heading, and the rule that carries its line out to the margin.

    The rule has to end in the same column as every other rule on the screen.
    It used to be spelled `width - len(name) - 5` in three separate places,
    which is two cells short of the `"  " + hairline(width - 2)` that
    `PickerView.header` draws directly above it: `2 + len(name) + 1` of lead
    leaves `width - len(name) - 3` for the dashes, not five. The result was a
    ragged right edge on every do-screen and every grouped result list,
    written out three times and so wrong three times.

    Measured with `vlen` rather than `len` for the same reason everything
    else here is: `name` reaches this via `.upper()`, and a group named after
    a topic label is not guaranteed to be one cell per character.
    """
    label = name.upper()
    return ("  " + paint(label, "mauve", BOLD) + " "
            + faint("─" * max(0, width - vlen(label) - 3)))


# ---------------------------------------------------------------- actions


class Action(NamedTuple):
    """Something a list can hand the row under the cursor to.

    `line` is a command, typed for you. `arm` is the sentence the row turns
    into when it has been chosen once and is waiting to be chosen again --
    empty means the action runs on the first press, which is right for
    anything that only reads. Anything that writes to the schedule carries
    one, because an action a keystroke away from where the cursor rests has
    to cost a second, identical keystroke.

    `prefill` is for a command the view can start and only you can finish:
    `tag 774 ` needs tag names nothing here knows, so the box gets the half
    the view is sure about and the cursor comes with it.

    `do` is the other kind of thing a list can offer: not a command at all,
    but a switch that changes how the list behind it is put together. It
    carries no `line`, runs no command, and is drawn with `⇄` rather than
    `▶`. Grouping and expand-all live here because they had nowhere else to
    live -- both were bound only to an alt- chord, and a terminal that eats
    Alt made them unreachable rather than merely inconvenient.
    """
    key: str
    label: str
    line: str = ""
    arm: str = ""
    prefill: bool = False
    do: "Callable[[], None] | None" = None
    mark: str = "▶"


def action_row(label: str, chosen: bool, armed: str = "", mark: str = "▶",
               width: int = 0) -> str:
    """One action, drawn the same way in every list that has any.

    `mark` is the only thing that varies: `▶` runs something, `⇄` changes how
    the screen behind it is put together. Two shapes is enough to tell a
    command from a switch without a second colour.

    `width` clamps it. An action's label is written by whoever offers the
    action and is the one row in these lists nothing else measures -- a long
    one wrapped in the terminal rather than being cut, and the overflow landed
    on the input box's own border and stayed there, because the frame differ
    only repaints rows the view admits to owning.
    """
    bar = paint("▎", "accent") if chosen else " "
    if armed:
        line = f"{bar} " + paint("▶", "coral") + " " + paint(armed, "coral", BOLD)
    else:
        glyph = paint(mark, "mint" if chosen else "faint")
        line = f"{bar} {glyph} " + paint(label, "mint", BOLD if chosen else "")
    return truncate(line, width) if width else line


def fire(view, act: Action, shell) -> bool:
    """Arm it, or run it. `view` supplies `_armed`; every list shares this.

    Two lists reimplementing "press it twice" is two lists that will disagree
    about it, and the one that gets it wrong starts a sitting over the whole
    bank by accident.
    """
    if act.arm and view._armed != act.key:
        view._armed = act.key
        return True
    view._armed = None
    # A switch changes the list behind this screen and starts nothing, so it
    # runs with or without a shell -- there is no command for one to run.
    if act.do is not None:
        act.do()
        return True
    if shell is None:
        return True
    if act.prefill:
        shell.prefill(act.line)
    else:
        shell.run_now(act.line)
    return True




# ---------------------------------------------------------------- picker


class PickerView(View):
    """A list you move a cursor through and open rows inside.

    Everything that is the same for every list -- the cursor, the scroll
    window that has to account for however many lines an open row costs, the
    group headings, the flatten into plain text -- lives here. A
    subclass supplies four things: how many rows there are, how to draw one,
    what is inside one, and what Enter means on a row that has no inside.
    """

    empty_text = "nothing here"

    def __init__(self, *, title: str, subject: str = "", note: str = "",
                 tally: str = ""):
        super().__init__()
        self.title = title
        self.subject = subject
        self.note = note
        self.tally = tally
        self.sel = 0
        self.top = 0
        self.focused = True
        self.expanded: set[int] = set()
        self.hover: int | None = None
        # The do-screen `←` opens, drawn in this view's place while it is up.
        self._doing: "ActionsView | None" = None
        self._detail: dict[int, list[str]] = {}
        self._width = -1

    # -- subclass API
    def count(self) -> int:
        raise NotImplementedError

    def row(self, idx: int, width: int, chosen: bool) -> str:
        raise NotImplementedError

    def detail(self, idx: int, width: int) -> list[str]:
        return []

    def expandable(self, idx: int) -> bool:
        return True

    def group_of(self, idx: int) -> str | None:
        return None

    def activate(self, idx: int, shell) -> bool:
        """Enter on a row with nothing to expand. Return True if handled."""
        return False

    def actions(self, idx: int) -> list[Action]:
        """What `←` offers for row `idx`. Empty means `←` has nothing to open.

        This is how a list reaches a command without a chord. Alt- and ctrl-
        chords get eaten by the terminal emulator or the window manager
        before the process sees them, so a list whose only route to "drill
        this" was `⌥d` was a list that did not work on someone's machine.
        `←` is the key `browse` already spends on "up a level", and up a
        level from a row is the things you can do to it.
        """
        return []

    def switches(self) -> list[Action]:
        """List-wide toggles, offered on the same do-screen as the row's own.

        These are about the whole list rather than the row under the cursor,
        but they belong here for the reason `actions` exists at all: `⌥a` was
        the only way to expand every row, and Alt is eaten by the terminal or
        the window manager on plenty of machines, so "expand all" was not a
        keystroke away -- it was unreachable. The chord stays bound for
        terminals that pass it through, and is no longer advertised, which is
        the same deal every other accelerator in here already had.
        """
        if not any(self.expandable(i) for i in range(self.count())):
            return []
        n = self.count()
        opened = len(self.expanded) >= sum(self.expandable(i) for i in range(n))

        def toggle() -> None:
            if opened:
                self.expanded.clear()
            else:
                self.expanded = {i for i in range(n) if self.expandable(i)}

        return [Action(key="expand-all",
                       label="collapse every row" if opened else "expand every row",
                       do=toggle, mark="⇄")]

    def action_subject(self, idx: int) -> str:
        """What the action screen calls the row it is acting on."""
        return ""

    def extra_keys(self, key: Key, shell) -> bool:
        return False

    def hints(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "open"), ("esc", "done")]

    # -- caching
    def _cached_detail(self, idx: int, width: int) -> list[str]:
        if width != self._width:
            self._detail.clear()
            self._width = width
        if idx not in self._detail:
            self._detail[idx] = self.detail(idx, width)
        return self._detail[idx]

    def invalidate(self) -> None:
        self._detail.clear()

    # -- render
    def header(self, width: int) -> list[str]:
        left = "  " + ui.head(self.title) + (
            "  " + paint(self.subject, "mauve", BOLD) if self.subject else "")
        right = faint(self.tally or _plural(self.count(), "row"))
        gap = max(1, width - vlen(left) - vlen(right))
        out = [left + " " * gap + right, "  " + ui.hairline(width - 2)]
        if self.note:
            out.append("  " + faint(self.note))
        return out

    def render(self, width: int) -> list[str]:
        if self._doing is not None:
            self._doing.viewport = self.viewport
            lines = self._doing.render(width)
            self.owner = list(self._doing.owner)
            return lines
        self.owner = []
        out: list[str] = []

        def put(line: str, item: int = -1) -> None:
            # Clamped here rather than trusted from each row builder: one line
            # a cell too long wraps in the terminal, which pushes the input box
            # off the bottom of the frame and tears the whole screen. It is
            # also the failure that shows up two panels away from its cause,
            # so it is worth paying for on every row.
            out.append(truncate(line, width))
            self.owner.append(item)

        put("")
        for line in self.header(width):
            put(line)
        put("")

        n = self.count()
        if not n:
            put("  " + faint(self.empty_text))
            return out

        self.sel = max(0, min(self.sel, n - 1))
        body = max(3, self.viewport - len(out) - 2)
        self._scroll_into_view(body, width)
        idx = self.top
        used = 0
        last_group = None
        while idx < n and used < body:
            g = self.group_of(idx)
            if g is not None and g != last_group:
                last_group = g
                put(group_rule(g, width))
                used += 1
            chosen = self.focused and idx == self.sel
            line = self.row(idx, width, chosen)
            if self.focused and idx == self.hover and not chosen:
                # The pointer says "this one, if you click"; the cursor bar
                # says "this one, if you press a key". Two different claims,
                # so the hover is a surface behind the row rather than a
                # second bar competing with the first.
                line = ui.wash(line, "hover", width)
            put(line, idx)
            used += 1
            if idx in self.expanded:
                for line in self._cached_detail(idx, width):
                    if used >= body:
                        break
                    put(line, idx)
                    used += 1
                put("", idx)
                used += 1
            idx += 1

        hidden = n - idx
        if self.top or hidden > 0:
            bits = []
            if self.top:
                bits.append(f"↑ {self.top} above")
            if hidden > 0:
                bits.append(f"↓ {hidden} below")
            put("  " + faint("   ".join(bits)))
        return out

    def _scroll_into_view(self, body: int, width: int) -> None:
        """Keep the cursor on screen, counting the rows expansion costs."""
        if not self.focused:
            self.top = 0
            return
        if self.sel < self.top:
            self.top = self.sel
            return
        while True:
            used = 0
            i = self.top
            last_group = None
            while i < self.count() and used < body:
                g = self.group_of(i)
                if g is not None and g != last_group:
                    last_group = g
                    used += 1
                used += 1
                if i in self.expanded:
                    used += len(self._cached_detail(i, width)) + 1
                if i == self.sel:
                    return
                i += 1
            if self.top >= self.sel:
                return
            self.top += 1

    # -- input
    def page(self) -> int:
        """How far PgUp/PgDn move. Roughly a screenful of collapsed rows."""
        return max(1, self.viewport - 6)

    def open_actions(self) -> bool:
        acts = self.actions(self.sel) + self.switches()
        if not acts:
            return False
        self._doing = ActionsView(title=self.title,
                                  subject=self.action_subject(self.sel),
                                  actions=acts)
        return True

    def close_actions(self) -> None:
        self._doing = None

    def handle(self, key: Key, shell) -> bool:
        if self._doing is not None:
            if key.name == "left" and not self._doing.armed:
                self.close_actions()
                return True
            handled = self._doing.handle(key, shell)
            if self._doing.fired:
                # The command it started owns the screen now, and what comes
                # back afterwards is the list, not the menu you left open.
                self.close_actions()
            return handled
        n = key.name
        last = max(0, self.count() - 1)
        if n == "up":
            self.sel = max(0, self.sel - 1)
        elif n == "down":
            self.sel = min(last, self.sel + 1)
        elif n == "pgup":
            self.sel = max(0, self.sel - self.page())
        elif n == "pgdn":
            self.sel = min(last, self.sel + self.page())
        elif n == "home":
            self.sel, self.top = 0, 0
        elif n == "end":
            self.sel = last
        elif n == "right":
            if self.expandable(self.sel):
                self.expanded.add(self.sel)
        elif n == "left":
            # Collapse first, walk up second -- the same order `browse` uses,
            # because left already means "close this" everywhere else.
            if self.sel in self.expanded:
                self.expanded.discard(self.sel)
            else:
                self.open_actions()
        elif n == "enter":
            self._toggle(self.sel, shell)
        elif n == "alt-a":
            if len(self.expanded) >= self.count():
                self.expanded.clear()
            else:
                self.expanded = {i for i in range(self.count()) if self.expandable(i)}
        else:
            return self.extra_keys(key, shell)
        return True

    def click(self, item: int, shell) -> bool:
        if self._doing is not None:
            handled = self._doing.click(item, shell)
            if self._doing.fired:
                self.close_actions()
            return handled
        if item == self.sel:
            self._toggle(item, shell)
        else:
            self.sel = item
            if self.expandable(item):
                self.expanded.add(item)
        return True

    def hover_at(self, item: int | None, col: int) -> bool:
        if self._doing is not None:
            return self._doing.hover_at(item, col)
        if self.hover == item:
            return False
        self.hover = item
        return True

    def scroll_by(self, delta: int) -> bool:
        """The wheel moves the cursor, and stops claiming it at either end.

        Stopping is the whole point: one gesture runs down the list and then
        keeps going into the transcript behind it. A view that swallowed the
        wheel forever left you scrolling a list that could not move, which
        reads as a list with no more rows when it has nine hundred.
        """
        n = self.count()
        if not n:
            return False
        target = max(0, min(n - 1, self.sel + delta))
        if target == self.sel:
            return False
        self.sel = target
        return True

    def _toggle(self, idx: int, shell) -> None:
        if not self.expandable(idx):
            self.activate(idx, shell)
            return
        if idx in self.expanded:
            self.expanded.discard(idx)
        else:
            self.expanded.add(idx)

    def footer(self) -> str:
        if self._doing is not None:
            return self._doing.footer()
        if not self.count():
            return faint("nothing to move through")
        return _keyline(self.hints())

    def flatten(self, width: int) -> list[str]:
        """Every row at once, minus the cursor and the scroll window.

        This is the no-shell path, so the window has to come off: a printout
        that stopped after twenty rows because that is what fits on a screen
        would be a printout that lies about what matched.
        """
        keep = (self.focused, self.top, self.viewport, self._doing)
        self.focused = False
        self.hover = None
        self._doing = None
        self.top = 0
        self.viewport = 10_000
        try:
            return self.render(width)
        finally:
            self.focused, self.top, self.viewport, self._doing = keep


class ActionsView(PickerView):
    """The do-screen: what the row you were on can be handed to.

    It is a picker like any other, which is the point -- the same arrows, the
    same cursor, the same `←` meaning back. It never opens on its own: the
    list that owns it draws it in its own place and takes it down again, so
    the shell still sees one view and the transcript still gets one thing
    left behind at the end.

    Nothing in here is reachable any other way, and nothing in here needs a
    modifier.
    """

    empty_text = "nothing to do with this one"

    def __init__(self, *, title: str, subject: str, actions: list[Action]):
        n = len(actions)
        super().__init__(title=title, subject=subject,
                         tally=f"{n} thing{'' if n == 1 else 's'} you can do")
        self.acts = actions
        self._armed: str | None = None
        self.fired = False

    @property
    def armed(self) -> bool:
        return self._armed is not None

    def count(self) -> int:
        return len(self.acts)

    def expandable(self, idx: int) -> bool:
        return False

    def group_of(self, idx: int) -> str | None:
        return "do"

    def row(self, idx: int, width: int, chosen: bool) -> str:
        act = self.acts[idx]
        return action_row(act.label, chosen,
                          act.arm if self._armed == act.key else "",
                          mark=act.mark, width=width)

    def activate(self, idx: int, shell) -> bool:
        act = self.acts[idx]
        was_armed = self._armed == act.key
        fire(self, act, shell)
        if not act.arm or was_armed:
            self.fired = True
        return True

    def handle(self, key: Key, shell) -> bool:
        n = key.name
        row = self.acts[self.sel] if self.sel < len(self.acts) else None
        if self._armed and not (n == "enter" and row and row.key == self._armed):
            # Anything that is not the same key again backs out, and the key
            # that backed out is spent doing so.
            self._armed = None
            if n == "left":
                return True
        if n == "right":
            # → means "into this one", and an action has no inside. It is also
            # the key you are holding down on the way here.
            return True
        return super().handle(key, shell)

    def click(self, item: int, shell) -> bool:
        if item != self.sel:
            self._armed = None
            self.sel = item
            return True
        return super().click(item, shell)

    def hints(self) -> list[tuple[str, str]]:
        if self.armed:
            return [("⏎", "yes, do it"), ("←", "back out")]
        return [("↑↓", "move"), ("⏎", "run it"), ("←", "back"), ("esc", "done")]


def _keyline(pairs: list[tuple[str, str]]) -> str:
    bits = []
    for i, (key, what) in enumerate(pairs):
        if i:
            bits.append(faint("·"))
        bits.append(paint(key, "accent") + faint(" " + what))
    return " ".join(bits)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------- questions

SORTS = [
    ("relevance", "best match first"),
    ("topic", "grouped by topic"),
    ("difficulty", "hardest first"),
    ("due", "most overdue first"),
    ("id", "bank order"),
]


class ResultsView(PickerView):
    """A ranked, columned, expandable list of questions.

    The column grid is fixed rather than computed per row: a list whose
    columns move as you scroll it is unreadable, and every row here has the
    same handful of facts before the question text starts.
    """

    empty_text = "nothing matched"

    def __init__(self, conn: sqlite3.Connection, rows: list[dict], *,
                 title: str, subject: str = "", note: str = "",
                 highlight: list[str] | None = None):
        super().__init__(title=title, subject=subject, note=note)
        self.conn = conn
        self.rows = [dict(r) | {"_rank": i} for i, r in enumerate(rows)]
        self.highlight = [t.lower() for t in (highlight or []) if len(t) > 2]
        self.sort = "relevance"
        self.group = False
        self._decorate()
        self._reorder()

    def count(self) -> int:
        return len(self.rows)

    @property
    def tally(self) -> str:
        return (_plural(len(self.rows), "result") + "  ·  "
                + dict(SORTS)[self.sort])

    @tally.setter
    def tally(self, _v: str) -> None:
        pass

    def group_of(self, idx: int) -> str | None:
        return (self.rows[idx].get("topic") or "general") if self.group else None

    # -- data
    def _decorate(self, rows: list[dict] | None = None) -> None:
        """Pull the facts the columns need in one query, not one per row.

        Takes a list because `self.rows` is not the only one: `browse`'s
        preview pane runs its own query, and an undecorated row carries no
        `difficulty` and no `due_at` -- so sorting it by either quietly fell
        back to bank order and the sort looked broken rather than absent.
        """
        rows = self.rows if rows is None else rows
        ids = [r["id"] for r in rows]
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        extra = {r["id"]: dict(r) for r in self.conn.execute(
            f"SELECT q.id, q.difficulty, q.subtopic, q.kind, s.due_at, s.reps "
            f"FROM questions q LEFT JOIN schedule s ON s.question_id = q.id "
            f"WHERE q.id IN ({marks})", ids)}
        for r in rows:
            r.update({k: v for k, v in extra.get(r["id"], {}).items() if k != "id"})

    def on_resume(self) -> None:
        """A drill started from a row moves the due dates in that same row.

        The list comes back after the sitting, so the columns it is showing
        are one command out of date unless they are re-read. Only the derived
        columns are refreshed -- re-running the search instead would reshuffle
        the list under a cursor you left somewhere on purpose.
        """
        self._decorate()
        self.invalidate()

    def sort_key(self, row: dict):
        """How the current sort orders one row.

        Broken out of `_reorder` because `self.rows` is not the only list of
        questions a view of this family draws: `browse`'s tree mode shows a
        preview pane built from its own query, and while the ordering lived
        inside `_reorder` that pane came out in bank order whatever the sort
        said. Pressing the sort key there re-sorted a list you were not
        looking at, which is a control with no visible effect.

        Every lookup is a `.get`: a preview row is a lighter record than a
        search hit and carries neither `_rank` nor `due_at`.
        """
        if self.sort == "relevance":
            # FTS hands back bm25 order with no score column attached, so the
            # ranking only survives if the arrival index is what we sort on.
            return (row.get("_rank", 0),)
        if self.sort == "topic":
            return (row.get("topic") or "~", -(row.get("difficulty") or 0), row["id"])
        if self.sort == "difficulty":
            return (-(row.get("difficulty") or 0), row.get("topic") or "~", row["id"])
        if self.sort == "due":
            return (row.get("due_at") or "9999", row["id"])
        return (row["id"],)

    def _reorder(self) -> None:
        keep = self.rows[self.sel]["id"] if self.rows and self.sel < len(self.rows) else None
        # Expansion is tracked by position, and a re-sort moves every position.
        # Left alone it opened whichever unrelated rows happened to land on
        # those numbers.
        open_ids = {self.rows[i]["id"] for i in self.expanded if i < len(self.rows)}
        self.rows.sort(key=self.sort_key)
        self._resorted()
        self.invalidate()
        self.expanded = {i for i, r in enumerate(self.rows) if r["id"] in open_ids}
        if keep is not None:
            for i, r in enumerate(self.rows):
                if r["id"] == keep:
                    self.sel = i
                    break

    # -- rows
    def row(self, idx: int, width: int, chosen: bool) -> str:
        r = self.rows[idx]
        caret = paint("▾" if idx in self.expanded else "▸",
                      "accent" if chosen else "faint")
        bar = paint("▎", "accent") if chosen else " "
        rank = pad(faint(f"{idx + 1:>3}"), 3)
        qid = pad(paint(f"#{r['id']}", "accent" if chosen else "sky"), 6)
        topic = pad(faint((r.get("topic") or "-")[:11]), 11)
        pips = difficulty_pips(r.get("difficulty"))
        due = pad(due_label(r.get("due_at")), 6)
        dot = status_dot(r.get("status") or "active")
        prefix = f"{bar} {caret} {rank} {dot} {qid} {topic} {pips} {due} "
        text_w = max(12, width - vlen(prefix) - 1)
        return prefix + self._headline(_one_line(r["canonical_text"]), text_w, chosen)

    def _headline(self, text: str, width: int, chosen: bool) -> str:
        """The question on one line, with the words you searched for lit up.

        Highlighting happens before truncation and after it would be wrong:
        truncate() counts visible cells, so the escape codes have to already
        be in place for the cut to land where it looks like it lands.

        BOLD and RESET are raw constants rather than palette lookups, so unlike
        paint() they emit at every depth. This asks the depth itself, because a
        redirected `find` was arriving with `\033[0m` welded to the end of any
        row short enough to survive truncation -- the long ones looked clean
        only because the cut threw the trailing reset away.
        """
        styled = ui.depth() > 0
        base = BOLD if (chosen and styled) else ""
        if not self.highlight:
            return truncate(ui.style(text, base) if base
                            else ui.paint(text, "text"), width)
        body_colour = ui.colour("text")
        out = []
        low = text.lower()
        i = 0
        while i < len(text):
            for term in self.highlight:
                if low.startswith(term, i):
                    out.append(ui.paint(text[i:i + len(term)], "accent", BOLD)
                               + base + body_colour)
                    i += len(term)
                    break
            else:
                out.append(text[i])
                i += 1
        body = "".join(out)
        lead = base + body_colour
        return truncate(lead + body + ui.RESET if lead else body, width)

    def detail(self, idx: int, width: int) -> list[str]:
        qid = self.rows[idx]["id"]
        row = self.conn.execute(
            "SELECT a.answer_key, a.rubric_points FROM answers a WHERE a.question_id = ?",
            (qid,)).fetchone()
        r = self.rows[idx]
        out: list[str] = []
        gutter = paint("  │  ", "line")
        body_w = width - 8
        answer = (row["answer_key"] if row else "") or ""
        if answer.strip():
            for line in ui.body(_soft(answer), "", body_w).split("\n"):
                out.append(gutter + ui.paint(line, "text"))
        else:
            out.append(gutter + faint("no answer on file yet"))
        points = json.loads((row["rubric_points"] if row else None) or "[]")
        if points:
            out.append(gutter)
            text = " · ".join(p if isinstance(p, str) else str(p.get("point", p))
                              for p in points[:4])
            for line in ui.wrap("must hit: " + text, "", body_w).split("\n"):
                out.append(gutter + faint(line))
        tags = tagging.tags_for(self.conn, qid)
        meta = [f"#{qid}", r.get("kind") or "technical"]
        if r.get("subtopic"):
            meta.append(r["subtopic"])
        if r.get("reps"):
            meta.append(_plural(r["reps"], "review"))
        line = faint(" · ".join(meta))
        if tags:
            line += "   " + " ".join(paint("#" + t, "mauve") for t in tags[:6])
        out.append(gutter)
        out.append(gutter + truncate(line, body_w))
        out.append(gutter + faint(
            f"← collapses · ⏎ opens #{qid} in full"))
        return out

    def actions(self, idx: int) -> list[Action]:
        if idx >= len(self.rows):
            return []
        r = self.rows[idx]
        qid = r["id"]
        out: list[Action] = []
        # Offered only when the scheduler would actually ask it. `--ids` does
        # not jump the due window -- nothing does -- so an action promising to
        # drill a question that comes round on Thursday would answer
        # "0 queued, 1 held back" and read as broken.
        if scheduler.due_questions(self.conn, limit=1, ids=[qid]):
            out.append(Action("drill", f"drill #{qid}", f"drill --ids {qid}",
                              arm=f"drill #{qid}? ⏎ starts the sitting · ← backs out"))
        # "show" used to live here too. It doesn't any more -- Enter on the
        # row opens the same screen now, and a row offering two paths to the
        # one thing that reads is a do-screen you have to check twice.
        out.append(Action("tag", f"tag #{qid}" + faint("   you type the tags"),
                          f"tag {qid} ", prefill=True))
        # Only when there is actually something to compare it with. Reaching a
        # near-duplicate used to mean running the whole-bank scan and then
        # finding this question in it, which is a minute of work to answer a
        # question about the row already under the cursor.
        twin = dupes.near(self.conn, qid, limit=1)
        if twin:
            other = twin[0]
            out.append(Action(
                "twin", f"compare it with #{other['id']}"
                + faint(f"   {other['similarity']:.0%} alike"),
                f"dupes --pair {qid},{other['id']}"))
        return out

    def _toggle(self, idx: int, shell) -> None:
        """Enter, or a second click, opens the full record instead of peeking.

        → still expands an inline preview without leaving the list -- that
        stays the free, quick-look gesture -- but Enter now commits, the same
        as it does one screen earlier in `browse`'s filters: → adds and
        stays, ⏎ adds and moves you on. Typing `show <id>` by hand afterward
        used to be the only way to actually read a question you had found.

        Only for an expandable row, i.e. a question. `BrowseView` shares this
        method for its filter screen too, where a row is a filter or a `do`
        action and `expandable` is already False -- that path still belongs
        to `activate`, unchanged.
        """
        if not self.expandable(idx):
            self.activate(idx, shell)
            return
        if idx >= len(self.rows) or shell is None:
            return
        shell.run_now(f"show {self.rows[idx]['id']}")

    def action_subject(self, idx: int) -> str:
        if idx >= len(self.rows):
            return ""
        r = self.rows[idx]
        return f"#{r['id']}  " + _one_line(r["canonical_text"])

    def switches(self) -> list[Action]:
        """Grouping, plus whatever the base picker offers.

        `⌥g` was the only way to reach this, which on a terminal that eats Alt
        meant there was no way to reach it. The chord still works where it
        survives the trip; this is the route that always does.
        """
        def toggle() -> None:
            self._set_group(not self.group)

        return super().switches() + [
            Action(key="group",
                   label="ungroup" if self.group else "group under topic headings",
                   do=toggle, mark="⇄")]

    def _resorted(self) -> None:
        """Hook for a subclass that keeps another list ordered by the sort."""

    def _set_group(self, on: bool) -> None:
        """Grouping implies the topic sort, or the headings interleave.

        One definition, because the chord and the switch row both change it
        and two copies of "and also fix the sort" is one copy that will be
        forgotten.
        """
        self.group = on
        if on and self.sort != "topic":
            self.sort = "topic"
            self._reorder()

    def extra_keys(self, key: Key, shell) -> bool:
        if key.name == "btab":
            names = [s for s, _ in SORTS]
            self.sort = names[(names.index(self.sort) + 1) % len(names)]
            self._reorder()
            return True
        if key.name == "alt-g":
            self._set_group(not self.group)
            return True
        return False

    def hints(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "open"), ("→", "peek"),
                ("←", "drill it, tag it"), ("⇧⇥", "sort"), ("esc", "done")]


def _soft(text: str) -> str:
    """Undo a source PDF's hard wrap so the reflow has paragraphs to work with."""
    out, buf = [], []
    for line in (text or "").replace("\r", "").split("\n"):
        if not line.strip():
            out.append(" ".join(buf))
            out.append("")
            buf = []
        else:
            buf.append(line.strip())
    out.append(" ".join(buf))
    return "\n".join(out)


# ---------------------------------------------------------------- tabs


class Pane(NamedTuple):
    """What a tab draws, and what it offers to do about it.

    A pane that only reports state makes you read a recommendation and then
    retype it. The dashboard was the last screen in the tool still doing that:
    it worked out that `drill -t dcf` was the best use of the next hour and
    then printed those nine characters in grey for you to copy.

    `lines` is the pane as before -- a build that returns a bare list is
    wrapped in one of these with no actions, so the panes that really are
    read-only (`stats`, `show`) did not have to change.
    """
    lines: list[str]
    actions: Sequence[Action] = ()


class TabsView(View):
    """One screen split into named panes you switch between.

    The dashboard is the case this exists for: readiness, mastery, retention,
    momentum and what-to-do-next are five separate questions that were being
    answered in one forty-row wall. Splitting them means each one gets the
    whole frame and none of them gets scrolled off by the others.

    Panes are built lazily and cached per width -- a tab you never open costs
    nothing, which matters when one of them runs a scan over the whole bank.

    A pane may also hand back actions, drawn as `action_row`s under it and
    walked with `↑↓`. `←` `→` are the tabs here, so the do-screen every list
    opens on `←` has nowhere to go; the actions live in the pane instead and
    are run with the same two identical presses `fire` asks for everywhere
    else. `⏎⏎` means the same thing here as it does in `browse`.
    """

    def __init__(self, title: str,
                 tabs: list[tuple[str, "Callable[[int], list[str]]"]],
                 *, subject: "str | Callable[[], str]" = "", start: int = 0,
                 footer: bool = True):
        super().__init__()
        self.title = title
        # A callable subject, for a headline that has to survive the command
        # the screen just ran. `dashboard` says "1% not started" next to a
        # pane that has just been re-read after a sitting; held as a string it
        # would be the one thing on screen still quoting the old number.
        self._subject = subject
        self.tabs = tabs
        self.idx = max(0, min(start, len(tabs) - 1))
        self.offset = 0
        self._cache: dict[int, Pane] = {}
        self._width = -1
        self._tab_spans: list[tuple[int, int, int]] = []
        self.hover_tab: int | None = None
        # `show` drives its own prompt below the card (`[p] prev · [d] drill
        # it · ...`) and folds a tab hint into that same line, so the frame's
        # own auto-footer would just be a second, differently-styled row
        # saying an overlapping thing right next to it -- that is the "double
        # hints" a command with its own prompt does not want.
        self._footer_enabled = footer
        # The action cursor, and the row it is waiting on a second press for.
        # Both are per-tab state, cleared on the way into a different tab: an
        # armed row you tabbed away from is an armed row you have forgotten.
        self.sel = 0
        self._armed: str | None = None
        self.hover_row: int | None = None

    @property
    def name(self) -> str:
        return self.tabs[self.idx][0] if self.tabs else ""

    @property
    def subject(self) -> str:
        return self._subject() if callable(self._subject) else self._subject

    def on_resume(self) -> None:
        """Every pane is now one command out of date, so drop all of them.

        A pane is a closure and the cache is keyed on the tab, so throwing the
        whole cache away costs one rebuild of the tab you are looking at and
        nothing at all for the four you are not.
        """
        self._cache.clear()
        self._armed = None

    def pane(self, width: int) -> Pane:
        if width != self._width:
            self._cache.clear()
            self._width = width
        if self.idx not in self._cache:
            built = self.tabs[self.idx][1](width)
            self._cache[self.idx] = (built if isinstance(built, Pane)
                                     else Pane(built))
        return self._cache[self.idx]

    def actions(self, width: int = 0) -> Sequence[Action]:
        """The current pane's actions. Width only matters for building it, and
        a key can arrive before the first render has said how wide that is."""
        return self.pane(width if width > 0 else ui.width()).actions

    def _compose(self, width: int) -> tuple[list[str], list[int]]:
        """The pane and its action rows as one line list, plus where they sit.

        Composed rather than drawn separately so the scroll window has one
        thing to window over: an action block pinned below a scrolling pane
        would be the only row on the screen that cannot be scrolled off, and
        on a short terminal it would eat the pane it is about.
        """
        p = self.pane(width)
        lines = list(p.lines)
        at: list[int] = []
        if p.actions:
            lines.append("")
            lines.append("  " + ui.head("DO THIS NEXT"))
            for i, act in enumerate(p.actions):
                at.append(len(lines))
                chosen = i == self.sel
                line = action_row(act.label, chosen,
                                  act.arm if self._armed == act.key else "",
                                  width=width)
                if i == self.hover_row and not chosen:
                    # Same split as every other list: the bar is what a key
                    # would take, the surface is what a click would.
                    line = ui.wash(line, "hover", width)
                lines.append(line)
        return lines, at

    def _tabbar(self, width: int) -> str:
        """The tab strip, and where each tab sits so a click can find it.

        Five tabs share one line, so the row a click lands on is not enough to
        say which tab was meant -- the column is the whole answer. The spans
        are recorded here rather than recomputed in the click handler, because
        two pieces of code measuring styled text the same way is one piece of
        code too many.
        """
        cells = []
        spans: list[tuple[int, int, int]] = []
        col = 2                                   # the leading "  "
        for i, (name, _) in enumerate(self.tabs):
            if i == self.idx:
                cell = ui.chip(name)
            elif i == self.hover_tab:
                # Lit, not chipped: the chip means "you are here", and a hover
                # that borrowed it would make two tabs look open at once.
                cell = " " + paint(name, "accent") + " "
            else:
                cell = " " + faint(name) + " "
            cells.append(cell)
            spans.append((col, col + vlen(cell), i))
            col += vlen(cell) + 1                 # + the joining space
        self._tab_spans = spans
        bar = "  " + " ".join(cells)
        right = faint(f"{self.idx + 1}/{len(self.tabs)}")
        gap = max(1, width - vlen(bar) - vlen(right))
        return bar + " " * gap + right

    TAB_BAR = 0          # the item index the tab strip answers to
    # Action rows number from 1, because the shell only routes a click to an
    # item index of 0 or more and the tab strip already owns 0.
    ACTION_BASE = 1

    def render(self, width: int) -> list[str]:
        self.owner = []
        out = [""]
        left = "  " + ui.head(self.title) + (
            "  " + paint(self.subject, "mauve", BOLD) if self.subject else "")
        # `show` hands the whole question in as the subject, and the longest
        # one in the bank is 539 characters. Unclamped it wrapped, and every
        # row of the card below it moved down by four.
        out.append(truncate(left, width))
        tabbar_row = len(out)
        out.append(truncate(self._tabbar(width), width))
        out.append("  " + ui.hairline(width - 2))

        pane, at = self._compose(width)
        body = max(3, self.viewport - len(out) - 1)
        self.offset = max(0, min(self.offset, max(0, len(pane) - body)))
        if at:
            # The cursor is on a row, so the row has to be on the screen. The
            # action block is at the bottom of the pane, which on a short
            # terminal is exactly the part the offset had scrolled away.
            line = at[min(self.sel, len(at) - 1)]
            self.offset = max(self.offset, line - body + 1)
            self.offset = min(self.offset, line)
            self.offset = max(0, self.offset)
        head_rows = len(out)
        window = pane[self.offset:self.offset + body]
        # Clamped here rather than trusted from each pane builder, for the
        # reason `PickerView.put` gives: a line one cell too long wraps in the
        # terminal, pushes the input box off the bottom of the frame and tears
        # the screen two panels away from whatever composed it. `_side_by_side`
        # learned this for the compare columns; every other pane was still
        # handing its lines straight to the frame, and `show`'s Sources pane
        # was quietly 62 cells over on the widest source in the bank.
        out.extend(truncate(line, width) for line in window)
        hidden = len(pane) - self.offset - len(window)
        # A pane drawn to fit its contents is a pane that moves. The frame is
        # anchored to the bottom of the terminal, so tabbing from a seventeen
        # row pane to a two row one slid the tab bar seventeen rows up the
        # screen -- under the ◂ ▸ the reader was in the middle of pressing.
        # `BrowseView` already pins its pane to the full body for exactly this
        # reason. Padding to `body` and keeping the scroll indicator's row
        # whether or not it has anything to say makes every tab on a screen
        # exactly as tall as every other, which is what holds the tab bar
        # still.
        out.extend([""] * max(0, body - len(window)))
        bits = []
        if self.offset:
            bits.append(f"↑ {self.offset} above")
        if hidden > 0:
            bits.append(f"↓ {hidden} below")
        out.append("  " + faint("   ".join(bits)) if bits else "")
        self.owner = [-1] * len(out)
        self.owner[tabbar_row] = self.TAB_BAR
        # An action row answers to its own item index so a click can land on
        # it. -1 is "chrome, ignore" and 0 is the tab strip, so they start at 1.
        for i, line in enumerate(at):
            row = head_rows + line - self.offset
            if head_rows <= row < head_rows + len(window):
                self.owner[row] = self.ACTION_BASE + i
        return out

    def _go_tab(self, idx: int) -> None:
        """Open a tab, and put down anything the last one was holding.

        An armed row you tabbed away from is an armed row you have forgotten
        about, and it would still be armed when you tabbed back.
        """
        self.idx = idx % len(self.tabs)
        self.offset = 0
        self.sel = 0
        self._armed = None

    def hover_at(self, item: int | None, col: int) -> bool:
        tab = None
        if item == self.TAB_BAR:
            tab = next((i for start, end, i in self._tab_spans
                        if start <= col < end), None)
        row = (item - self.ACTION_BASE if item is not None
               and item >= self.ACTION_BASE else None)
        if tab == self.hover_tab and row == self.hover_row:
            return False
        self.hover_tab, self.hover_row = tab, row
        return True

    def click_at(self, item: int, col: int, shell) -> bool:
        """A tab opens on the click it lands on; an action still takes three.

        Opening a tab is free and reversible, so it happens on the first
        click. An action is neither, and it keeps the same deliberate route it
        has in every other list: click to select it, click to arm it, click to
        run it.
        """
        if item == self.TAB_BAR:
            for start, end, i in self._tab_spans:
                if start <= col < end:
                    if i != self.idx:
                        self._go_tab(i)
                    return True
            return True
        acts = self.actions(self._width)
        row = item - self.ACTION_BASE
        if not 0 <= row < len(acts):
            return False
        if row != self.sel:
            self.sel = row
            self._armed = None
            return True
        return fire(self, acts[row], shell)

    def handle(self, key: Key, shell) -> bool:
        n = key.name
        acts = self.actions(self._width)
        if acts and self._armed and not (n == "enter"
                                         and acts[self.sel].key == self._armed):
            # Anything that is not the same key again backs out, and the key
            # that backed out is spent doing so.
            self._armed = None
            if n in ("left", "right"):
                return True
        if n in ("right", "btab"):
            self._go_tab(self.idx + 1)
        elif n == "left":
            self._go_tab(self.idx - 1)
        elif n == "enter" and acts:
            return fire(self, acts[self.sel], shell)
        elif n == "down":
            # With actions on the pane the cursor is what ↑↓ move; the render
            # scrolls to keep it in view, so the pane still travels. Only a
            # pane with nothing to do falls back to scrolling on its own.
            if acts and self.sel < len(acts) - 1:
                self.sel += 1
            elif acts:
                self.offset += 1
            else:
                self.offset += 1
        elif n == "up":
            if acts and self.sel > 0:
                self.sel -= 1
            else:
                self.offset = max(0, self.offset - 1)
        elif n == "pgdn":
            self.offset += max(1, self.viewport - 6)
        elif n == "pgup":
            self.offset = max(0, self.offset - max(1, self.viewport - 6))
        elif n == "home":
            self.offset = 0
            self.sel = 0
        elif n == "end":
            self.offset = 10_000
            if acts:
                self.sel = len(acts) - 1
        else:
            return False
        return True

    def scroll_by(self, delta: int) -> bool:
        """Scroll the pane, and hand the wheel back at its top and bottom."""
        pane = self._cache.get(self.idx)
        limit = max(0, len(pane.lines) - 1) if pane else 0
        target = max(0, min(limit, self.offset + delta))
        if target == self.offset:
            return False
        self.offset = target
        return True

    def footer(self) -> str:
        if not self._footer_enabled:
            return ""
        # A keymap is the one piece of documentation a reader tests by pressing
        # the key, so a screen with a single tab does not advertise `◂ ▸`:
        # `help` is one tab and offered a key that moves nothing.
        tab = [("◂ ▸", "tab")] if len(self.tabs) > 1 else []
        keys = tab + [("↑↓", "scroll"), ("esc", "done")]
        if self.actions(self._width):
            if self._armed:
                return _keyline([("⏎", "yes, do it"), ("←→", "back out")])
            keys = tab + [("↑↓", "move"), ("⏎⏎", "run it"), ("esc", "done")]
        return _keyline(keys)

    def flatten(self, width: int) -> list[str]:
        """Print every pane, since a printed tab bar you cannot click is a lie.

        An action becomes the command it would have run. Outside the shell
        there is nothing to press, and the thing you actually want is the line
        to type -- which is what the dashboard printed in grey before any of
        these were pressable.
        """
        out = [""]
        out.append(truncate("  " + ui.head(self.title) + (
            "  " + paint(self.subject, "mauve", BOLD) if self.subject else ""), width))
        for i, (name, build) in enumerate(self.tabs):
            out.append("")
            out.append(group_rule(name, width))
            built = build(width)
            p = built if isinstance(built, Pane) else Pane(built, [])
            out.extend(p.lines)
            if p.actions:
                out.append("")
                out.append("  " + ui.head("DO THIS NEXT"))
                for act in p.actions:
                    out.append("    " + act.label
                               + faint("   " + act.line.strip()))
        return out


# ---------------------------------------------------------------- tags


class TagsView(PickerView):
    """The tag map, with the questions under each tag one keypress away."""

    empty_text = "no tags yet - `autotag` builds them from the taxonomy"

    def __init__(self, conn: sqlite3.Connection, rows: list[dict]):
        # Two bare adjacent integers -- `#dcf  74  72` -- are not readable as
        # "how many questions" and "how many of them are due". The dashboard's
        # mastery table already labels its columns; this one was the list
        # asking you to infer them. Spaced to the row above's own layout: 4 of
        # lead, a 26-cell name, then the two counts right-aligned in 4 each.
        super().__init__(title="TAGS", tally=_plural(len(rows), "tag"),
                         note="  " + pad("tag", 26)
                              + f"{'size':>4}{'due':>4}   mastery")
        self.conn = conn
        self.rows = [dict(r) for r in rows]
        self.sort = "size"

    def count(self) -> int:
        return len(self.rows)

    def row(self, idx: int, width: int, chosen: bool) -> str:
        r = self.rows[idx]
        bar = paint("▎", "accent") if chosen else " "
        caret = paint("▾" if idx in self.expanded else "▸",
                      "accent" if chosen else "faint")
        name = "#" + r["name"]
        name_cell = pad(paint(name, "accent" if chosen else "mauve",
                              BOLD if chosen else ""), 26)
        n = pad(faint(f"{r['n']:>4}"), 4)
        due = pad(ui.warn(f"{r['due'] or 0:>4}") if r.get("due")
                  else faint("   ·"), 4)
        m = analytics.mastery_frac(r["avg_rating"])
        mastery = (ui.meter(m, 12) + f" {m:>3.0%}" if m is not None
                   else faint("·" * 12 + "   -"))
        return f"{bar} {caret} {name_cell}{n}{due}   {mastery}"

    def detail(self, idx: int, width: int) -> list[str]:
        name = self.rows[idx]["name"]
        gutter = paint("  │  ", "line")
        out = []
        qs = self.conn.execute(
            "SELECT q.id, q.canonical_text, q.topic FROM questions q "
            "JOIN question_tags qt ON qt.question_id = q.id "
            "JOIN tags t ON t.id = qt.tag_id "
            "WHERE t.name = ? AND q.status = 'active' ORDER BY q.id LIMIT 12",
            (name,)).fetchall()
        for q in qs:
            line = (paint(f"#{q['id']}", "sky") + "  "
                    + pad(faint((q["topic"] or "-")[:11]), 11) + " "
                    + ui.paint(_one_line(q["canonical_text"]), "text"))
            out.append(gutter + truncate(line, width - 8))
        if not qs:
            out.append(gutter + faint("nothing active under this tag"))
        elif self.rows[idx]["n"] > len(qs):
            out.append(gutter + faint(f"… {self.rows[idx]['n'] - len(qs)} more"))
        out.append(gutter)
        out.append(gutter + faint(f"← for what you can do with #{name}"
                                  f" · or type  drill --tag {name}"))
        return out

    def actions(self, idx: int) -> list[Action]:
        if idx >= len(self.rows):
            return []
        r = self.rows[idx]
        name = r["name"]
        due = r.get("due") or 0
        count = faint(f"   {due} due" if due else f"   {r['n']} questions, none due yet")
        return [
            Action("drill", f"drill #{name}" + count, f"drill --tag {name}",
                   arm=f"drill #{name}? ⏎ starts the sitting · ← backs out"),
            Action("browse", f"browse #{name}" + faint("   stack more filters on it"),
                   f"browse --tag {name}"),
        ]

    def action_subject(self, idx: int) -> str:
        return "#" + self.rows[idx]["name"] if idx < len(self.rows) else ""

    def extra_keys(self, key: Key, shell) -> bool:
        if key.name == "alt-d" and self.rows:
            shell.run_now(f"drill --tag {self.rows[self.sel]['name']}")
            return True
        if key.name == "btab":
            self.sort = {"size": "mastery", "mastery": "name", "name": "size"}[self.sort]
            if self.sort == "size":
                self.rows.sort(key=lambda r: -r["n"])
            elif self.sort == "mastery":
                # Never-drilled sorts last: "unknown" is not "weak", and
                # putting it first would bury the tags you are actually bad at.
                self.rows.sort(key=lambda r: (r["avg_rating"] is None,
                                              r["avg_rating"] or 0))
            else:
                self.rows.sort(key=lambda r: r["name"])
            self.invalidate()
            self.tally = _plural(len(self.rows), "tag") + "  ·  by " + self.sort
            return True
        return False

    def hints(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "expand"), ("←", "drill it, browse it"),
                ("⇧⇥", "sort"), ("esc", "done")]


# ---------------------------------------------------------------- sittings


class SessionsView(PickerView):
    """Past sittings, and the one you can pick back up."""

    empty_text = "no sittings yet - `drill` starts one"

    def __init__(self, conn: sqlite3.Connection, rows: list[dict],
                 open_id: int | None):
        super().__init__(title="SITTINGS", tally=_plural(len(rows), "sitting"))
        self.conn = conn
        self.rows = [dict(r) for r in rows]
        self.open_id = open_id

    def count(self) -> int:
        return len(self.rows)

    def row(self, idx: int, width: int, chosen: bool) -> str:
        s = self.rows[idx]
        bar = paint("▎", "accent") if chosen else " "
        caret = paint("▾" if idx in self.expanded else "▸",
                      "accent" if chosen else "faint")
        live = self.open_id is not None and s["id"] == self.open_id
        sid = pad(paint(f"#{s['id']}", "accent" if chosen else "sky"), 5)
        when = pad(faint(s["started_at"][:16].replace("T", " ")), 17)
        kind = pad(paint(s["kind"], "mauve"), 7)
        done = pad(ui.paint(f"{s['done']:>3} done", "text"), 10)
        avg = pad(faint(f"{s['avg_rating']:.2f}/4") if s["avg_rating"]
                  else faint("  -   "), 9)
        mins = pad(faint(f"{int((s['seconds'] or 0) // 60):>3}m"), 5)
        state = (ui.warn(f"{s['left']} left") if live and s["left"]
                 else faint("done"))
        return f"{bar} {caret} {sid}{when}{kind}{done}{avg}{mins} {state}"

    def detail(self, idx: int, width: int) -> list[str]:
        s = self.rows[idx]
        gutter = paint("  │  ", "line")
        out = []
        items = s.get("done_items") or []
        texts = self._texts([d["id"] for d in items[:12]])
        for d in items[:12]:
            out.append(gutter + truncate(
                verdict_mark(d.get("rating")) + " "
                + paint(f"#{d['id']}", "sky") + "  "
                + ui.paint(texts.get(d["id"], "(gone from the bank)"), "text"),
                width - 8))
        if len(items) > 12:
            out.append(gutter + faint(f"… {len(items) - 12} more"))
        if not items:
            out.append(gutter + faint("nothing answered in this sitting yet"))
        live = self.open_id is not None and s["id"] == self.open_id
        out.append(gutter)
        out.append(gutter + faint("← for what you can do with this sitting"
                                  + (" · it is the one you can resume" if live else "")))
        return out

    def _texts(self, ids: list[int]) -> dict[int, str]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        return {r["id"]: _one_line(r["canonical_text"]) for r in self.conn.execute(
            f"SELECT id, canonical_text FROM questions WHERE id IN ({marks})", ids)}

    def _drillable(self, ids: list[int]) -> list[int]:
        """Which of these the scheduler would actually ask right now.

        A sitting's questions are mostly not due the moment it ends -- that is
        what rating one of them "again" did to its schedule -- so an action
        offering to drill them would answer "0 queued, 12 held back" and read
        as broken. The count in the label is the real one, and an action with
        nothing behind it is not offered at all.
        """
        if not ids:
            return []
        return [r["id"] for r in
                scheduler.due_questions(self.conn, limit=len(ids), ids=ids)]

    def actions(self, idx: int) -> list[Action]:
        if idx >= len(self.rows):
            return []
        s = self.rows[idx]
        items = s.get("done_items") or []
        out: list[Action] = []
        if self.open_id is not None and s["id"] == self.open_id and s["left"]:
            out.append(Action(
                "resume", f"resume it" + faint(f"   {s['left']} still queued"),
                "drill --resume",
                arm="pick it back up? ⏎ resumes the sitting · ← backs out"))
        fluffed = self._drillable(
            [d["id"] for d in items if d.get("rating") and d["rating"] <= 2])
        if fluffed:
            ids = ",".join(str(i) for i in fluffed)
            out.append(Action(
                "fluffed", f"drill the {len(fluffed)} you rated 1 or 2",
                f"drill --ids {ids}",
                arm=f"drill those {len(fluffed)}? ⏎ starts the sitting · ← backs out"))
        again = self._drillable([d["id"] for d in items])
        if again and len(again) != len(fluffed):
            ids = ",".join(str(i) for i in again)
            out.append(Action(
                "again", f"drill all {len(again)} of these that are due again",
                f"drill --ids {ids}",
                arm=f"drill those {len(again)}? ⏎ starts the sitting · ← backs out"))
        return out

    def action_subject(self, idx: int) -> str:
        return f"sitting #{self.rows[idx]['id']}" if idx < len(self.rows) else ""

    def extra_keys(self, key: Key, shell) -> bool:
        if key.name == "alt-r" and self.open_id is not None:
            shell.run_now("drill --resume")
            return True
        return False

    def hints(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "expand"), ("←", "resume it, re-drill it"),
                ("esc", "done")]


RATING_NAME = {1: "again", 2: "hard", 3: "good", 4: "easy"}


def verdict_mark(rating: int | None) -> str:
    if rating is None:
        return faint("  ·")
    name = {1: "coral", 2: "gold", 3: "mint", 4: "mint"}.get(rating, "faint")
    return paint(f"{rating:>3}", name)


# ---------------------------------------------------------------- providers


class ProvidersView(PickerView):
    """The three vendors side by side: who answers now, and what switching costs.

    Every LLM setting in the tool is *relative to one of these rows* -- the
    four model defaults, whether `find --semantic` can run at all, which key a
    failure tells you to check -- and they were spread down a settings table
    that listed them as eight unrelated keys. Read there, "model_grade:
    gemini-3.5-flash" and "anthropic_api_key: set" are two true facts that
    do not add up to "switching to Claude would work".

    A provider with no key is still a row. It is the one you are trying to
    configure, and a list that hides it answers the question by omission.
    """

    empty_text = "no providers - which cannot happen, the table is in llm.py"

    def __init__(self, rows: list[dict]):
        super().__init__(title="LLM PROVIDERS",
                         subject=f"{llm.provider_label()} is answering")
        self.rows = rows
        # Open on the one that is answering, cursor on it. The first question
        # this screen is asked is "what am I running", and the answer to that
        # is the four model names inside the row rather than the row itself.
        self.sel = next((i for i, r in enumerate(rows) if r["active"]), 0)
        self.expanded = {self.sel}

    def on_resume(self) -> None:
        """Re-read after a command. Switching provider from this very screen is
        the common case, so a cached row list would show the old answer to the
        question it was just used to change."""
        self.rows = llm.overview()
        self.subject = f"{llm.provider_label()} is answering"
        self.invalidate()

    def count(self) -> int:
        return len(self.rows)

    def row(self, idx: int, width: int, chosen: bool) -> str:
        r = self.rows[idx]
        bar = paint("▎", "accent") if chosen else " "
        caret = paint("▾" if idx in self.expanded else "▸",
                      "accent" if chosen else "faint")
        # Filled for the one answering, hollow for the rest. Colour says the
        # same thing, but `flatten` prints this list into a pipe with the
        # colour stripped, and a screen that names the active provider only in
        # a shade is a screen that names it nowhere.
        dot = (paint("●", "mint") if r["active"]
               else paint("○", "sky" if r["key_set"] else "faint"))
        name = pad(paint(r["label"], "accent" if chosen else "text",
                         BOLD if r["active"] else ""), 9)
        key = pad(ui.ok("key …" + r["key_tail"]) if r["key_set"]
                  else faint("no key"), 12)
        model = pad(paint(r["models"]["grade"], "mauve"), 22)
        emb = pad(faint("embeds") if r["embeds"] else faint("no embeddings"), 15)
        if r["failed_today"]:
            spend = ui.bad(f"{r['failed_today']} failed")
        elif r["calls_today"]:
            spend = faint(f"{r['calls_today']} today")
        else:
            spend = faint("·")
        return f"{bar} {caret} {dot} {name}{key}{model}{emb}{spend}"

    def detail(self, idx: int, width: int) -> list[str]:
        r = self.rows[idx]
        gutter = paint("  │  ", "line")
        out = []
        for job in llm.JOBS:
            model = r["models"][job]
            line = pad(faint(job), 9) + (paint(model, "mauve") if model
                                         else faint("- none sold -"))
            if job in r["stale"]:
                # A setting that stopped applying and still reads as set is
                # worse than one that never existed.
                line += "  " + ui.warn(f"ignoring IB_MODEL_{job.upper()}="
                                       f"{r['stale'][job]}, which is not a "
                                       f"{r['label']} model")
            out.append(gutter + truncate(line, width - 8))
        out.append(gutter)
        out.append(gutter + pad(faint("key"), 9)
                   + (ui.ok(f"{r['key_env']} set (…{r['key_tail']})") if r["key_set"]
                      else ui.warn(f"{r['key_env']} not set")))
        out.append(gutter + pad(faint("console" if r["key_set"] else "get one"), 9)
                   + paint(r["console"], "sky"))
        out.append(gutter + pad(faint("limits"), 9) + paint(r["limits"], "sky"))
        if r["calls_today"] or r["failed_today"]:
            spend = (f"{r['calls_today']} calls, {r['failed_today']} failed, "
                     f"{r['tokens_today']:,} tokens")
            out.append(gutter + pad(faint("today"), 9) + ui.paint(spend, "text"))
        if r["last_failure"]:
            out.append(gutter + pad(faint("last"), 9)
                       + truncate(ui.bad(r["last_failure"]), width - 20))
        out.append(gutter)
        can = ["test the key" if r["key_set"] else "set a key"]
        if not r["active"]:
            can.insert(0, "switch to it")
        out.append(gutter + faint("← to " + ", ".join(can)))
        return out

    def actions(self, idx: int) -> list[Action]:
        if idx >= len(self.rows):
            return []
        r = self.rows[idx]
        out: list[Action] = []
        key_action = Action(
            "key", ("replace" if r["key_set"] else "set") + f" the {r['label']} key"
            + faint(f"   {r['console']}"),
            f"settings {r['setting']} ", prefill=True)
        # With no key, setting one is the only thing here that leads anywhere,
        # so it is the row the cursor lands on. Switching to a provider you
        # cannot call is a legal thing to want and stays offered underneath.
        if not r["key_set"]:
            out.append(key_action)
        if not r["active"]:
            note = ("" if r["key_set"]
                    else faint("   you still need a key for it"))
            out.append(Action("use", f"use {r['label']} for everything" + note,
                              f"settings llm_provider {r['name']}"))
        if r["key_set"]:
            # The only way to find out whether a key works is to spend a call
            # with it, so this one is armed like anything else that costs
            # something: an action a keystroke from where the cursor rests has
            # to cost a second, identical keystroke.
            out.append(Action(
                "test", f"test the {r['label']} key" + faint("   one small call"),
                f"llm --test {r['name']}",
                arm=f"spend one call on {r['label']}? ⏎ tests it · ← backs out"))
            out.append(key_action)
        return out

    def action_subject(self, idx: int) -> str:
        return self.rows[idx]["label"] if idx < len(self.rows) else ""

    def hints(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "expand"),
                ("←", "switch, test, set a key"), ("esc", "done")]


# ---------------------------------------------------------------- cross-audit


class DisagreementsView(PickerView):
    """Where the two auditors disagree, worst first.

    Worst means: the first pass kept it and the second says it is wrong,
    because that is the case that quietly teaches you a wrong answer.

    Both sides are named from the rows rather than in the prose. While one
    vendor gave every first opinion and another gave every second, writing
    "Gemini" and "Claude" here cost nothing; it becomes a caption that names
    the wrong model the first time either setting moves.
    """

    empty_text = "the two auditors agree on everything checked so far"

    def __init__(self, conn: sqlite3.Connection, rows: list[dict], checked: int):
        super().__init__(
            title="DISAGREEMENTS",
            note="worst first: the first pass let it in, the second says it is wrong",
            tally=f"{len(rows)} of {checked} cross-audited")
        self.conn = conn
        self.rows = [dict(r) for r in rows]

    def count(self) -> int:
        return len(self.rows)

    def row(self, idx: int, width: int, chosen: bool) -> str:
        r = self.rows[idx]
        worst = crossaudit.severity(r["g_verdict"], r["c_verdict"]) == 0
        bar = paint("▎", "accent") if chosen else " "
        caret = paint("▾" if idx in self.expanded else "▸",
                      "accent" if chosen else "faint")
        mark = ui.bad("!!") if worst else faint("  ")
        qid = pad(paint(f"#{r['id']}", "accent" if chosen else "sky"), 6)
        move = pad(ui.verdict(r["g_verdict"]) + faint(" → ")
                   + ui.verdict(r["c_verdict"]), 22)
        conf = pad(faint(f"{r['c_confidence'] or 0:.2f}"), 5)
        prefix = f"{bar} {caret} {mark} {qid} {move}{conf} "
        text_w = max(12, width - vlen(prefix) - 1)
        text = _one_line(r["canonical_text"])
        return prefix + truncate(ui.style(text, BOLD) if chosen
                                 else ui.paint(text, "text"), text_w)

    def detail(self, idx: int, width: int) -> list[str]:
        r = self.rows[idx]
        gutter = paint("  │  ", "line")
        out = []
        second = llm.label_for(r.get("c_provider")) or "the second pass"
        if r["c_reason"]:
            for line in ui.wrap(f"{second}: " + r["c_reason"], "",
                                width - 8).split("\n"):
                out.append(gutter + ui.paint(line, "text"))
        if r.get("corrected_answer"):
            out.append(gutter)
            for line in ui.wrap(f"{second} would say: " + r["corrected_answer"],
                                "", width - 8).split("\n")[:8]:
                out.append(gutter + faint(line))
        out.append(gutter)
        out.append(gutter + faint(f"status {r['status']} · show {r['id']} for everything"
                                  " · disagreements -r to decide them"))
        return out

    def actions(self, idx: int) -> list[Action]:
        if idx >= len(self.rows):
            return []
        qid = self.rows[idx]["id"]
        # No drill here on purpose: a question the two auditors disagree about
        # is held out of every sitting until someone decides it, so offering
        # to drill it would be offering something the scheduler will refuse.
        return [
            Action("show", f"show #{qid}" + faint("   both verdicts in full"),
                   f"show {qid}"),
            Action("resolve", "decide these" + faint("   one at a time"),
                   "disagreements -r"),
        ]

    def action_subject(self, idx: int) -> str:
        return f"#{self.rows[idx]['id']}" if idx < len(self.rows) else ""

    def hints(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "expand"), ("←", "show it, decide them"),
                ("esc", "done")]


# ---------------------------------------------------------------- recap


def _clock(iso: str) -> str:
    return (iso or "")[11:16]


def _day_label(iso: str) -> str:
    day = (iso or "")[:10]
    today = datetime.now(timezone.utc).date()
    try:
        d = datetime.fromisoformat(day).date()
    except ValueError:
        return day
    delta = (today - d).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    if delta < 7:
        return d.strftime("%A").lower()
    return d.isoformat()


class RecapView(PickerView):
    """What you have already answered, and how it went.

    The list a drill used to leave behind in scrollback, except it is a list
    you can move through: the sitting itself now folds each answered question
    down to one line, and this is where the twenty lines went. Newest first,
    because the question you want to reread is almost always the last one.
    """

    empty_text = "nothing answered in this window - `drill` starts a sitting"

    def __init__(self, conn: sqlite3.Connection, rows: list[dict], *,
                 window: str, note: str = ""):
        graded = sum(1 for r in rows if r.get("score") is not None)
        avg = ([r["rating"] for r in rows if r.get("rating")])
        tally = _plural(len(rows), "answer")
        if avg:
            tally += f"   {sum(avg) / len(avg):.2f}/4"
        if graded:
            tally += f"   {graded} graded"
        super().__init__(title="RECAP", subject=window,
                         note=note or "newest first",
                         tally=tally)
        self.conn = conn
        self.rows = rows
        self.window = window

    def count(self) -> int:
        return len(self.rows)

    def group_of(self, idx: int) -> str | None:
        return _day_label(self.rows[idx]["asked_at"])

    def row(self, idx: int, width: int, chosen: bool) -> str:
        r = self.rows[idx]
        bar = paint("▎", "accent") if chosen else " "
        caret = paint("▾" if idx in self.expanded else "▸",
                      "accent" if chosen else "faint")
        when = pad(faint(_clock(r["asked_at"])), 6)
        mark = pad(verdict_mark(r["rating"]) + " "
                   + faint(RATING_NAME.get(r["rating"] or 0, "")), 12)
        score = pad(paint(f"{r['score']:.0%}", "mauve") if r["score"] is not None
                    else faint("  ·  "), 6)
        qid = pad(paint(f"#{r['question_id']}", "accent" if chosen else "sky"), 6)
        topic = pad(faint((r["topic"] or "general")[:11]), 12)
        prefix = f"{bar} {caret} {when}{mark}{score}{qid}{topic}"
        text_w = max(12, width - vlen(prefix) - 1)
        text = _one_line(r["canonical_text"])
        return prefix + truncate(ui.style(text, BOLD) if chosen
                                 else ui.paint(text, "text"), text_w)

    def detail(self, idx: int, width: int) -> list[str]:
        r = self.rows[idx]
        gutter = paint("  │  ", "line")
        out = []
        for line in ui.wrap(_one_line(r["phrasing"] or r["canonical_text"]),
                            "", width - 8).split("\n"):
            out.append(gutter + ui.paint(line, "text"))
        out.append(gutter)
        if r.get("user_answer"):
            out.append(gutter + faint("you said"))
            for line in ui.wrap(_one_line(r["user_answer"]), "", width - 8).split("\n")[:10]:
                out.append(gutter + ui.paint(line, "text"))
        else:
            out.append(gutter + faint("revealed and self-rated - nothing typed"))
        hits = json.loads(r.get("rubric_hits") or "[]")
        if hits:
            out.append(gutter)
            out.append(gutter + faint(
                f"{sum(1 for h in hits if h)}/{len(hits)} rubric points hit"
                f" · graded by {r['grader']}"))
        out.append(gutter)
        out.append(gutter + faint(next_due_phrase(r["due_at"])))
        return out

    def actions(self, idx: int) -> list[Action]:
        if idx >= len(self.rows):
            return []
        r = self.rows[idx]
        qid = r["question_id"]
        out = [Action("show", f"open #{qid}" + faint("   the whole card"), f"show {qid}")]
        if scheduler.due_questions(self.conn, limit=1, ids=[qid]):
            out.append(Action(
                "again", "drill it again now", f"drill --ids {qid}",
                arm="drill it again? ⏎ starts the sitting · ← backs out"))
        fluffed = sorted({x["question_id"] for x in self.rows
                          if x["rating"] and x["rating"] <= 2})
        drillable = ([q["id"] for q in scheduler.due_questions(
            self.conn, limit=len(fluffed), ids=fluffed)] if fluffed else [])
        if drillable:
            ids = ",".join(str(i) for i in drillable)
            out.append(Action(
                "weak", f"drill the {len(drillable)} you rated 1 or 2 here",
                f"drill --ids {ids}",
                arm=f"drill those {len(drillable)}? ⏎ starts the sitting · ← backs out"))
        return out

    def action_subject(self, idx: int) -> str:
        return f"#{self.rows[idx]['question_id']}" if idx < len(self.rows) else ""

    def hints(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "what you said"),
                ("←", "drill it again, open it"), ("esc", "done")]


# ---------------------------------------------------------------- question lines


class ChainsView(PickerView):
    """Questions that only make sense after the one before them.

    Two things share this list, because they are two states of one decision:
    a line already recorded, and a candidate the scan turned up. The candidate
    rows carry the actions -- link it, or say it stands on its own.
    """

    empty_text = "no follow-ups found that are not already linked"

    def __init__(self, conn: sqlite3.Connection, rows: list[dict], *,
                 title: str, note: str = "", tally: str = ""):
        super().__init__(title=title, note=note, tally=tally)
        self.conn = conn
        self.rows = rows

    def count(self) -> int:
        return len(self.rows)

    def group_of(self, idx: int) -> str | None:
        return self.rows[idx].get("group")

    def row(self, idx: int, width: int, chosen: bool) -> str:
        r = self.rows[idx]
        bar = paint("▎", "accent") if chosen else " "
        caret = paint("▾" if idx in self.expanded else "▸",
                      "accent" if chosen else "faint")
        if r.get("tier"):
            state = pad(paint("certain", "coral") if r["tier"] == "certain"
                        else paint("likely", "gold"), 9)
        else:
            state = pad(faint("linked"), 9)
        qid = pad(paint(f"#{r['id']}", "accent" if chosen else "sky"), 6)
        after = pad(faint(f"↖ #{r['parent_id']}") if r.get("parent_id")
                    else ui.warn("orphan"), 9)
        prefix = f"{bar} {caret} {state}{qid}{after}"
        text_w = max(12, width - vlen(prefix) - 1)
        text = _one_line(r["text"])
        return prefix + truncate(ui.style(text, BOLD) if chosen
                                 else ui.paint(text, "text"), text_w)

    def detail(self, idx: int, width: int) -> list[str]:
        r = self.rows[idx]
        gutter = paint("  │  ", "line")
        out = []
        if r.get("parent_text"):
            label = "already follows" if r.get("linked") else "the question before it"
            out.append(gutter + faint(f"{label}  #{r['parent_id']}"))
            for line in ui.wrap(_one_line(r["parent_text"]), "", width - 8).split("\n")[:6]:
                out.append(gutter + ui.paint(line, "text"))
        else:
            out.append(gutter + ui.warn("nothing active came before it in its source"))
            out.append(gutter + faint("the lead-in was rejected or never extracted - "
                                      "this one needs its text rewritten, not a link"))
        if r.get("why"):
            out.append(gutter)
            out.append(gutter + faint("reads as a follow-up: " + "; ".join(r["why"])))
        return out

    def actions(self, idx: int) -> list[Action]:
        if idx >= len(self.rows):
            return []
        r = self.rows[idx]
        out: list[Action] = []
        if r.get("linked"):
            out.append(Action("unlink", f"unlink it from #{r['parent_id']}",
                              f"chains --unlink {r['id']}",
                              arm="unlink it? ⏎ drops the lead-in · ← backs out"))
        elif r.get("parent_id"):
            out.append(Action("link", f"link it after #{r['parent_id']}",
                              f"chains --link {r['id']} {r['parent_id']}",
                              arm=f"link #{r['id']} after #{r['parent_id']}? "
                                  "⏎ records it · ← backs out"))
        if not r.get("linked"):
            out.append(Action("standalone", "it stands on its own"
                              + faint("   stops the scan asking"),
                              f"chains --standalone {r['id']}"))
        out.append(Action("show", f"open #{r['id']}" + faint("   the whole card"),
                          f"show {r['id']}"))
        return out

    def action_subject(self, idx: int) -> str:
        return f"#{self.rows[idx]['id']}" if idx < len(self.rows) else ""

    def hints(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "the question before it"),
                ("←", "link it, or clear it"), ("esc", "done")]


def tree_prefix(rails: list[bool], last: bool, depth: int) -> str:
    """The gutter in front of one node of a tree, from `chains.graph`'s shape.

    Drawn from `ui.SQUARE` rather than from characters spelled here, so a tree
    in the shell and the same tree printed to a pipe come out of one set of
    glyphs -- which is the reason those live in `ui.py` at all.
    """
    if depth == 0:
        return ""
    gutter = "".join((ui.SQUARE["v"] + "  ") if rail else "   " for rail in rails)
    branch = (ui.SQUARE["bl"] if last else ui.SQUARE["lt"]) + ui.SQUARE["h"]
    return faint(gutter + branch) + " "


class ChainGraphView(PickerView):
    """One question line, whole: up to its lead-in and down through the lot.

    `ChainsView` is the review queue - one row per pair, because what it is
    asking is "does this one follow that one". This answers a different
    question: what does the run I am standing in actually look like. Those are
    not the same list, because a line forks. An interviewer sets up a scenario
    and asks three separate things about it, and from inside any one of the
    three the other two are invisible - `lead_in` walks a single ancestry, so
    nothing in `drill` or `show` has ever been able to show a branch.

    Read-only about the shape: nothing here links or unlinks, because a tree
    is where you find out a link is wrong and `chains` is where you say so.
    The row you came in on is marked, since a line of eight all rendered the
    same way does not tell you which one you asked about.
    """

    empty_text = "no question by that id"

    def __init__(self, conn: sqlite3.Connection, rows: list[dict], qid: int):
        forks = sum(1 for r in rows if not r["last"] or r["depth"] > 1)
        super().__init__(
            title="QUESTION LINE", subject=f"#{qid}",
            note="each one is asked after the one it hangs from"
                 + ("  ·  this line forks" if forks else ""),
            tally=_plural(len(rows), "question"))
        self.conn = conn
        self.rows = rows
        self.qid = qid

    def count(self) -> int:
        return len(self.rows)

    def row(self, idx: int, width: int, chosen: bool) -> str:
        r = self.rows[idx]
        bar = paint("▎", "accent") if chosen else " "
        caret = paint("▾" if idx in self.expanded else "▸",
                      "accent" if chosen else "faint")
        dot = status_dot(r["status"] or "active")
        qid = pad(paint(f"#{r['id']}", "accent" if chosen else "sky"), 6)
        here = paint("▸", "mauve") if r["target"] else " "
        prefix = (f"{bar} {caret} {here} {dot} {qid}"
                  + tree_prefix(r["rails"], r["last"], r["depth"]))
        text_w = max(12, width - vlen(prefix) - 1)
        text = _one_line(r["text"])
        if r["target"]:
            return prefix + truncate(paint(text, "text", BOLD), text_w)
        return prefix + truncate(ui.style(text, BOLD) if chosen
                                 else ui.paint(text, "text"), text_w)

    def detail(self, idx: int, width: int) -> list[str]:
        r = self.rows[idx]
        gutter = paint("  │  ", "line")
        body_w = width - 8
        row = self.conn.execute(
            "SELECT answer_key, rubric_points FROM answers WHERE question_id = ?",
            (r["id"],)).fetchone()
        out: list[str] = []
        answer = (row["answer_key"] if row else "") or ""
        if answer.strip():
            for line in ui.body(_soft(answer), "", body_w).split("\n"):
                out.append(gutter + ui.paint(line, "text"))
        else:
            out.append(gutter + faint("no answer on file yet"))
        points = json.loads((row["rubric_points"] if row else None) or "[]")
        if points:
            out.append(gutter)
            text = " · ".join(p if isinstance(p, str) else str(p.get("point", p))
                              for p in points[:4])
            for line in ui.wrap("must hit: " + text, "", body_w).split("\n"):
                out.append(gutter + faint(line))
        out.append(gutter)
        if r["parent_id"]:
            out.append(gutter + faint(f"asked after #{r['parent_id']}"))
        else:
            out.append(gutter + faint("this is where the line starts"))
        return out

    def actions(self, idx: int) -> list[Action]:
        if idx >= len(self.rows):
            return []
        qid = self.rows[idx]["id"]
        out = [Action("show", f"open #{qid}" + faint("   the whole card"),
                      f"show {qid}")]
        if scheduler.due_questions(self.conn, limit=1, ids=[qid]):
            out.append(Action(
                "drill", f"drill #{qid}", f"drill --ids {qid}",
                arm=f"drill #{qid}? ⏎ starts the sitting · ← backs out"))
        return out

    def action_subject(self, idx: int) -> str:
        return f"#{self.rows[idx]['id']}" if idx < len(self.rows) else ""

    def _toggle(self, idx: int, shell) -> None:
        if idx >= len(self.rows) or shell is None:
            return
        shell.run_now(f"show {self.rows[idx]['id']}")

    def hints(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "open"), ("→", "peek"),
                ("←", "drill it"), ("esc", "done")]


# ---------------------------------------------------------------- duplicates


def _wrap_spans(spans: list[tuple[str, bool]], width: int,
                lit: str = "gold") -> list[str]:
    """`dupes.diff_words` output laid out into styled lines of at most `width`.

    Wrapped from the spans rather than from the finished string, because the
    finished string is styled and every wrapper in the standard library counts
    escape bytes as columns. `ui.hard_wrap` counts cells correctly but breaks
    mid-word, which is right for a transcript line that must not tear the
    frame and wrong for a paragraph you are being asked to read closely.
    """
    lines: list[str] = []
    cur: list[str] = []
    used = 0
    for word, differs in spans:
        add = len(word) + (1 if cur else 0)
        if cur and used + add > width:
            lines.append(" ".join(cur))
            cur, used, add = [], 0, len(word)
        cur.append(paint(word, lit, BOLD) if differs else faint(word))
        used += add
    if cur:
        lines.append(" ".join(cur))
    return lines or [faint("(nothing on file)")]


def _col_width(width: int, gap: int = 3) -> int:
    return (width - gap - 4) // 2


def _side_by_side(left: list[str], right: list[str], width: int,
                  gap: int = 3) -> list[str]:
    """Two blocks in two columns, or stacked when the terminal is too narrow.

    Below about 76 cells each column is under 30 and a rubric point wraps to
    five lines apiece, at which point the columns are further from readable
    than the same two blocks one above the other.

    Both columns are truncated, not just the left. `ui.columns` clamps the
    left one because it has to -- the right border of a column is what the
    padding is measured against -- and leaves the right one whole, which is
    correct for a printout and wrong inside the shell: `TabsView` hands its
    pane lines to the frame unclamped, so one over-long right column tears
    the screen two panels away from the mistake.
    """
    col = _col_width(width, gap)
    if col < 30:
        out = [truncate(line, width - 4) for line in left]
        if left and right:
            out.append("")
        return out + [truncate(line, width - 4) for line in right]
    out = []
    for i in range(max(len(left), len(right))):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        # rstrip so a row whose right column ran out is not a line of trailing
        # spaces -- those survive into a drag-select copy as real characters.
        out.append(("  " + pad(truncate(l, col), col) + " " * gap
                    + truncate(r, col)).rstrip())
    return out


def _pair_facts(conn: sqlite3.Connection, qid: int) -> dict:
    """Everything a compare needs about one side of a pair, in one place."""
    q = conn.execute(
        "SELECT q.id, q.canonical_text, q.topic, q.subtopic, q.status, "
        "q.difficulty, q.kind, q.origin, q.created_at, q.parent_id, "
        "a.answer_key, a.rubric_points, a.common_mistakes "
        "FROM questions q LEFT JOIN answers a ON a.question_id = q.id "
        "WHERE q.id = ?", (qid,)).fetchone()
    if q is None:
        return {}
    rec = dict(q)
    rec["rubric_points"] = json.loads(q["rubric_points"] or "[]")
    rec["common_mistakes"] = json.loads(q["common_mistakes"] or "[]")
    rec["tags"] = tagging.tags_for(conn, qid)
    sched = conn.execute(
        "SELECT due_at, reps, lapses FROM schedule WHERE question_id = ?",
        (qid,)).fetchone()
    rec["schedule"] = dict(sched) if sched else None
    for name, sql in (
            ("reviews", "SELECT COUNT(*) FROM reviews WHERE question_id = ?"),
            ("notes", "SELECT COUNT(*) FROM notes WHERE question_id = ?"),
            ("phrasings", "SELECT COUNT(*) FROM phrasings WHERE question_id = ?"),
            ("sources", "SELECT COUNT(*) FROM question_sources WHERE question_id = ?")):
        rec[name] = conn.execute(sql, (qid,)).fetchone()[0]
    return rec


def _pair_headline(rec: dict, width: int) -> str:
    """One line naming a side: which question, and what state it is in."""
    if not rec:
        return ui.bad("gone from the bank")
    bits = [paint(f"#{rec['id']}", "sky", BOLD),
            paint(rec["status"], STATUS_DOT.get(rec["status"], "faint")),
            faint(rec["topic"] or "-")]
    return truncate("  ".join(bits), width)


def _pair_carries(rec: dict) -> str:
    """What this side would take with it, or lose, in a merge."""
    if not rec:
        return ""
    bits = []
    if rec["reviews"]:
        bits.append(_plural(rec["reviews"], "review"))
    if rec["schedule"] and rec["schedule"]["reps"]:
        bits.append(f"{rec['schedule']['reps']} reps")
    if rec["notes"]:
        bits.append(_plural(rec["notes"], "note"))
    if rec["tags"]:
        bits.append(_plural(len(rec["tags"]), "tag"))
    if rec["sources"]:
        bits.append(_plural(rec["sources"], "source"))
    return faint(" · ".join(bits) or "nothing recorded against it yet")


class DupesView(PickerView):
    """Near-duplicate pairs, closest first.

    This used to be a print-and-input loop that walked the pairs one at a
    time: it could only go forwards, it left every pair it had shown in
    scrollback, and the only way to see the second question was to have read
    past the first. It is a list like every other list now -- `→` peeks, `⏎`
    opens the two side by side, `←` is what you can do about it.

    Grouped by what the admission gate would have called the pair had it met
    it at ingest, because that is the difference that decides how hard you
    have to look: at 0.88 the gate would have merged them without asking.
    """

    empty_text = "nothing in the bank is close enough to anything else"

    def __init__(self, conn: sqlite3.Connection, rows: list[dict], *,
                 threshold: float, note: str = ""):
        super().__init__(title="DUPLICATES", note=note or "closest first")
        self.conn = conn
        self.rows = rows
        self.threshold = threshold

    @property
    def tally(self) -> str:
        return (f"{len(self.rows)} pair{'' if len(self.rows) == 1 else 's'}"
                f"  ·  at or above {self.threshold:.0%}")

    @tally.setter
    def tally(self, _v: str) -> None:
        pass

    def count(self) -> int:
        return len(self.rows)

    def on_resume(self) -> None:
        """A pair decided from the compare screen should not still read as
        open here -- that is exactly the "it still acts like I didn't
        decide" complaint, one screen up. Dropped rather than re-scanned:
        the scan is a whole-bank pass, and coming back from one decision
        should not pay for it.
        """
        if not self.rows:
            return
        ids = {p["a"]["id"] for p in self.rows} | {p["b"]["id"] for p in self.rows}
        marks = ",".join("?" * len(ids))
        live = {r["id"]: r["status"] for r in self.conn.execute(
            f"SELECT id, status FROM questions WHERE id IN ({marks})", list(ids))}
        done = dupes.settled(self.conn)
        self.rows = [p for p in self.rows
                     if dupes.key(p["a"]["id"], p["b"]["id"]) not in done
                     and live.get(p["a"]["id"]) != "rejected"
                     and live.get(p["b"]["id"]) != "rejected"]
        self.sel = max(0, min(self.sel, len(self.rows) - 1))
        self.top = 0
        self.expanded.clear()
        self.invalidate()

    def group_of(self, idx: int) -> str | None:
        kind = dupes.verdict_of(self.rows[idx]["similarity"])
        return ("the gate would have merged these"
                if kind == "duplicate" else "close enough to check")

    def row(self, idx: int, width: int, chosen: bool) -> str:
        p = self.rows[idx]
        a, b = p["a"], p["b"]
        bar = paint("▎", "accent") if chosen else " "
        caret = paint("▾" if idx in self.expanded else "▸",
                      "accent" if chosen else "faint")
        hot = p["similarity"] >= 0.88
        sim = pad(paint(f"{p['similarity']:.0%}", "coral" if hot else "gold",
                        BOLD), 5)
        ids = pad(paint(f"#{a['id']}", "accent" if chosen else "sky")
                  + faint(" ⇄ ") + paint(f"#{b['id']}",
                                         "accent" if chosen else "sky"), 16)
        topic = pad(faint((a["topic"] or "-")[:11]), 11)
        prefix = f"{bar} {caret} {sim} {ids}{topic} "
        text_w = max(12, width - vlen(prefix) - 1)
        text = _one_line(a["canonical_text"])
        return prefix + truncate(ui.style(text, BOLD) if chosen
                                 else ui.paint(text, "text"), text_w)

    def detail(self, idx: int, width: int) -> list[str]:
        p = self.rows[idx]
        gutter = paint("  │  ", "line")
        body_w = width - 8
        left, right = dupes.diff_words(_one_line(p["a"]["canonical_text"]),
                                       _one_line(p["b"]["canonical_text"]))
        out: list[str] = []
        for q, spans in ((p["a"], left), (p["b"], right)):
            out.append(gutter + paint(f"#{q['id']}", "sky")
                       + "  " + faint(q["status"]))
            for line in _wrap_spans(spans, body_w - 2):
                out.append(gutter + "  " + line)
        out.append(gutter)
        out.append(gutter + faint("lit words are where the two differ"))
        return out

    def actions(self, idx: int) -> list[Action]:
        if idx >= len(self.rows):
            return []
        p = self.rows[idx]
        a, b = p["a"]["id"], p["b"]["id"]
        return pair_actions(self.conn, a, b)

    def action_subject(self, idx: int) -> str:
        if idx >= len(self.rows):
            return ""
        p = self.rows[idx]
        return f"#{p['a']['id']} ⇄ #{p['b']['id']}"

    def _toggle(self, idx: int, shell) -> None:
        """Enter opens the compare, the same way it opens `show` in a result
        list: → is the free peek, ⏎ is the screen you actually decide on."""
        if idx >= len(self.rows) or shell is None:
            return
        p = self.rows[idx]
        shell.run_now(f"dupes --pair {p['a']['id']},{p['b']['id']}")

    def hints(self) -> list[tuple[str, str]]:
        return [("↑↓", "move"), ("⏎", "compare"), ("→", "peek"),
                ("←", "merge it, clear it"), ("esc", "done")]


def pair_actions(conn: sqlite3.Connection, a: int, b: int) -> list[Action]:
    """What you can do about one pair. One definition, two screens.

    The list and the compare offer exactly the same three decisions, so they
    are built here rather than twice: two lists that each spell out "fold #2
    into #1" are two lists that will eventually disagree about which id is
    which, and that mistake rejects the wrong question.
    """
    live = {r["id"]: r["status"] for r in conn.execute(
        "SELECT id, status FROM questions WHERE id IN (?, ?)", (a, b))}
    if any(live.get(q, "rejected") == "rejected" for q in (a, b)):
        # Already settled by a merge. Offering to merge it again would run a
        # second `set_status` batch that `undo` would then have to unwind
        # before it could reach the merge you actually meant.
        return [Action("show", f"open #{a}" + faint("   the whole card"), f"show {a}"),
                Action("show-b", f"open #{b}" + faint("   the whole card"), f"show {b}")]
    if dupes.key(a, b) in dupes.settled(conn):
        # Already settled as distinct. Offering "they are different
        # questions" again is what let one press of Enter look like it did
        # nothing -- the screen came back showing the same five options, so a
        # second Enter fired the same verdict a second time.
        return [Action("show", f"open #{a}" + faint("   the whole card"), f"show {a}"),
                Action("show-b", f"open #{b}" + faint("   the whole card"), f"show {b}"),
                Action("undistinct", "actually, these are the same question"
                       + faint("   clears the settled verdict"),
                       f"dupes --undistinct {a},{b}")]
    return [
        Action("keep-a", f"keep #{a}" + faint(f"   folds #{b} into it"),
               f"dupes --merge {a},{b}",
               arm=f"fold #{b} into #{a}? ⏎ merges them · ← backs out"),
        Action("keep-b", f"keep #{b}" + faint(f"   folds #{a} into it"),
               f"dupes --merge {b},{a}",
               arm=f"fold #{a} into #{b}? ⏎ merges them · ← backs out"),
        Action("distinct", "they are different questions"
               + faint("   stops the scan asking"),
               f"dupes --distinct {a},{b}"),
        Action("show", f"open #{a}" + faint("   the whole card"), f"show {a}"),
        Action("show-b", f"open #{b}" + faint("   the whole card"), f"show {b}"),
    ]


class ComparePairView(TabsView):
    """Two questions the scan thinks are the same one, facet by facet.

    A tab per facet rather than one long pane, for the reason `show` is built
    that way: the question, the answer, the rubric and the card are four
    separate things you can want to compare, and stacking all four means
    scrolling past three of them to reach the one that decides it.

    Each pane is two columns with the differing words lit, and drops to one
    column stacked when the terminal is too narrow for two to be readable.
    The three decisions ride on every pane, because whichever facet settled it
    is the facet you are looking at when you decide.
    """

    def __init__(self, conn: sqlite3.Connection, a: int, b: int, *,
                 similarity: float | None = None):
        self.conn = conn
        self.a, self.b = a, b
        self.similarity = similarity
        head = f"#{a} ⇄ #{b}"
        if similarity is not None:
            head += f"   {similarity:.0%} alike"
        super().__init__("COMPARE", [
            ("Question", lambda w: self._pane(w, self._question)),
            ("Answer", lambda w: self._pane(w, self._answer)),
            ("Rubric", lambda w: self._pane(w, self._rubric)),
            ("Card", lambda w: self._pane(w, self._card)),
        ], subject=head)

    # -- the two records, re-read on every build so a merge is visible
    def _sides(self) -> tuple[dict, dict]:
        return _pair_facts(self.conn, self.a), _pair_facts(self.conn, self.b)

    def _pane(self, width: int, build) -> Pane:
        left, right = self._sides()
        col = max(30, (width - 7) // 2)
        lines = ["",
                 *_side_by_side([_pair_headline(left, col)],
                                [_pair_headline(right, col)], width),
                 ""]
        lines += build(width, left, right)
        gone = [q["id"] for q in (left, right)
                if q and q["status"] == "rejected"]
        if gone:
            lines += ["", "  " + ui.warn(
                f"#{gone[0]} has been folded away - `undo` takes that back")]
        elif dupes.key(self.a, self.b) in dupes.settled(self.conn):
            lines += ["", "  " + ui.ok(
                "settled: these are different questions")]
        return Pane(lines, pair_actions(self.conn, self.a, self.b))

    def _question(self, width: int, left: dict, right: dict) -> list[str]:
        col = _col_width(width)
        la, lb = dupes.diff_words(_one_line(left.get("canonical_text", "")),
                                  _one_line(right.get("canonical_text", "")))
        out = _side_by_side(_wrap_spans(la, col), _wrap_spans(lb, col), width)
        return out + ["", "  " + faint("lit words are where the two differ")]

    def _answer(self, width: int, left: dict, right: dict) -> list[str]:
        col = _col_width(width)
        la, lb = dupes.diff_words(_one_line(left.get("answer_key") or ""),
                                  _one_line(right.get("answer_key") or ""))
        return _side_by_side(_wrap_spans(la, col), _wrap_spans(lb, col), width)

    def _rubric(self, width: int, left: dict, right: dict) -> list[str]:
        col = _col_width(width)

        def block(rec: dict) -> list[str]:
            points = rec.get("rubric_points") or []
            if not points:
                return [faint("no rubric on file")]
            out: list[str] = []
            for i, p in enumerate(points, 1):
                text = p if isinstance(p, str) else str(p.get("point", p))
                # Wrapped on words, not on cells. `ui.hard_wrap` counts cells
                # correctly and breaks wherever the count runs out, which is
                # right for a transcript line that must not tear the frame and
                # unreadable for a rubric point: it splits "Shareholders'"
                # across two lines in the middle of the word.
                wrapped = ui.wrap(ui.claim(text), "", max(12, col - 4)).split("\n")
                out.append(faint(f"{i}. ") + ui.paint(wrapped[0], "text"))
                out.extend("   " + ui.paint(line, "text") for line in wrapped[1:])
            return out

        return _side_by_side(block(left), block(right), width)

    def flatten(self, width: int) -> list[str]:
        """Every pane, but the decisions printed once at the end.

        `TabsView.flatten` prints each pane's actions under it, which is right
        where the panes offer different things -- the dashboard's do. Here all
        four offer the same three decisions, so the inherited version prints
        them four times and the printout is a third repetition.
        """
        out = [""]
        out.append(truncate("  " + ui.head(self.title) + (
            "  " + paint(self.subject, "mauve", BOLD) if self.subject else ""), width))
        for name, build in self.tabs:
            out.append("")
            out.append(group_rule(name, width))
            out.extend(build(width).lines)
        acts = self.actions(width)
        if acts:
            out.append("")
            out.append("  " + ui.head("DO THIS NEXT"))
            for act in acts:
                out.append("    " + act.label + faint("   " + act.line.strip()))
        return out

    def _card(self, width: int, left: dict, right: dict) -> list[str]:
        col = _col_width(width)

        def block(rec: dict) -> list[str]:
            if not rec:
                return [faint("gone")]
            sched = rec["schedule"]
            out = [
                ui.kv("topic", (rec["topic"] or "-")
                      + (faint(" / " + rec["subtopic"]) if rec["subtopic"] else "")),
                ui.kv("difficulty", f"{rec['difficulty'] or '-'}/5"),
                ui.kv("origin", rec["origin"] or "-"),
                ui.kv("added", (rec["created_at"] or "")[:10]),
                ui.kv("next due", scheduler.due_phrase(sched["due_at"])
                      if sched else "never drilled"),
                "",
                "  " + faint("a merge would move"),
                "  " + _pair_carries(rec),
            ]
            if rec["tags"]:
                out.append("  " + truncate(
                    " ".join(paint("#" + t, "mauve") for t in rec["tags"]), col - 2))
            return out

        return _side_by_side(block(left), block(right), width)


# ---------------------------------------------------------------- browse


# The chapters under Technicals: every topic that is not one of the two a kind
# of its own already covers. `general` is in here -- it is the catch-all for a
# technical question that fits no other chapter, not a fourth kind of question.
_TECHNICAL_TOPICS = tuple(t for t in topics.TOPICS
                          if t not in ("markets", "behavioural"))

# The top of the tree: the three kinds, and nothing else.
#
# `General` used to sit up here as a fourth chapter on `topic:general`, which
# made it the one top-level row that was not a kind and, worse, a row wholly
# *inside* another one -- every general question is also a technical, so the
# four counts summed to more than the bank and opening two of them showed the
# same questions twice. It is a topic like any other now, and it opens under
# Technicals where its questions actually live.
_TYPE_NODES = (
    (("kind", "technical"), "Technicals"),
    (("kind", "behavioural"), "Behavioral"),
    (("kind", "market_awareness"), "Market Questions"),
)

_TYPE_LABELS = dict(_TYPE_NODES)


def _label_key(s: str) -> str:
    """Two labels are "the same word" for tree-folding purposes once case,
    spaces and punctuation are off the table -- "Accounting" and "accounting"
    are the same word with a capital letter's difference between them."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


class BrowseView(ResultsView):
    """The bank as a book of chapters, with the pages you have marked above it.

    Two screens sharing one cursor. TREE is a table of contents you read
    top to bottom and open where you like: kind (Technicals / Behavioral /
    Market Questions), then topic, then the tag families and tags underneath
    -- the same hierarchy `tagging.TREE` already carries, just reachable
    inline instead of behind a second screen. Each level is indented under the
    one it opened from, carries its own count, and is coloured by how deep it
    sits under its kind -- the first level open under any chapter is gold,
    whatever it actually is (a topic for Technicals, a tag for Behavioral and
    Market Awareness, which drop straight through to their tags), and
    everything nested deeper than that is mauve. RESULTS is a `ResultsView`
    over whatever you have marked -- same columns, same expansion, same
    sorting.

    They are one class rather than two views for the same reason they always
    were: bouncing between two attached views would tear down and rebuild the
    query on every hop, and a browse is twenty hops.

    Marking a chapter is `space`, and only `space` -- it is the one key that
    toggles a filter, so it is never a side effect of moving around the tree.
    It toggles without touching where the cursor is or what is open, so
    "M&A and Behavioral" is two presses on two different rows. The filter
    strip above the tree is what that builds: every marked chapter, click
    one to drop it. `⏎` and `→` both just go in: on a branch either opens it
    to look inside, on a leaf either hands the cursor to the right-hand pane,
    uncommitted, to read that leaf's own questions one at a time. Neither
    marks anything -- `⏎` used to mark the row under the cursor on its way
    into RESULTS, which meant pressing the key that reads "into this chapter"
    also silently committed it as a filter with no way to look without
    marking.

    `←` walks the cursor back out, one step at a time: out of the preview
    pane first if that is where it is, then closed whatever is open under
    the tree cursor, then up to the row's parent; at the root of the tree it
    does nothing further -- there is nowhere else for "up a level" to mean.
    `esc` is what leaves `browse` altogether.

    The right-hand pane is a live, uncommitted preview of whatever the
    tree cursor is on -- just that row, not the marked set. It starts
    read-only, a peek that moves with the tree cursor without taking it, but
    `→` on a leaf hands it the cursor too: ↑↓ walk its own questions, `⏎`
    drops a rubric card open under the one you are on -- a dropdown, not a
    trip to `show`, which is a separate screen with its own editing and
    tagging keys that reading a question does not need -- and `←` hands the
    cursor back. Nothing here commits a filter -- that is still `space` or
    `⏎` on the tree row -- so reading a leaf's questions and deciding to
    mark it are two separate, deliberate gestures rather than one doing
    double duty.
    """

    empty_text = "no question matches every filter - ← to drop one"

    _TREE_LEFT_W = 34         # "Deal Process & Mechanics" plus a 4-digit count
    _CHIPS_ITEM = 0           # reserved item index for the filter-chip strip
    _TREE_BASE = 1            # real tree/pinned rows start here

    def __init__(self, conn: sqlite3.Connection, facets: list[tuple[str, str]] | None = None,
                 *, match: browse.Match = browse.DEFAULT):
        self.facets = list(facets or [])
        self.match = match
        # A fresh, unfiltered `browse` opens on the tree -- the structured
        # way in. `browse --topic dcf` already told it what you want, so that
        # one still lands on results, and a piped/one-shot call keeps the
        # flat listing too since there is no shell to page a tree through.
        self.mode = "tree" if not self.facets and tui.active() else "results"
        # Keyed by the node's own `path`, not by its bare facet -- the same
        # tag or family can sit under more than one chapter (`credit` is
        # both a dcf and an lbo tag), and keying on the facet alone opened or
        # closed every occurrence of it in the tree at once, wherever the
        # cursor actually was.
        self.open_nodes: set[tuple[tuple[str, str], ...]] = set()
        self._tree_sel = 0
        self._tree_top = 0
        self._tree_rows: list[dict] = []
        # Keyed by the path (a tuple of facets from the root down to and
        # including the node), because the same family can sit under more
        # than one branch and its children's counts depend on which one.
        self._tree_cache: dict[tuple, list[dict]] = {}
        self._preview_cache: dict[tuple, list[dict]] = {}
        # → on a leaf hands the cursor to its preview pane instead of marking
        # it -- marking already has a key (space). `_preview_sel`/`_preview_top`
        # are that pane's own cursor and scroll position, meaningless while
        # `_preview_focus` is False.
        self._preview_focus = False
        self._preview_sel = 0
        self._preview_top = 0
        # Which preview rows (positions, same numbering as `_preview_sel`)
        # are showing their rubric underneath them -- a dropdown, not a trip
        # to `show`: reading what a question actually asks should not mean
        # leaving the tree for a whole other screen.
        self._preview_expanded: set[int] = set()
        self._chip_spans: list[tuple[int, int, int]] = []
        self._chip_line_pos: int | None = None
        # Where the cursor was in the results list, so going up a level and
        # back does not dump you at the top of a hundred rows.
        self._result_sel = 0
        # Which questions were open, parked while the tree is up. `expanded`
        # holds row *positions*, and the two screens do not share a
        # numbering, so it cannot cross over uninterpreted.
        self._results_expanded: set[int] = set()
        # A do-row that has been chosen once and is waiting to be chosen again.
        self._armed: str | None = None
        self._ids_csv: str | None = None
        # Facets `_toggle_node` added on a mark's behalf (the implied kind
        # behind a topic shown nested under a chapter it disagrees with) --
        # tracked so unmarking that node can undo exactly what it added.
        self._auto_facets: set[tuple[str, str]] = set()
        super().__init__(conn, browse.matching(conn, self.facets, match=match),
                         title="BROWSE")
        self.sort = "topic"
        self._reorder()
        # `_reorder` keeps the cursor on the row it was on, which is right when
        # you press ⇥ and wrong here: the row it was on is row 0 of the
        # relevance order the list was built in, and holding onto it opened
        # `browse` at row 117 of 1086 with 116 questions scrolled off above.
        self.sel = self.top = self._result_sel = 0
        if self.mode == "tree":
            self._tree_sel = self._first_browse_row()

    # -- the filter stack

    def _row_identity(self, r: dict) -> tuple:
        """What a tree row *is*, independently of where it sits.

        A node's `path` is already unique. A pinned row is not so lucky --
        but the pinned rows above the tree come and go with the facet count
        (`clear every filter` only exists once there is one) and a filter
        that empties a family takes its tag rows with it, so the number a
        row happens to have this render is not a safe thing to hold onto.
        """
        if r.get("act") == "node":
            return ("node", r["path"])
        return ("pinned", r.get("act"), r.get("kind"), r.get("name"))

    def _refresh(self) -> None:
        """Re-run the query and throw away everything derived from the old one."""
        key = None
        if self.mode == "tree" and self._tree_rows and self._tree_sel < len(self._tree_rows):
            key = self._row_identity(self._tree_rows[self._tree_sel])
        self.rows = [dict(r) | {"_rank": i} for i, r in enumerate(
            browse.matching(self.conn, self.facets, match=self.match))]
        self._tree_cache.clear()
        self._preview_cache.clear()
        self._decorate()
        self.expanded.clear()
        self._results_expanded.clear()
        self.invalidate()
        self._armed = None
        self._ids_csv = None
        self._preview_focus = False
        self._preview_expanded.clear()
        self.sel = min(self.sel, max(0, len(self.rows) - 1))
        self._result_sel = min(self._result_sel, max(0, len(self.rows) - 1))
        self._reorder()
        if key is not None:
            self.count()
            self._tree_sel = next((i for i, r in enumerate(self._tree_rows)
                                   if self._row_identity(r) == key), self._tree_sel)
            self._tree_sel = max(0, min(self._tree_sel, len(self._tree_rows) - 1))

    def add(self, kind: str, value: str) -> None:
        if (kind, value) not in self.facets:
            self.facets.append((kind, value))
            self._refresh()

    def drop(self, kind: str, value: str) -> None:
        if (kind, value) in self.facets:
            self.facets.remove((kind, value))
            # Whatever removed it -- the chip itself, `alt-x`, or `_toggle_node`
            # unwinding its own "also" -- the auto-added relationship is moot
            # once the facet is gone. Leaving it tracked would make a later,
            # unrelated manual re-mark of the same facet look auto-owned and
            # get silently dropped out from under the person who just chose it.
            self._auto_facets.discard((kind, value))
            self._refresh()

    def _toggle_chip(self, facet: tuple[str, str]) -> None:
        (self.drop if facet in self.facets else self.add)(*facet)

    def _toggle_node(self, r: dict) -> None:
        """Mark or unmark a tree row, carrying its implied kind with it.

        A topic node nested under Technicals only appears there because it is
        scoped to `kind:technical` while it is being previewed (`_children`
        bakes the parent path into the count it shows). Marking used to commit
        only the bare topic facet, so the set you actually got included every
        kind sharing that topic -- not just the ~7 the row said were there.
        `also` names the extra facet(s) that scope implied, and this keeps
        them in lockstep with the node's own facet: added together on mark,
        and removed together on unmark only if nothing else still wants them.
        """
        facet = r["facet"]
        was_marked = facet in self.facets
        self._toggle_chip(facet)
        for extra in r.get("also", ()):
            if not was_marked:
                if extra not in self.facets:
                    self.add(*extra)
                    self._auto_facets.add(extra)
            elif extra in self._auto_facets:
                self.drop(*extra)

    def selected_ids(self) -> list[int]:
        return [r["id"] for r in self.rows]

    def ids_csv(self) -> str:
        """The selection as `drill --ids` wants it. Order is not part of it:
        the scheduler decides what gets asked first, whatever order it arrives
        in, which is what makes caching this across a re-sort safe."""
        if self._ids_csv is None:
            self._ids_csv = ",".join(str(i) for i in self.selected_ids())
        return self._ids_csv

    # -- the tree: what sits under a path, and the flattened rows it makes

    def _children(self, path: tuple[tuple[str, str], ...]) -> list[dict]:
        """What is directly under `path` -- the four Type rows when it is
        empty. Every level bottoms out in a `browse.py` query that is already
        scoped to `self.facets` plus this path, uncommitted -- the same
        mechanism the old topic preview used, just walked one level further
        each time a branch opens.
        """
        scope = self.facets + list(path)
        if not path:
            kinds = browse.kind_counts(self.conn, scope, match=self.match)
            return [{"facet": facet, "label": label, "n": kinds.get(facet[1], 0),
                     "has_children": True} for facet, label in _TYPE_NODES]
        kind, value = path[-1]
        if kind == "kind" and value == "technical":
            # Technicals' chapters are the ten technical topics, always all
            # ten and always in the same order, at zero as readily as at 190 --
            # a structure that drops a row the moment a filter empties it is
            # not a structure, it is a second results list.
            #
            # Behavioural and Market Awareness are not listed this way because
            # each holds exactly one topic, named after the kind itself
            # (`kind_for_topic` is what makes that true): a single chapter
            # containing the whole book. They drop straight through to their
            # tags.
            #
            # A topic that is *not* technical but has technical-kind questions
            # in it is shown anyway, under its own name. That only ever happens
            # when the two columns disagree about a question, and a misfiling
            # is a thing to see and fix rather than to hide behind a fixed list.
            counts = browse.topic_counts(self.conn, scope, match=self.match)
            shown = [t for t in topics.TOPICS
                     if t in _TECHNICAL_TOPICS or counts.get(t)
                     or ("topic", t) in self.facets]
            # `counts` is already scoped to `kind:technical` (it is baked into
            # `scope` via `path`), which is exactly right for the number shown
            # -- but the topics not in `_TECHNICAL_TOPICS` only ever appear
            # here because a kind/topic disagreement put a technical-kind
            # question under a topic that would normally live elsewhere.
            # Marking the row has to carry that same kind restriction with
            # it, or the set it commits is every kind sharing the topic, not
            # the handful the row actually counted.
            return [{"facet": ("topic", t), "label": topics.TOPIC_LABELS[t],
                     "n": counts.get(t, 0), "has_children": True,
                     **({} if t in _TECHNICAL_TOPICS
                        else {"also": (("kind", "technical"),)})}
                    for t in shown]
        # `options()` drops a tag already in its facets list -- right for the
        # old add-only filter screen, where a chosen tag had nothing left to
        # do, and wrong here, where a marked leaf is meant to stay put with
        # its own checkmark. Tags marked elsewhere in the tree are dropped
        # from the scope (this branch's own path is kept, since that is what
        # actually selects it) so a chip never hides itself or a sibling.
        tag_scope = [f for f in self.facets if f[0] != "tag"] + list(path)
        fams = browse.options(self.conn, tag_scope, match=self.match)["tag"]
        if kind == "tag":
            # Expanding a family: `fams` was computed with the family itself
            # already in scope, so its own row (still present, since it still
            # has children) carries exactly the grandchildren to show.
            fam = next((f for f in fams if f["name"] == value), None)
            return [{"facet": ("tag", c["name"]), "label": c["name"],
                     "n": c["n"], "has_children": False}
                    for c in (fam["children"] if fam else [])]
        # A family that shares its chapter's own name (`accounting` under
        # Accounting, `lbo` under LBO) adds nothing by existing as a row --
        # the chapter you are already inside already means everything under
        # it, so nesting a same-named chapter inside it reads as the bank
        # containing itself. Fold it: splice its own children straight into
        # this level instead of wrapping them in a chapter restating the one
        # you just opened. A family with a name of its own (`credit`,
        # `capital-markets`) is unaffected -- it groups something the chapter
        # name does not already say.
        chapter_label = (topics.TOPIC_LABELS[value] if kind == "topic"
                         else _TYPE_LABELS.get((kind, value), value))
        chapter_key = _label_key(chapter_label)
        out: list[dict] = []
        for f in fams:
            if _label_key(f["name"]) == chapter_key:
                out.extend({"facet": ("tag", c["name"]), "label": c["name"],
                            "n": c["n"], "has_children": False}
                           for c in f["children"])
            else:
                out.append({"facet": ("tag", f["name"]), "label": f["name"],
                            "n": f["n"], "has_children": bool(f["children"])})
        return out

    def _node_children(self, path: tuple[tuple[str, str], ...]) -> list[dict]:
        if path not in self._tree_cache:
            self._tree_cache[path] = self._children(path)
        return self._tree_cache[path]

    def _walk(self, path: tuple[tuple[str, str], ...], depth: int) -> list[dict]:
        out: list[dict] = []
        for child in self._node_children(path):
            facet = child["facet"]
            child_path = path + (facet,)
            out.append({"group": "browse", "act": "node", "facet": facet,
                        "label": child["label"], "n": child["n"], "depth": depth,
                        "has_children": child["has_children"],
                        "open": child_path in self.open_nodes,
                        "selected": facet in self.facets, "path": child_path,
                        "also": child.get("also", ())})
            if child["has_children"] and child_path in self.open_nodes:
                out.extend(self._walk(child_path, depth + 1))
        return out

    def _build_pinned_rows(self) -> list[dict]:
        """The rows above the tree: actions on the current set, then the
        joiners -- everything that is about the set as a whole rather than
        about one chapter.

        The flags (`due`, `unseen`, `weak`, ...) used to be listed here as a
        third group. They are questions about the *schedule*, not about what
        the bank contains, and `browse` is the screen for the second one -- so
        they cost five rows above every chapter, permanently, to answer
        something `drill` and the dashboard already answer. `browse --flag due`
        still sets one from the command line, and it still shows as a chip.
        """
        out: list[dict] = []
        n = len(self.rows)
        if n:
            ids = self.ids_csv()
            # No per-question action here. `tag #N` used to sit in this group
            # and named `self.rows[self._result_sel]` -- the *results-mode*
            # cursor, saved on the way out of the other mode -- while the pane
            # beside it showed the tree's own preview list. On a fresh browse
            # that offered to tag whatever the sort had put first, a question
            # nothing on screen was showing and the cursor was nowhere near.
            #
            # It was in the wrong group either way: this one is "everything
            # about the set as a whole", and tagging one question is not that.
            # `open N as a list` is one row below, and every row there carries
            # its own correct `tag #N` on its own do-screen.
            for act in (
                Action("drill", f"drill these {n}", f"drill --ids {ids}",
                       arm=f"drill these {n}? ⏎ starts the sitting · ← backs out"),
                Action("mock", f"mock these {n}", f"mock --ids {ids}",
                       arm=f"mock these {n}? ⏎ starts the clock · ← backs out"),
            ):
                out.append({"group": "do", "act": act.key, "action": act, "n": None})
            out.append({"group": "do", "act": "list",
                        "label": f"open {n} as a list", "n": None})
        if self.facets:
            out.append({"group": "do", "act": "clear",
                        "label": "clear every filter", "n": None})
        by = browse.split(self.facets)
        if len(by) > 1:
            now, then = ("any", "every") if self.match.any_of else ("every", "any")
            out.append({"group": "active filters", "act": "join", "n": None,
                        "toggle": f"matching {now} filter"
                                  + faint(f"   ⏎ for {then}")})
        if len(by.get("tag", [])) >= 2:
            now, then = (("all", "any") if self.match.all_within("tag")
                         else ("any", "all"))
            out.append({"group": "active filters", "act": "within",
                        "kind": "tag", "n": None,
                        "toggle": f"tags: {now} of them"
                                  + faint(f"   ⏎ for {then}")})
        return out

    def _build_rows(self) -> list[dict]:
        return self._build_pinned_rows() + self._walk((), 0)

    # -- PickerView surface

    def count(self) -> int:
        if self.mode == "tree":
            self._tree_rows = self._build_rows()
            return len(self._tree_rows)
        return len(self.rows)

    @property
    def tally(self) -> str:
        n = len(self.rows)
        if self.mode == "tree":
            return faint(f"browsing {_plural(n, 'question')}"
                         f"  ·  {dict(SORTS)[self.sort]}")
        return _plural(n, "question") + "  ·  " + dict(SORTS)[self.sort]

    @tally.setter
    def tally(self, _v: str) -> None:
        pass

    def render(self, width: int) -> list[str]:
        if self.mode == "tree":
            return self._render_tree(width)
        return super().render(width)

    def click_at(self, item: int, col: int, shell) -> bool:
        if self.mode == "tree":
            return self._click_tree(item, col, shell)
        return super().click_at(item, col, shell)

    def on_resume(self) -> None:
        super().on_resume()
        self._tree_cache.clear()
        self._preview_cache.clear()
        self._preview_focus = False
        self._preview_expanded.clear()

    def scroll_by(self, delta: int) -> bool:
        if self.mode != "tree":
            return super().scroll_by(delta)
        if self._preview_focus:
            n = len(self._preview_rows())
            if not n:
                return False
            target = max(0, min(n - 1, self._preview_sel + delta))
            if target == self._preview_sel:
                return False
            self._preview_sel = target
            return True
        n = len(self._tree_rows) or self.count()
        if not n:
            return False
        target = max(0, min(n - 1, self._tree_sel + delta))
        if target == self._tree_sel:
            return False
        self._tree_sel = target
        return True

    def header(self, width: int) -> list[str]:
        out = super().header(width)
        self._chip_line_pos = None
        if self.facets:
            out.append("  " + self._chip_line(width))
            self._chip_line_pos = len(out) - 1
        elif self.mode == "results":
            out.append("  " + faint("no filters - press ← to mark some"))
        return out

    def _chip_line(self, width: int) -> str:
        """Every marked chapter as a removable pill. Click one to drop it --
        the same route `space` takes from the tree, just from the strip that
        is visible over both screens.
        """
        cells = []
        spans: list[tuple[int, int, int]] = []
        col = 2                                   # the leading "  "
        for i, (kind, value) in enumerate(self.facets):
            cell = ui.chip(f"{kind}:{value}", "mauve")
            cells.append(cell)
            spans.append((col, col + vlen(cell), i))
            col += vlen(cell) + 1
        self._chip_spans = spans
        return truncate(" ".join(cells), width - 2)

    # -- tree mode: the table of contents on the left, a live preview on the right

    def _fit(self, s: str, width: int) -> str:
        return pad(truncate(s, width), width)

    def _row_line(self, r: dict, width: int, chosen: bool) -> str:
        # A chapter is checked first, because it carries a `label` of its own
        # and every row here that is *not* a chapter is drawn from one. Tested
        # last, a topic came out as a plain action row: no indent, no count and
        # no checkmark, so an opened branch looked exactly like the level above
        # it and the tree read as one flat list.
        if r.get("act") == "node":
            return self._node_line(r, width, chosen)
        if "action" in r:
            act = r["action"]
            return action_row(act.label, chosen,
                              act.arm if self._armed == act.key else "",
                              width=width)
        if "toggle" in r:
            return action_row(r["toggle"], chosen, mark="⇄", width=width)
        return action_row(r["label"], chosen, width=width)

    # What a row's colour tracks: how deep it sits *under its kind*, not what
    # facet type it happens to be. Three hues at full strength rather than one
    # hue fading out with depth -- a dimmed row reads as unavailable, and
    # every row here is one keystroke from a drill.
    #
    # It used to be keyed by facet type (kind/topic/tag), so the first row
    # under a chapter was gold for Technicals (a topic) but mauve for
    # Behavioral and Market Awareness (a tag, since those two drop straight
    # through to their tags with no topic level between). That is correct
    # about what each row *is*, but it reads as arbitrary: the first thing you
    # open under any chapter looks like a different kind of row depending on
    # which chapter it was. Keying on depth instead makes "the first level
    # under the kind" one colour everywhere, and "anything nested deeper than
    # that" another -- which is also what the `#` in front of a tag name used
    # to do, without spending three cells of a 25-cell column or making the
    # tree look like two different naming schemes.
    _DEPTH_TINT = ("text", "gold", "mauve")   # index by depth, clamped at the last

    def _tint_for(self, depth: int) -> str:
        return self._DEPTH_TINT[min(depth, len(self._DEPTH_TINT) - 1)]

    def _node_line(self, r: dict, width: int, chosen: bool) -> str:
        """One chapter: how deep it sits, what it is, whether it opens,
        whether it is marked, and how many questions are under it.

        The indent is two cells a level rather than four -- the column is a
        third of the terminal and a tag three levels down still has to be
        readable in what is left of it.
        """
        # Marked is the one thing that overrides the row's own colour, because
        # it is the one thing that is about *this* row rather than about what
        # kind of row it is. The cursor does not need a colour of its own: it
        # has the bar, the bold and the count.
        tint = "sky" if r["selected"] else self._tint_for(r["depth"])
        bar = paint("▎", "accent") if chosen else " "
        depth = "  " * r["depth"]
        if r["has_children"]:
            exp = paint("▾" if r["open"] else "▸", "accent" if chosen else tint)
        else:
            exp = paint("·", "accent" if chosen else tint)
        mark = paint("✓", "mint") if r["selected"] else " "
        # The count is right-aligned by the gap rather than by a fixed field:
        # padding it to four cells took those cells off the label, and in a
        # 20-cell column that is the difference between `Technicals` and
        # `Technica…`. The digits still end at the same edge on every row.
        count = str(r["n"])
        head = f"{bar} {depth}{exp}{mark} "
        label = paint(truncate(r["label"],
                               max(4, width - vlen(head) - len(count) - 3)),
                      tint, BOLD if chosen else "")
        left = head + label
        gap = max(1, width - vlen(left) - len(count) - 1)
        return left + " " * gap + paint(count, "accent" if chosen else "faint")

    def _left_lines(self, width: int) -> list[tuple[str, int | None]]:
        """One entry per drawn line: its text, and the tree-row index it
        belongs to, or `None` for a group header no cursor can land on."""
        out: list[tuple[str, int | None]] = []
        last_group = None
        for i, r in enumerate(self._tree_rows):
            g = r.get("group")
            if g is not None and g != last_group:
                last_group = g
                out.append((self._fit("  " + paint(g.upper(), "mauve", BOLD), width), None))
            chosen = self.focused and not self._preview_focus and i == self._tree_sel
            out.append((self._row_line(r, width, chosen), i))
        return out

    def _scroll_tree_into_view(self, lines: list[tuple[str, int | None]], body: int) -> None:
        pos = next((i for i, (_, idx) in enumerate(lines) if idx == self._tree_sel), 0)
        if pos < self._tree_top:
            self._tree_top = pos
        elif pos >= self._tree_top + body:
            self._tree_top = pos - body + 1
        self._tree_top = max(0, min(self._tree_top, max(0, len(lines) - body)))

    def _scroll_preview_into_view(self, lines: list[tuple[str, int | None]], body: int) -> None:
        pos = next((i for i, (_, idx) in enumerate(lines) if idx == self._preview_sel), 0)
        if pos < self._preview_top:
            self._preview_top = pos
        elif pos >= self._preview_top + body:
            self._preview_top = pos - body + 1
        self._preview_top = max(0, min(self._preview_top, max(0, len(lines) - body)))

    @staticmethod
    def _path_facets(path: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
        """`path` as filter facets, with a nested tag family collapsed to its
        deepest tag.

        A tag path is the only kind that ever nests two facets of its own
        kind -- a family, then one of its children -- and a family always
        matches its whole subtree, so the child is a strict subset of it.
        Tag values of the same kind are OR-ed by default, and OR-ing a subset
        onto its own superset changes nothing: every leaf under `fit` would
        preview the same 67 questions `fit` itself does, because the family
        facet inherited from the path never stopped applying. Only the
        deepest tag facet is what the cursor is actually sitting on.
        """
        tags = [f for f in path if f[0] == "tag"]
        out = [f for f in path if f[0] != "tag"]
        if tags:
            out.append(tags[-1])
        return out

    def _preview_rows(self) -> list[dict]:
        """The cursor row's own matching questions, uncommitted -- just this
        row, not the marked set, cached per path for the trip back. This is
        also what `_preview_focus` scrolls through: → on a leaf hands the
        cursor to this same list rather than marking the leaf, since marking
        already has a key (space) -- the tree row it came from does not move.

        A pinned row is the exception, because a pinned row *is* about the
        marked set: `drill these 94` shows the 94 it would ask. It also stops
        the pane going blank the moment the cursor rests on the rows it opens
        on."""
        if not self._tree_rows or self._tree_sel >= len(self._tree_rows):
            return self.rows
        r = self._tree_rows[self._tree_sel]
        if r.get("act") != "node":
            return self.rows
        path = tuple(r["path"])
        if path not in self._preview_cache:
            rows = [dict(x) | {"_rank": i} for i, x in enumerate(browse.matching(
                self.conn, self.facets + self._path_facets(path), match=self.match))]
            self._decorate(rows)
            rows.sort(key=self.sort_key)
            self._preview_cache[path] = rows
        return self._preview_cache[path]

    def _preview_row(self, rows: list[dict], idx: int, width: int,
                     chosen: bool = False, expanded: bool = False) -> str:
        r = rows[idx]
        dot = status_dot(r.get("status") or "active")
        qid = pad(paint(f"#{r['id']}", "accent" if chosen else "sky"), 6)
        bar = paint("▎", "accent") if chosen else " "
        caret = paint("▾" if expanded else "▸", "accent" if chosen else "faint")
        prefix = f"{bar} {caret} {dot} {qid} "
        text_w = max(8, width - vlen(prefix))
        return self._fit(prefix + self._headline(
            _one_line(r["canonical_text"]), text_w, chosen), width)

    def _preview_detail(self, row: dict, width: int) -> list[str]:
        """The dropdown under an expanded preview row: the rubric as the
        answer, the same card `drill` and `show` reveal, scaled to the
        narrower right-hand column -- so checking what a question actually
        asks does not mean leaving the tree for `show`'s own screen.
        """
        a = self.conn.execute(
            "SELECT answer_key, rubric_points FROM answers WHERE question_id = ?",
            (row["id"],)).fetchone()
        gutter = "  │  "
        body_w = max(20, width - vlen(gutter))
        points = json.loads((a["rubric_points"] if a else None) or "[]")
        if points:
            text = [p if isinstance(p, str) else str(p.get("point", p)) for p in points]
            card = ui.answer_card(text, w=body_w, indent="")
        else:
            answer = ((a["answer_key"] if a else "") or "").strip()
            card = ui.body(answer, "", body_w) if answer else faint("no answer on file yet")
        return [gutter + line for line in card.split("\n")] + [""]

    def _right_lines(self, preview: list[dict], width: int) -> list[tuple[str, int | None]]:
        """One entry per drawn line on the right, the same shape `_left_lines`
        returns on the left -- a preview row is one line collapsed, several
        once its dropdown is open, so the two panes can no longer assume
        row-for-row alignment and each needs its own flattened list to scroll."""
        out: list[tuple[str, int | None]] = []
        for i, r in enumerate(preview):
            chosen = self.focused and self._preview_focus and i == self._preview_sel
            expanded = i in self._preview_expanded
            out.append((self._preview_row(preview, i, width, chosen, expanded), i))
            if expanded:
                out.extend((line, i) for line in self._preview_detail(r, width))
        return out

    @staticmethod
    def _more_note(above: int, below: int) -> str:
        bits = []
        if above:
            bits.append(f"↑ {above} above")
        if below:
            bits.append(f"↓ {below} below")
        return faint("   ".join(bits))

    def _render_tree(self, width: int) -> list[str]:
        self.owner = []
        out: list[str] = []

        def put(line: str, item: int = -1) -> None:
            out.append(truncate(line, width))
            self.owner.append(item)

        put("")
        hdr = self.header(width)
        for i, line in enumerate(hdr):
            put(line, self._CHIPS_ITEM if i == self._chip_line_pos else -1)
        put("")

        self.count()      # rebuilds self._tree_rows before anything reads it
        self._tree_sel = max(0, min(self._tree_sel, max(0, len(self._tree_rows) - 1)))

        sep = "  │  "
        # Two fifths rather than a third: the chapter names are the part you
        # steer by, and on a 64-cell terminal a third of the screen cut
        # `Deal Process & Mechanics` down to `Deal Pro…`. The preview is a
        # headline either way.
        left_w = min(self._TREE_LEFT_W, max(18, width * 2 // 5))
        right_w = max(20, width - left_w - vlen(sep))

        lines = self._left_lines(left_w)
        body = max(3, self.viewport - len(out) - 1)
        self._scroll_tree_into_view(lines, body)

        preview = self._preview_rows()
        if self._preview_focus:
            self._preview_sel = max(0, min(self._preview_sel, max(0, len(preview) - 1)))
        else:
            self._preview_expanded.clear()
        rlines = self._right_lines(preview, right_w)
        self._scroll_preview_into_view(rlines, body)
        right_lines = [text for text, _ in rlines[self._preview_top:self._preview_top + body]]

        # Always `body` rows, however few there are to put in them. The frame
        # is anchored to the bottom of the terminal, so a screen drawn to fit
        # its contents is a screen that moves every time the contents change:
        # walking from a topic with 190 questions to one with 12 slid the whole
        # chapter list up under the cursor. The height belongs to the pane, not
        # to whichever column happens to be longest this frame.
        for i in range(body):
            li = self._tree_top + i
            left_txt, row_idx = lines[li] if li < len(lines) else ("", None)
            right_txt = right_lines[i] if i < len(right_lines) else ""
            line = self._fit(left_txt, left_w) + sep + right_txt
            item = (self._TREE_BASE + row_idx) if row_idx is not None else -1
            if self.focused and item >= 0 and item == self.hover:
                line = ui.wash(line, "hover", width)
            put(line, item)

        left_note = self._more_note(
            self._tree_top, max(0, len(lines) - self._tree_top - body))
        right_note = self._more_note(
            self._preview_top, max(0, len(rlines) - self._preview_top - len(right_lines)))
        put(self._fit("  " + left_note, left_w) + sep + right_note)
        return out

    def _click_tree(self, item: int, col: int, shell) -> bool:
        if item == self._CHIPS_ITEM:
            hit = next((i for s, e, i in self._chip_spans if s <= col < e), None)
            if hit is not None and hit < len(self.facets):
                self.drop(*self.facets[hit])
            return True
        idx = item - self._TREE_BASE
        if idx < 0 or idx >= len(self._tree_rows):
            return True
        if idx != self._tree_sel:
            self._armed = None
            self._tree_sel = idx
            return True
        r = self._tree_rows[idx]
        if r.get("act") == "node":
            if r["has_children"]:
                self.open_nodes ^= {r["path"]}
            else:
                self._toggle_node(r)
        elif r.get("group") == "do":
            self._activate_pinned(r, shell)
        else:
            self._activate_pinned(r, shell, stay=True)
        return True

    def group_of(self, idx: int) -> str | None:
        if self.mode == "results":
            return super().group_of(idx)
        return None

    def expandable(self, idx: int) -> bool:
        return self.mode == "results"

    def row(self, idx: int, width: int, chosen: bool) -> str:
        if self.mode == "results":
            return super().row(idx, width, chosen)
        return self._row_line(self._tree_rows[idx], width, chosen)

    def activate(self, idx: int, shell, stay: bool = False) -> bool:
        if self.mode == "results":
            return super().activate(idx, shell)
        r = self._tree_rows[idx]
        if r.get("act") == "node":
            self.add(*r["facet"])
            if not stay:
                self._to_results()
            return True
        return self._activate_pinned(r, shell, stay)

    def _activate_pinned(self, r: dict, shell, stay: bool = False) -> bool:
        act = r["act"]
        if "action" in r:
            # Starting a sitting is the one thing here that writes to the
            # schedule, and it takes a second, identical press to fire --
            # through the same `fire` every other list's actions go through.
            return fire(self, r["action"], shell)
        if act == "join":
            self.match = self.match.flipped()
            self._refresh()
            return True
        if act == "within":
            self.match = self.match.toggled(r["kind"])
            self._refresh()
            return True
        if act == "clear":
            self.facets = []
            self._refresh()
            return True
        self._to_results()                          # act == "list"
        return True

    def _run(self, act: str, shell) -> None:
        shell.run_now(f"{act} --ids {self.ids_csv()}")

    def _to_results(self) -> None:
        self.mode = "results"
        self.expanded = {i for i in self._results_expanded if i < len(self.rows)}
        self._armed = None
        self._preview_focus = False
        self._preview_expanded.clear()
        self.hover = None
        self.sel = min(self._result_sel, max(0, len(self.rows) - 1))
        self.top = 0

    def _resorted(self) -> None:
        # The preview pane is cached per tree path and ordered on the way in,
        # so a sort the cache predates is a sort the pane never sees.
        self._preview_cache.clear()

    def _first_browse_row(self) -> int:
        """Where the tree opens: a chapter, never a do-row.

        `→` is safe on a do-row (it means "into this one", and an action has
        no inside), but landing on `drill these N` regardless is still the
        row `←` `⏎` would fire, and that is one keystroke closer to a sitting
        starting by accident than landing on a chapter is.
        """
        self.count()
        for i, r in enumerate(self._tree_rows):
            if r.get("group") == "browse":
                return i
        return 0

    def _to_tree(self) -> None:
        self._result_sel = self.sel
        self._results_expanded = set(self.expanded)
        self.expanded = set()
        self.mode = "tree"
        self._tree_sel = self._first_browse_row()
        self._tree_top = 0
        self._armed = None
        self._preview_focus = False
        self._preview_expanded.clear()
        self.hover = None

    def _handle_preview(self, key: Key, shell) -> bool:
        """Cursor inside the right-hand pane, walking the leaf's own
        questions -- the same list `_preview_rows` was already showing,
        just now with a cursor on it instead of only a hover.

        `⏎` drops the rubric open right there rather than jumping to
        `show`: `show` is a separate screen with its own editing and
        tagging keys, and landing on it just to read what a question asks
        swapped the whole tree out for a screen you had not asked to open.
        A dropdown answers the same question -- what does this one say --
        without leaving the tree.

        ← is what walks the cursor back to the tree row it came from,
        matching the "→ deeper, ← shallower" the tree itself uses.
        """
        n = key.name
        preview = self._preview_rows()
        last = max(0, len(preview) - 1)
        if n == "up":
            self._preview_sel = max(0, self._preview_sel - 1)
            return True
        if n == "down":
            self._preview_sel = min(last, self._preview_sel + 1)
            return True
        if n == "pgup":
            self._preview_sel = max(0, self._preview_sel - self.page())
            return True
        if n == "pgdn":
            self._preview_sel = min(last, self._preview_sel + self.page())
            return True
        if n == "left":
            self._preview_focus = False
            self._preview_expanded.clear()
            return True
        if n == "enter":
            if preview:
                self._preview_expanded ^= {self._preview_sel}
            return True
        return self.extra_keys(key, shell)

    def _handle_tree(self, key: Key, shell) -> bool:
        n = key.name
        is_space = n == "char" and key.ch == " "
        rows = self._tree_rows
        if not rows:
            return self.extra_keys(key, shell)
        if self._preview_focus:
            return self._handle_preview(key, shell)
        r = rows[self._tree_sel] if self._tree_sel < len(rows) else None
        if self._armed and not (n == "enter" and r and r.get("act") == self._armed):
            # Anything that is not the same key again backs out, and the key
            # that backed out is spent doing so: ← disarms rather than also
            # moving the cursor or leaving the row it was on.
            self._armed = None
            if n == "left":
                return True
        if n == "up":
            self._tree_sel = max(0, self._tree_sel - 1)
            return True
        if n == "down":
            self._tree_sel = min(len(rows) - 1, self._tree_sel + 1)
            return True
        if n == "pgup":
            self._tree_sel = max(0, self._tree_sel - self.page())
            return True
        if n == "pgdn":
            self._tree_sel = min(len(rows) - 1, self._tree_sel + self.page())
            return True
        if r is None:
            return self.extra_keys(key, shell)
        if is_space:
            if r.get("act") == "node":
                self._toggle_node(r)
            return True
        if n in ("right", "enter") and r.get("act") == "node":
            # → and ⏎ both just go in -- neither marks. Marking already has
            # its own key (space), so pressing "go into this row" must not
            # also silently commit it as a filter; that used to be ⏎'s job
            # and meant there was no way to look at a chapter without
            # marking it. A leaf has nowhere further down in the tree, so
            # either key hands the cursor to its own questions instead.
            if r["has_children"]:
                self.open_nodes.add(r["path"])
            else:
                preview = self._preview_rows()
                if preview:
                    self._preview_focus = True
                    self._preview_sel = 0
                    self._preview_top = 0
            return True
        if n == "right":
            if r.get("group") == "do":
                # → has no inside for an action -- it is also the key you are
                # holding down while walking the tree, so it is the last one
                # that should be able to start a drill.
                return True
            return self._activate_pinned(r, shell, stay=True)
        if n == "enter":
            return self._activate_pinned(r, shell)
        if n == "left":
            if r.get("act") == "node" and r["has_children"] and r["open"]:
                self.open_nodes.discard(r["path"])
                return True
            if r.get("act") == "node" and len(r["path"]) > 1:
                parent_path = r["path"][:-1]
                parent = next((i for i, rr in enumerate(rows)
                              if rr.get("path") == parent_path), None)
                if parent is not None:
                    self._tree_sel = parent
                return True
            return True         # the root, or a pinned row: nowhere further up
        return self.extra_keys(key, shell)

    def handle(self, key: Key, shell) -> bool:
        """← is up a level; on a question that is open it collapses that
        first, the same as everywhere else in the shell."""
        if self.mode == "tree":
            return self._handle_tree(key, shell)
        n = key.name
        if n == "left" and self.sel not in self.expanded:
            self._to_tree()
            return True
        return super().handle(key, shell)

    def extra_keys(self, key: Key, shell) -> bool:
        # Alt chords stay bound as accelerators for terminals that pass them
        # through, but nothing advertises them and nothing needs them: every
        # one of these is reachable from the arrows, Enter and space alone.
        n = key.name
        if n == "alt-n":
            self._to_results() if self.mode == "tree" else self._to_tree()
            return True
        if n == "alt-x":
            if self.facets:
                self.drop(*self.facets[-1])
            return True
        if n in ("alt-d", "alt-m") and self.rows:
            self._run("drill" if n == "alt-d" else "mock", shell)
            return True
        if n == "alt-t" and self.mode == "results" and self.rows:
            shell.prefill(f"tag {self.rows[self.sel]['id']} ")
            return True
        if self.mode == "tree":
            # Grouping and expand-all are results-list ideas and are swallowed
            # here: the tree's own structure is the grouping, and its rows open
            # one chapter at a time.
            #
            # The sort is not. It used to be swallowed alongside them, on the
            # grounds that it "would silently reorder rows the cursor is
            # sitting on" -- but the cursor sits on chapters, which the
            # taxonomy orders, and the sort has never touched those. What it
            # orders is the preview pane, which is a list of questions like any
            # other. While that pane ignored the sort the key really did
            # nothing, so swallowing it was honest; now that the pane follows
            # it, the key does the obvious thing.
            if n == "alt-g" or n == "alt-a":
                return True
            return super().extra_keys(key, shell)
        return super().extra_keys(key, shell)

    def hints(self) -> list[tuple[str, str]]:
        if self.mode == "tree":
            if self._armed:
                return [("⏎", "yes, start it"), ("←", "back out")]
            if self._preview_focus:
                return [("↑↓", "move"), ("⏎", "peek"),
                        ("←", "back to the tree"), ("esc", "done")]
            r = (self._tree_rows[self._tree_sel]
                 if self._tree_rows and self._tree_sel < len(self._tree_rows)
                 else None)
            branch = bool(r and r.get("act") == "node" and r.get("has_children"))
            label = "open it" if branch else "view questions"
            return [("↑↓", "move"),
                    ("→/⏎", label), ("space", "mark"),
                    ("←", "back"), ("esc", "done")]
        return [("↑↓", "move"), ("→", "peek"), ("⏎", "open"),
                ("←", "collapse · back to the tree"), ("esc", "done")]
