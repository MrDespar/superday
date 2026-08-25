"""Timed mock interview.

Differs from drilling in the ways that matter: no rubric shown before you
answer, a clock running, follow-ups that press on the weakest part of what you
just said, and a scorecard at the end instead of feedback after each question.
Closer to the real thing, and much less comfortable.

The scorecard scores three things separately, because they fail separately and
you fix them differently:

  - **technical accuracy** -- did the answer contain the right substance
  - **communication** -- was it delivered in a way an MD would sit through
  - **resilience**    -- when pressed on the weak part, did it hold or fold

A candidate who is 80% technical and 40% resilient is not "a 60% candidate";
they are someone who knows the material and falls apart under a follow-up, and
that is a completely different evening's work.
"""
from __future__ import annotations

import random
import sqlite3
import time

from . import chains, grade, llm, session, ui
from .scheduler import record_review


def phrasing_for(conn, q) -> str:
    """One wording of this question, chosen at random from the ones on file.

    Same rule as `drill`: the bank stores every wording a source printed, and
    being asked "what is negative working capital" when you revised "walk me
    through working capital" is the whole point of keeping them. `recap` reads
    back which one you actually got, so a sitting is reconstructable.
    """
    rows = [r["text"] for r in conn.execute(
        "SELECT text FROM phrasings WHERE question_id = ?", (q["id"],))]
    return random.choice([q["canonical_text"], *rows]) if rows else q["canonical_text"]
from .ui import bad, dim, head, ok, question, rule, verdict, warn

ROUNDS = {
    "screen": {"minutes": 20, "count": 8, "spread": ["accounting", "ev_eqv", "valuation"]},
    "technical": {"minutes": 45, "count": 15,
                  "spread": ["accounting", "ev_eqv", "valuation", "dcf", "ma", "lbo"]},
    "superday": {"minutes": 60, "count": 20,
                 "spread": ["accounting", "ev_eqv", "valuation", "dcf", "ma", "lbo",
                            "markets", "behavioural"]},
    # A real superday is not all technicals, and fit is where most candidates
    # actually lose the offer.
    "fit": {"minutes": 25, "count": 10, "spread": ["behavioural"]},
    # Product rounds. A DCM desk does not open with a merger model and an ECM
    # desk does not open with a debt schedule, so a mock that draws from the
    # generalist spread rehearses the wrong hour. Both keep `markets` in the
    # spread because both interviews genuinely open on where things are
    # trading.
    "dcm": {"minutes": 45, "count": 15,
            "spread": ["dcm", "markets", "accounting", "lbo"]},
    "ecm": {"minutes": 45, "count": 15,
            "spread": ["ecm", "markets", "valuation", "ev_eqv"]},
    # The mid-market round: how price is actually paid, not how it is modelled.
    "midmarket": {"minutes": 45, "count": 15,
                  "spread": ["deal_process", "ma", "valuation", "accounting"]},
}


PERSONAS = {
    "standard": {
        "blurb": "Standard investment banking interviewer. Balanced, direct, structured.",
        # How often a weak answer gets pressed on, and how weak it has to be.
        "followup_below": 0.70,
        "followup_rate": 1,
        "opening": "Let's get started. Answer as if you were speaking.",
    },
    "skeptical_md": {
        "blurb": "Skeptical MD. Cuts through buzzwords, presses on commercial logic.",
        "followup_below": 0.85,
        "followup_rate": 2,
        "opening": "I've heard the textbook version. Tell me what you actually think.",
    },
    "exacting_vp": {
        "blurb": "Exacting VP. Demands exact formulas, complete bridges, correct signs.",
        "followup_below": 0.90,
        "followup_rate": 2,
        "opening": "Be precise. I will stop you if a formula is wrong.",
    },
}


def _spellings(name: str) -> tuple[str, ...]:
    """A slice name, plus the separator it was probably meant to have.

    Topics are written with underscores (`ev_eqv`) and tags with hyphens
    (`deal-process`), so a round naming a slice gets it wrong roughly half the
    time. The failure was silent and expensive: an unmatched slice contributes
    nothing, the round quietly backfills from the random pool, and the mock
    rehearses a different interview than the one on the label.
    """
    base = name.strip().lower().lstrip("#")
    return tuple(dict.fromkeys((base, base.replace("_", "-"), base.replace("-", "_"))))


