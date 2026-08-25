"""Stacked filters over the bank.

`find` answers "where is the question about X". This answers the other half:
"show me what I have on X", starting broad and narrowing until the set is small
enough to drill. That is a different motion -- you do not know the answer you
are looking for, you are looking at the shape of the bank -- and it wants
filters you can pile on and peel off rather than a query you retype.

Two rules carry the defaults:

  - **OR within a kind.** `topic:lbo` plus `topic:ma` gives you both. Selecting
    a second value of the same kind can only ever widen the set, which is what
    people expect from a filter list and is the opposite of what AND would do
    (two topics AND-ed is always empty, and a UI whose obvious gesture returns
    nothing teaches you not to use it).
  - **AND across kinds.** `topic:lbo` plus `tag:covenants` gives you the five
    questions in the overlap. Narrowing is what stacking is for.

Both are defaults rather than laws, and `Match` is where they are set. The
groups can be OR-ed instead (`topic:valuation` OR `tag:leases` -- everything
on either, which is how a study set gets assembled rather than narrowed), and
a kind can AND its own values instead (`tag:covenants` AND `tag:lbo-returns`
-- only the questions carrying both). Two levels of boolean, which is as much
as a filter list can express without becoming a formula editor, and enough for
every question anyone has actually wanted to ask of this bank.

`status` sits outside all of it and is always AND-ed. It defaults to `active`,
and a rejected question reaching a drill because it was OR-ed in from the other
side of the expression is exactly the failure the default exists to prevent.

Tags are hierarchy-aware: a filter on a family matches every tag beneath it
(`tagging.descendants`), so `tag:accounting` catches a question tagged only
`dta-dtl`. Without that the tree would be decoration.

The counts attached to each option are counts WITHIN the current set, not
within the bank. That is the number that tells you whether adding a filter is
worth the keystroke, and it is what makes stacking feel like it is responding
to you.
"""
from __future__ import annotations

import sqlite3
from typing import NamedTuple

from . import llm, tagging

# A filter is (kind, value). Kinds are closed: an unknown one is a bug in the
# caller rather than something to guess at.
KINDS = ("topic", "tag", "kind", "status", "flag")

class Match(NamedTuple):
    """How the filters combine: one word for the groups, a set for the kinds.

    `outer` joins the kind groups -- "all" is AND (narrow), "any" is OR
    (widen). `alls` names the kinds that AND their own values instead of
    OR-ing them.

    Flags start in `alls` because that is what they have always done and it is
    what they mean: `unseen` plus `disputed` is a question about the overlap,
    not about the union. Saying so here rather than in the SQL is the point --
    the rule used to be documented as "OR within a kind" while one kind
    quietly did the opposite, and nothing on screen said so.
    """
    outer: str = "all"
    alls: frozenset[str] = frozenset({"flag"})

    @property
    def any_of(self) -> bool:
        return self.outer == "any"

    def all_within(self, kind: str) -> bool:
        return kind in self.alls

    def flipped(self) -> "Match":
        return self._replace(outer="all" if self.any_of else "any")

    def toggled(self, kind: str) -> "Match":
        alls = set(self.alls) ^ {kind}
        return self._replace(alls=frozenset(alls))

    @classmethod
    def of(cls, tags_all: bool = False, any_of: bool = False) -> "Match":
        """The CLI's two flags, as a Match."""
        alls = {"flag"} | ({"tag"} if tags_all else set())
        return cls(outer="any" if any_of else "all", alls=frozenset(alls))


DEFAULT = Match()

# The flags are the questions you ask about a set rather than about a question:
# what is due, what have I never seen, where am I weak, what is disputed.
# Each one is a bare boolean expression, not a ` AND ...` fragment: a filter
# that can only be appended to a conjunction is a filter that cannot be OR-ed
# with anything, and the joiner is now the user's to choose.
FLAGS = {
    "due": """
        EXISTS (SELECT 1 FROM schedule s WHERE s.question_id = q.id
                 AND s.due_at IS NOT NULL AND s.due_at <= datetime('now'))
    """,
    "unseen": """
        NOT EXISTS (SELECT 1 FROM reviews rv WHERE rv.question_id = q.id)
    """,
    "weak": """
        EXISTS (SELECT 1 FROM reviews rv WHERE rv.question_id = q.id
                 AND rv.rating IS NOT NULL
              GROUP BY rv.question_id HAVING AVG(rv.rating) < 3.0)
    """,
    # The cross-audit's whole output is the handful where two models disagree;
    # being able to filter a browse down to those is how they get worked
    # through rather than admired in a summary.
    "disputed": f"""
        EXISTS (
            SELECT 1 FROM audits c
             WHERE c.question_id = q.id
               AND c.provider IN {llm.SECOND_SQL}
               AND c.id = (SELECT MAX(c2.id) FROM audits c2
                            WHERE c2.question_id = q.id AND c2.provider = c.provider)
               AND c.verdict != COALESCE(
                   (SELECT g.verdict FROM audits g
                     WHERE g.question_id = q.id AND g.provider IN {llm.PRIMARY_SQL}
                  ORDER BY g.id DESC LIMIT 1), c.verdict)
        )
    """,
    "untagged": """
        NOT EXISTS (SELECT 1 FROM question_tags qt WHERE qt.question_id = q.id)
    """,
}