def pick(conn: sqlite3.Connection, spec: dict) -> list[sqlite3.Row]:
    """Spread across the round's slices, weighting frequency and real asks.

    A slice is a topic OR a tag, tried in that order. Topics are coarse and
    there are only nine of them, so a product round -- DCM, ECM, mid-market --
    has no topic to name and would otherwise fall straight through to the
    random filler and rehearse the wrong hour. Accepting a tag as a slice is
    what makes those rounds ask what they claim to ask.

    `spec["ids"]` restricts the whole pool to an explicit set, which is how a
    filtered `browse` hands its selection to a mock.
    """
    pool = spec.get("ids")
    pool_clause = ""
    if pool is not None:
        pool_clause = (" AND q.id IN (" + ",".join(str(int(i)) for i in pool) + ")"
                       if pool else " AND 0")
    slices = spec["spread"]
    per_slice = max(1, spec["count"] // max(len(slices), 1))
    picked: list[sqlite3.Row] = []
    seen: set[int] = set()
    for slice_name in slices:
        names = _spellings(slice_name)
        marks = ",".join("?" * len(names))
        rows = list(conn.execute(
            "SELECT q.*, (SELECT COUNT(DISTINCT source_id) FROM question_sources "
            "  WHERE question_id = q.id) AS frequency "
            "FROM questions q WHERE q.status = 'active' " + pool_clause + " AND ("
            f"  q.topic IN ({marks}) OR EXISTS ("
            "     SELECT 1 FROM question_tags qt JOIN tags t ON t.id = qt.tag_id "
            f"      WHERE qt.question_id = q.id AND LOWER(t.name) IN ({marks})) ) "
            "ORDER BY (q.origin = 'interviewer_asked') DESC, frequency DESC, RANDOM() "
            "LIMIT ?", (*names, *names, per_slice)
        ))
        for r in rows:
            if r["id"] not in seen:
                picked.append(r)
                seen.add(r["id"])
    if len(picked) < spec["count"]:
        for r in conn.execute(
            "SELECT q.*, 0 AS frequency FROM questions q WHERE q.status='active'"
            + pool_clause + " ORDER BY RANDOM() LIMIT ?", (spec["count"] * 2,)
        ):
            if r["id"] not in seen:
                picked.append(r)
                seen.add(r["id"])
            if len(picked) >= spec["count"]:
                break
    # A lead-in that made the cut is asked before the question that needs it.
    return chains.order(picked[: spec["count"]])


def run(conn: sqlite3.Connection, round_name: str = "technical",
        persona: str = "standard", local: bool = False,
        ids: list[int] | None = None) -> None:
    spec = dict(ROUNDS.get(round_name, ROUNDS["technical"]))
    if ids is not None:
        # A mock over a browse selection keeps the round's clock and persona
        # but draws only from what you filtered to, and cannot ask for more
        # questions than the selection holds.
        spec["ids"] = ids
        spec["count"] = min(spec["count"], len(ids)) or spec["count"]
    p = PERSONAS.get(persona, PERSONAS["standard"])
    questions = pick(conn, spec)
    if not questions:
        print(warn("no active questions, run `superday ingest` first"))
        return

    # `local` is the same guarantee drilling makes: no call leaves the machine.
    # A mock is the most expensive thing this tool does -- twenty questions and
    # every follow-up is a graded call -- so being able to rehearse the format
    # without paying for it matters more here, not less.
    graded = llm.available() and not local
    started = time.time()
    budget = spec["minutes"] * 60
    sid = session.open_session(conn, "mock", [q["id"] for q in questions],
                               {"round": round_name, "persona": persona})

    print(rule("="))
    print(f"  {head('MOCK: ' + round_name.upper())}   {len(questions)} questions   "
          f"{spec['minutes']} minutes   {dim('[' + persona + ']')}")
    print(dim(f"  {p['blurb']}"))
    print(dim("  No rubric until the end. Answer as if speaking. q to abandon."))
    if not graded:
        print(warn("  (local: answers recorded, you self-rate at the end)") if local
              else warn("  (no API key: answers recorded, self-rated at the end)"))
    print(rule("="))
    print(f"\n  {head(p['opening'])}")

    transcript = []
    for i, q in enumerate(questions, 1):
        left = budget - (time.time() - started)
        if left <= 0:
            print(bad("\n  time is up"))
            break
        left_str = f"{int(left // 60)}m{int(left % 60):02d}s left"
        print(f"\n[{i}/{len(questions)}]  {warn(left_str) if left < 120 else dim(left_str)}")
        print(rule())
        # A follow-up asked cold is unanswerable in a mock for the same reason
        # it is in a drill, and here it also costs a graded minute.
        prior = chains.lead_in(conn, q["id"])
        for p_q in prior:
            # Dimmed line by line, not around the block: the shell splits on
            # newlines, so one colour code wrapped round the whole paragraph
            # leaves every middle line undressed.
            for line in ui.wrap("you have just answered: "
                                + " ".join(p_q["canonical_text"].split())).split("\n"):
                print(dim(line))
        if prior:
            print()
        # The wording an interviewer would use, which is not always the one on
        # the card. `drill` has served a random phrasing for realism since the
        # phrasings table existed; a mock, where you are being timed and cannot
        # ask for the question again, is where being asked it in unfamiliar
        # words matters most, and it was the one screen still reading the
        # canonical every time.
        asked = phrasing_for(conn, q)
        print(question(asked))
        t0 = time.time()
        try:
            answer = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(dim("\n  abandoned"))
            break
        if answer.lower() in {":q", "q", "quit", "exit"}:
            print(dim("\n  abandoned"))
            break
        if answer.lower() in {"skip", "pass"}:
            answer = ""
        elapsed = time.time() - t0

        result = grade.grade(conn, q["id"], answer, persona=persona) if (graded and answer) else None
        followups: list[dict] = []
        if result and "error" not in result:
            followups = _press(conn, q, result, p, persona)
            result["followups"] = followups
        # The wording actually asked goes into the transcript, not just onto
        # the screen. `recap` reads `reviews.phrasing` back to reconstruct a
        # sitting, and the scorecard is where that row gets written -- so the
        # phrasing has to survive the trip from the question loop to it.
        transcript.append((q, asked, answer, elapsed, result))
        session.record(conn, sid, q["id"], (result or {}).get("suggested_rating"),
                       elapsed, graded=bool(result))

    session.close(conn, sid, note=f"{round_name}/{persona}")
    scorecard(conn, transcript, round_name, time.time() - started, graded, persona)


def _press(conn, q, result: dict, p: dict, persona: str) -> list[dict]:
    """Follow up on a weak answer, up to this persona's appetite for it.

    This is the part that makes it an interview rather than a quiz: the
    follow-up is aimed at what you just failed to say, and how you handle it is
    scored separately from the original answer.
    """
    out: list[dict] = []
    pending = result.get("followup")
    for _ in range(p["followup_rate"]):
        if not pending or result["score"] >= p["followup_below"]:
            break
        print(f"\n  {warn('follow-up:')} {pending}")
        try:
            reply = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not reply or reply.lower() in {"q", "skip", "pass"}:
            out.append({"question": pending, "answer": reply, "score": 0.0})
            break
        # Graded against the same rubric: the follow-up is asking for the part
        # that was missing, so "did they now say it" is exactly the question.
        follow = grade.grade(conn, q["id"], reply, persona=persona)
        score = follow["score"] if follow and "error" not in follow else None
        out.append({"question": pending, "answer": reply, "score": score})
        pending = (follow or {}).get("followup")
        if score is None:
            break
    return out


def _score_color(frac: float):
    return ok if frac >= 0.66 else warn if frac >= 0.33 else bad


def axes(entries: list[dict]) -> dict:
    """The three scores, and the evidence behind each.

    An axis is None rather than 0 when nothing was measured. A mock where no
    answer was ever pressed has no resilience reading, and printing 0% for that
    would be a lie about your weakest area -- the one number on this screen you
    are most likely to act on.

    Technical and communication are still measurable with no API key: the
    self-rating carries technical, and delivery is read from the answer text
    locally. Only resilience genuinely needs grading, since it depends on a
    follow-up that only a grader can aim.
    """
    tech: list[float] = []
    comm: list[float] = []
    resilience: list[float] = []
    pressed = 0

    for e in entries:
        if e.get("score") is not None:
            tech.append(e["score"])
        structure = e.get("structure")
        if structure:
            comm.append((structure - 1) / 4)
        for f in e.get("followups", []):
            pressed += 1
            if f.get("score") is None:
                resilience.append(0.0)
                continue
            # Recovering under pressure is the point: what matters is whether
            # the follow-up landed the material, not whether it beat the
            # original answer by some margin.
            resilience.append(f["score"])

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    return {
        "technical": mean(tech),
        "communication": mean(comm),
        "resilience": mean(resilience),
        "pressed": pressed,
        "answered": len(tech),
    }


def scorecard(conn, transcript, round_name, elapsed, graded, persona="standard") -> None:
    if not transcript:
        return
    print("\n" + rule("="))
    print(f"  {head('SCORECARD')}   {round_name}   {dim('[' + persona + ']')}   "
          f"{int(elapsed // 60)}m{int(elapsed % 60):02d}s")
    print(rule("="))

    by_topic: dict[str, list[float]] = {}
    entries: list[dict] = []
    for q, asked, answer, secs, result in transcript:
        topic = q["topic"] or "general"
        if result and "error" not in result:
            score = result["score"]
            mark = {"strong": "STRONG", "adequate": "OK", "weak": "WEAK",
                    "wrong": "WRONG"}.get(result["verdict"], "?")
            print(f"\n  [{verdict(f'{mark:6s}')}] {score:.0%}  {int(secs)}s  "
                  + ui.truncate(q["canonical_text"], 52))
            for hit, point in zip(result["rubric_hits"], result["rubric"]):
                tag = ok("hit ") if hit else bad("MISS")
                print(f"      {tag}  {point[:64]}")
            if result.get("structure_note"):
                print(dim(f"      delivery: {result['structure_note'][:120]}"))
            if result.get("feedback"):
                print(dim(f"      -> {result['feedback'][:180]}"))
            for f in result.get("followups", []):
                s = f"{f['score']:.0%}" if f.get("score") is not None else " -- "
                print(dim(f"      pressed: {s}  {f['question'][:70]}"))
            by_topic.setdefault(topic, []).append(score)
            entries.append({"score": score, "structure": result.get("structure"),
                            "followups": result.get("followups", [])})
            rating = result["suggested_rating"]
        else:
            print(f"\n  [{dim('   -  ')}]  {int(secs)}s  "
                  + ui.truncate(q["canonical_text"], 52))
            if not graded:
                try:
                    raw = input("      self-rate 1-4 > ").strip()
                except (EOFError, KeyboardInterrupt):
                    raw = ""
                rating = int(raw) if raw in {"1", "2", "3", "4"} else 3
                by_topic.setdefault(topic, []).append((rating - 1) / 3)
                entries.append({"score": (rating - 1) / 3,
                                "structure": grade.structure_floor(answer),
                                "followups": []})
            else:
                rating = 3
        record_review(
            conn, q["id"], rating, phrasing=asked, user_answer=answer or None,
            score=(result or {}).get("score"),
            rubric_hits=(result or {}).get("rubric_hits"),
            grader=llm.model_grade() if (result and "error" not in result) else "self",
        )

    a = axes(entries)
    lines: list[str] = []
    for label, key, note in (
        ("technical", "technical", f"{a['answered']} answered"),
        ("communication", "communication", "delivery"),
        ("resilience", "resilience",
         f"pressed {a['pressed']}x" if a["pressed"] else "never pressed"),
    ):
        val = a[key]
        bar = ui.meter(val, 20) if val is not None else dim("\u00b7" * 20)
        pct = f"{val:>4.0%}" if val is not None else dim("   -")
        lines.append(f"  {label:<14}{bar} {pct}   " + dim(note))

    lines.append("")
    lines.append(head("BY TOPIC"))
    for topic, scores in sorted(by_topic.items(), key=lambda kv: sum(kv[1]) / len(kv[1])):
        avg = sum(scores) / len(scores)
        lines.append(f"  {topic:<14}{ui.meter(avg, 20)} {avg:>4.0%}  "
                     + dim(f"{len(scores)} asked"))

    overall = [s for v in by_topic.values() for s in v]
    footer = None
    if overall:
        avg_overall = sum(overall) / len(overall)
        lines.append("")
        lines.append("  " + head("OVERALL") + "  "
                     + _score_color(avg_overall)(f"{avg_overall:.0%}")
                     + dim(f"   {_verdict_line(avg_overall, a)}"))
        weakest = min(by_topic.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))[0]
        footer = (dim("next: ") + ui.style(f"superday drill -t {weakest}", ui.BOLD)
                  + dim(" · ") + ui.style("drill --weak", ui.BOLD))
    print("\n" + ui.window(f"MOCK SCORECARD  {round_name.upper()}", lines, footer=footer))


def _verdict_line(overall: float, a: dict) -> str:
    """Name the failure mode rather than restating the number.

    "62%" tells you nothing you can act on. "You know it and cannot say it
    under pressure" tells you what tomorrow evening is for.
    """
    tech = a["technical"]
    comm = a["communication"]
    res = a["resilience"]
    if tech is not None and comm is not None and tech - comm > 0.25:
        return "you know it, but it is not landing -- structure the delivery"
    if tech is not None and comm is not None and comm - tech > 0.25:
        return "it sounds good and the substance is thin"
    if res is not None and tech is not None and tech - res > 0.25:
        return "solid first answers, folds when pressed -- drill the follow-ups"
    if overall >= 0.8:
        return "interview ready on this material"
    if overall >= 0.6:
        return "close: the gaps are specific, not general"
    return "the material is not in yet -- coverage before polish"