FLAG_HELP = {
    "due": "the schedule says today",
    "unseen": "never drilled",
    "weak": "averaging under 3",
    "disputed": "the two auditors differ",
    "untagged": "carries no tag",
}


def split(facets: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Group the filter list by kind, preserving the order they were added."""
    out: dict[str, list[str]] = {}
    for kind, value in facets:
        out.setdefault(kind, [])
        if value not in out[kind]:
            out[kind].append(value)
    return out


def _has_tag(conn: sqlite3.Connection, name: str) -> tuple[str, list]:
    """One tag filter, expanded to its whole subtree."""
    family = sorted(tagging.descendants(conn, name))
    marks = ",".join("?" * len(family))
    return (f"EXISTS (SELECT 1 FROM question_tags qt "
            f"JOIN tags tg ON tg.id = qt.tag_id "
            f"WHERE qt.question_id = q.id AND tg.name IN ({marks}))"), family


def _join(parts: list[tuple[str, list]], word: str) -> tuple[str, list]:
    """Fold clause fragments into one parenthesised expression."""
    parts = [p for p in parts if p[0].strip()]
    if not parts:
        return "", []
    sql = f" {word} ".join(f"({p[0].strip()})" for p in parts)
    params: list = []
    for p in parts:
        params.extend(p[1])
    return (sql if len(parts) == 1 else f"({sql})"), params


def _group(conn: sqlite3.Connection, kind: str, values: list[str],
           match: Match) -> tuple[str, list]:
    """One kind's filters, joined by that kind's own word."""
    word = "AND" if match.all_within(kind) else "OR"
    if kind in ("topic", "kind"):
        # COALESCE, because that is the name the option list offered you: a
        # question with no topic is shown as `general`, and a filter that could
        # not match what it was named after is a filter that quietly does
        # nothing.
        default = "general" if kind == "topic" else "technical"
        marks = ",".join("?" * len(values))
        return f"COALESCE(q.{kind}, '{default}') IN ({marks})", list(values)
    if kind == "tag":
        return _join([_has_tag(conn, v) for v in values], word)
    if kind == "flag":
        return _join([(FLAGS[v], []) for v in values if v in FLAGS], word)
    return "", []


def where(conn: sqlite3.Connection, facets: list[tuple[str, str]],
          *, match: Match = DEFAULT, exclude: bool = False) -> tuple[str, list]:
    """The WHERE tail and its parameters, for a query aliasing questions as q.

    `exclude` inverts the filter half and leaves the status half alone, which
    is what "how many would this filter *add*" is counted against when the
    groups are OR-ed together.
    """
    by = split(facets)

    # Status is never part of the expression the joiner applies to. Defaulting
    # to active is what stops a rejected question being drillable, and an
    # `any` that could OR its way past that default would undo it.
    statuses = by.get("status") or ["active"]
    marks = ",".join("?" * len(statuses))
    sql = f" AND q.status IN ({marks})"
    params = list(statuses)

    groups = [_group(conn, kind, by[kind], match)
              for kind in ("topic", "kind", "tag", "flag") if by.get(kind)]
    body, body_params = _join(groups, "AND" if not match.any_of else "OR")
    if body:
        sql += f" AND {'NOT ' if exclude else ''}({body})"
        params.extend(body_params)
    return sql, params


def matching(conn: sqlite3.Connection, facets: list[tuple[str, str]],
             *, match: Match = DEFAULT, limit: int | None = None) -> list[dict]:
    """Every question the stacked filters agree on."""
    tail, params = where(conn, facets, match=match)
    sql = (f"SELECT q.id, q.canonical_text, q.topic, q.status, q.kind "
           f"FROM questions q WHERE 1=1 {tail} "
           f"ORDER BY q.topic, q.id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, params)]


def ids(conn: sqlite3.Connection, facets: list[tuple[str, str]],
        *, match: Match = DEFAULT) -> list[int]:
    tail, params = where(conn, facets, match=match)
    return [r["id"] for r in conn.execute(
        f"SELECT q.id FROM questions q WHERE 1=1 {tail}", params)]


# ---------------------------------------------------------------- options

def options(conn: sqlite3.Connection, facets: list[tuple[str, str]],
            *, match: Match = DEFAULT) -> dict[str, list[dict]]:
    """What you could add next, counted against what adding it would do.

    While the groups are AND-ed, that is "how many of the questions currently
    on screen carry this": adding a filter can only narrow, so the count is
    what would survive. A count equal to the whole set means the filter would
    do nothing; a count of zero means it is not offered at all.

    While they are OR-ed it has to be the other number -- how many the filter
    would *bring in* -- because adding one can only widen, and counting inside
    a set the option is about to grow would report the overlap and call it the
    reason to press the key. Same query, `exclude`-d.
    """
    tail, params = where(conn, facets, match=match, exclude=match.any_of)
    base = f"FROM questions q WHERE 1=1 {tail}"
    chosen = split(facets)

    def rows(sql: str, extra: list | None = None) -> list[dict]:
        return [dict(r) for r in conn.execute(sql, params + (extra or []))]

    out: dict[str, list[dict]] = {}

    out["topic"] = [r for r in rows(
        f"SELECT COALESCE(q.topic, 'general') AS name, COUNT(*) AS n {base} "
        f"GROUP BY 1 ORDER BY n DESC, 1")
        if r["name"] not in chosen.get("topic", [])]

    out["kind"] = [r for r in rows(
        f"SELECT COALESCE(q.kind, 'technical') AS name, COUNT(*) AS n {base} "
        f"GROUP BY 1 ORDER BY n DESC, 1")
        if r["name"] not in chosen.get("kind", [])]

    # Tags come back as a two-level forest. Both levels are counted with their
    # own COUNT(DISTINCT q.id) against the same filtered base rather than by
    # summing children up: a question tagged both `wacc` and `beta` must count
    # once toward `intrinsic-value`, not twice, and summing would say twice.
    tagged = ("FROM questions q "
              "JOIN question_tags qt2 ON qt2.question_id = q.id "
              "JOIN tags tg ON tg.id = qt2.tag_id "
              "LEFT JOIN tags p ON p.id = tg.parent_id "
              f"WHERE 1=1 {tail}")
    # A tag with no parent has no family to sit under, and a question tagged
    # directly with a family tag belongs to that family rather than to nothing.
    family_expr = (f"COALESCE(p.name, CASE WHEN tg.kind = '{tagging.FAMILY_KIND}' "
                   f"THEN tg.name END, 'unfiled')")
    families = rows(f"SELECT {family_expr} AS name, COUNT(DISTINCT q.id) AS n "
                    f"{tagged} GROUP BY 1")
    leaves = rows(f"SELECT tg.name AS name, {family_expr} AS family, "
                  f"COUNT(DISTINCT q.id) AS n {tagged} "
                  f"AND tg.kind != '{tagging.FAMILY_KIND}' GROUP BY tg.id")
    out["tag"] = _forest(families, leaves, chosen.get("tag", []))

    out["flag"] = []
    for flag in FLAGS:
        if flag in chosen.get("flag", []):
            continue
        n = conn.execute(
            f"SELECT COUNT(*) c {base} AND ({FLAGS[flag]})", params).fetchone()["c"]
        if n:
            out["flag"].append({"name": flag, "n": n, "help": FLAG_HELP[flag]})
    return out


def _forest(families: list[dict], leaves: list[dict],
            chosen: list[str]) -> list[dict]:
    """Assemble the two counted levels into the tree the picker walks.

    Anything whose parent was never set lands under `unfiled`, on purpose. A
    new tag that silently vanished from the browser would be worse than an
    untidy heading, because you would only notice when a drill came up short.
    """
    by_family: dict[str, list[dict]] = {}
    for leaf in leaves:
        by_family.setdefault(leaf["family"], []).append(
            {"name": leaf["name"], "n": leaf["n"]})

    out = []
    for fam in families:
        kids = sorted(by_family.get(fam["name"], []),
                      key=lambda c: (-c["n"], c["name"]))
        out.append({"name": fam["name"], "n": fam["n"],
                    "children": [c for c in kids if c["name"] not in chosen]})
    out.sort(key=lambda f: (f["name"] == "unfiled", -f["n"], f["name"]))
    return [f for f in out if f["children"] or f["name"] not in chosen]


def counting_scope(facets: list[tuple[str, str]], kind: str,
                   match: Match) -> tuple[list[tuple[str, str]], bool]:
    """What a fixed column of options is counted against, and whether to invert.

    Every row in such a column is one keystroke from being pressed, so its
    number has to be what pressing it would do. Three cases, and only the last
    of them is "count what is on screen now":

    - **The groups are OR-ed.** Pressing anything widens the whole expression,
      so the number is what it would bring in from outside it: the same
      `exclude`d count `options()` takes.
    - **The groups are AND-ed and this kind ORs its own values.** Pressing an
      option widens *within* its group, so the number is counted against every
      filter except this group's. Counting inside the current set instead
      reports the overlap -- and for a column like topic, where a question has
      exactly one, that overlap is structurally zero for every row not already
      marked. Mark `accounting` and all nine remaining chapters read `0` while
      each of them is one keystroke from adding between 48 and 190 questions.
    - **The groups are AND-ed and this kind ANDs its own values.** Pressing an
      option can only narrow, so what survives inside the current set is both
      the honest number and the useful one.

    Dropping the group is right for the rows already marked as well as the
    ones that are not: with the group gone, a marked value is counted against
    the same remaining filters, which is exactly the share it contributes to
    the union. So one query serves the whole column.
    """
    if match.any_of:
        return list(facets), True
    if match.all_within(kind):
        return list(facets), False
    return [f for f in facets if f[0] != kind], False


def topic_counts(conn: sqlite3.Connection, facets: list[tuple[str, str]],
                  *, match: Match = DEFAULT) -> dict[str, int]:
    """Every topic's count under the current filters, zero included.

    `options()`'s topic list drops a topic once its count is zero -- right
    for a list you are picking the next filter from, wrong for a fixed
    12-row column that has to keep showing every topic whether or not the
    current filters have anything left in it.

    Counted through `counting_scope`, so the number against a topic is what
    marking it would do rather than what it overlaps with today. See there for
    why those are different numbers and why the difference was always zero.
    """
    scope, exclude = counting_scope(facets, "topic", match)
    tail, params = where(conn, scope, match=match, exclude=exclude)
    rows = conn.execute(
        f"SELECT COALESCE(q.topic, 'general') AS name, COUNT(*) AS n "
        f"FROM questions q WHERE 1=1 {tail} GROUP BY 1", params)
    return {r["name"]: r["n"] for r in rows}


def kind_counts(conn: sqlite3.Connection, facets: list[tuple[str, str]],
                 *, match: Match = DEFAULT) -> dict[str, int]:
    """Every kind's count under the current filters, zero included.

    Same reason `topic_counts` exists: the Type row above the tree is a fixed
    three rows (technical/behavioural/market_awareness), and `options()`'s
    kind list drops one the moment its count under the current filters hits
    zero -- right for a list you are picking the next filter from, wrong for
    a row that has to keep showing up either way.

    Counted through `counting_scope` for the same reason `topic_counts` is:
    kinds OR their own values too, so once one chapter is marked the other two
    were reporting their overlap with it, which is nothing.
    """
    scope, exclude = counting_scope(facets, "kind", match)
    tail, params = where(conn, scope, match=match, exclude=exclude)
    rows = conn.execute(
        f"SELECT COALESCE(q.kind, 'technical') AS name, COUNT(*) AS n "
        f"FROM questions q WHERE 1=1 {tail} GROUP BY 1", params)
    return {r["name"]: r["n"] for r in rows}


def describe(facets: list[tuple[str, str]], match: Match = DEFAULT) -> str:
    """The filter stack as one readable line, for the static/piped rendering.

    It says what the query does, including the joiners. The old version wrote
    ` or ` between two topics and ` AND ` between the kinds regardless of what
    the SQL had been asked to do, which made it a caption rather than a
    description.
    """
    if not facets:
        return "everything active"
    by = split(facets)
    bits = []
    for kind in KINDS:
        values = by.get(kind)
        if not values or kind == "status":
            continue
        joiner = " AND " if match.all_within(kind) else " or "
        bits.append(f"{kind}: " + joiner.join(values))
    outer = f"   {'OR' if match.any_of else 'AND'}   "
    line = outer.join(bits)
    # Status is not part of the expression, so it is not joined into it.
    if by.get("status"):
        line += ("   ·   " if line else "") + "status: " + " or ".join(by["status"])
    return line
