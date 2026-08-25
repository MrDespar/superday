Prompt for a future session (recommended: run with Opus, this is exactly the
kind of judgment-heavy, correctness-sensitive work it's suited for). Paste
this whole file as your instruction, or point an agent at it with
`read prompts/flesh-out.md and do it`.

---

# Mission

`superday` is a personal CLI I built for drilling investment-banking
interview questions: ingest question banks from PDFs/docx, extract
question/answer pairs with an LLM, grade my spoken-style answers against a
rubric, run timed mock interviews, and track spaced repetition. It's a real
tool I use, with a real SQLite database of my actual progress
(`ib.db` - do not treat this as a toy fixture).

It currently works but is thin: eleven-ish commands, a REPL, decent color,
and one LLM provider (Gemini, used everywhere - extraction, enrichment,
grading, and the existing "audit" QA pass). Your job is to make it
substantially better: a richer feature set, a more capable interactive
experience, and - the one non-negotiable piece - a genuine second opinion
from Claude on whether the question bank is actually *correct*, not just
well-formatted.

Do not treat this as "add a pile of features." Treat it as: understand what
this tool is for, find where it's weakest, and make deliberate, well-argued
improvements. I'd rather have five features done properly with tests than
fifteen done sloppily.

## Step 0 - orient yourself before proposing anything

Read, in this order:

1. `ib/cli.py` - every command, the REPL, the argparse wiring. This is the
   surface area you're extending.
2. `ib/ui.py` - the color/formatting toolkit. Anything you add must go
   through this, not raw ANSI codes or un-styled `print()`.
3. `ib/db.py` and `migrations/*.sql` - schema and the migration mechanism.
   Migrations are numbered `.sql` files, applied once, tracked in
   `_migrations`, idempotent by design. Never edit an already-applied
   migration; add a new numbered one.
4. `ib/llm.py` - the existing Gemini client. Read the module docstring
   carefully: "kept deliberately small and dependency-free... every call is
   JSON-schema constrained where the caller needs structure, because a
   grader that sometimes returns prose is a grader you cannot trust." Match
   this philosophy for whatever you add - no heavyweight SDK dependency
   where a ~100-line `urllib` client will do, and no unconstrained free-text
   LLM output where you need a structured verdict.
5. `ib/audit.py` - the existing QA pass, and its docstring: it deliberately
   runs a *different* model than the one that did the extraction, because "a
   model grading its own output agrees with itself." That principle is
   exactly why Claude needs to be in this system, not another Gemini call.
6. `ib/mock.py`, `ib/grade.py`, `ib/enrich.py`, `ib/scheduler.py`,
   `ib/admission.py` - the rest of the pipeline.
7. `ib/tests.py` - the existing test style (plain functions, `assert`-based,
   run via `superday selftest`). All 17 current tests must keep passing;
   whatever you add should get tests in the same style.
8. `completions/_superday` - the zsh completion file. Every new subcommand
   needs an entry here too, or it's a regression in UX, not an addition.

Also actually run the tool for five minutes: `./superday`, poke at `list`,
`stats`, `drill`, `mock`. Get a feel for what's clunky before you decide
what to build.

## The one required feature: a Claude cross-audit

I only have a `GEMINI_API_KEY` right now, no Anthropic key. Part of this
work is setting that up correctly, not assuming it exists.

**Why this matters more than anything else here:** this tool teaches me
answers. If the answer bank has a wrong fact in it (a bad DCF assumption, a
mixed-up formula, a hallucinated rubric point), I don't just get a bad
answer - I learn the wrong thing and walk into a real interview with it. The
existing `audit` command already tries to guard against this with Gemini
grading its own extraction, but that's a weaker check than it looks: same
vendor, correlated failure modes, no true independence. Claude reviewing the
same material is a materially different signal.

Build this:

1. **A small Anthropic client**, e.g. `ib/claude.py`, mirroring `ib/llm.py`'s
   shape: `available()`, `api_key()` reading `ANTHROPIC_API_KEY` via the same
   `load_env()` pattern from `.env.local`, a `generate()`-equivalent, retry
   and backoff on 429/5xx. Anthropic's API doesn't have Gemini's
   `responseSchema` field - use tool-use with a single forced tool
   (`tool_choice: {"type": "tool", "name": "..."}`) and an `input_schema` to
   get the same structural guarantee the Gemini path relies on. Don't pull
   in the `anthropic` SDK unless you have a good reason to break from the
   "dependency-free" convention already established - a raw HTTPS POST to
   `https://api.anthropic.com/v1/messages` is not much code.

2. **A cross-audit pass** that is explicitly a *second opinion*, not a
   replacement: it should not silently overwrite what Gemini's `audit`
   already decided. Store both verdicts. That probably means either a new
   `audits` table (provider, question_id, verdict, reason, confidence,
   ran_at - one row per (question, provider) pair, append-only) or new
   columns alongside the existing `audit_verdict`/`audit_reason`
   (`audit_verdict_claude`, `audit_reason_claude`, ...). Pick whichever fits
   the existing schema philosophy better - read the comment at the top of
   `migrations/001_init.sql` about "derived and disposable" vs "yours and
   permanent" tables before you decide, and add it as a new migration file.

3. **Surface disagreement, don't bury it.** The genuinely useful output of
   this feature is not "Opus also said keep" on 700 questions - it's the
   handful where the two models disagree, especially where Gemini's audit
   kept something Claude would reject, since that's the failure mode that
   actually hurts me. Design the CLI output (new command, e.g.
   `superday cross-audit`, or a `--model claude` flag on the existing
   `audit`) to foreground disagreements the way `ib/audit.py`'s
   `AUTO_APPLY_AT` confidence threshold already foregrounds low-confidence
   calls for human review. A `superday stats`-style summary
   ("812 agree, 14 disagree, 3 both flagged as wrong") is the right shape.

4. **Cost-consciousness**, since this is a paid API I don't have yet and
   Opus is not cheap: default to a `--limit` rather than silently walking
   the whole ~800-question bank on first run, follow the existing
   `IB_MODEL_ENRICH`/`IB_MODEL_GRADE` env-var override pattern (something
   like `IB_MODEL_AUDIT_CLAUDE`, defaulting to an Opus model id) so the model
   tier is swappable without a code change, and batch requests the same way
   `ib/audit.py` already does (`batch_size`) rather than one call per
   question.

5. **Setup path**: since I don't have the key yet, whatever you build should
   fail with the same kind of clear, actionable message `ib/llm.py` gives
   for a missing `GEMINI_API_KEY` - tell me where to get an Anthropic key
   and what to put in `.env.local`, don't stack-trace.

Match `ib/ui.py` conventions for all of this: `head()`/`ok()`/`warn()`/
`bad()`/`verdict()` for anything that touches disagreement severity, not new
one-off styling.

## Everything else is yours to propose - but propose before you build

For the rest - new commands, deeper existing ones, richer TUI, other
"connections" (external tools/integrations) - don't just start
implementing. Survey the codebase and the actual usage patterns, then come
back to me with a prioritized, reasoned list: what's weak, what you'd add,
roughly how big each piece is, and what tradeoffs it carries. Get my
sign-off before you build anything expensive, destructive, or
architecture-changing (schema changes beyond the cross-audit, new required
external dependencies, anything that touches `ib.db` in bulk). Small,
obviously-good, easily-reversible polish you can just do.

To seed your thinking - none of these are mandates, they're candidates, and
I'd rather you find better ones by actually reading the code than rubber-
stamp this list:

- **Disagreement/audit dashboard** - a proper view into cross-audit results
  over time, not just a one-shot run.
- **`superday show <id>`** - full detail on one question: every phrasing,
  every source it's corroborated by, its full review/rating history, both
  audit verdicts if present. Right now that history is scattered across
  `review`, `stats`, and raw SQL.
- **Undo / soft-delete** - `accept-all` now confirms before running (good),
  but there's still no way to undo a bad `reject` or a bad bulk accept
  short of hand-editing SQLite. A `superday undo` for the last mutating
  action, or a status history table, closes a real gap.
- **Backup/export** - `ib/config.py` says outright that the DB is kept out
  of iCloud on purpose because "SQLite and cloud sync corrupt each other."
  That's a correct call, but it currently means zero backup story. A
  `superday export` (JSON dump) / `superday import`, or a scheduled
  `.sqlite` snapshot command, is worth considering.
- **Tagging / search** - beyond `topic`, free-text tags and a
  `superday find <query>` full-text search across the bank.
- **Interview countdown / study plan** - `superday plan <date>` that spreads
  the current weak-topic backlog (see the `stats` "WEAKEST TOPICS" section
  and `_mastery_bar`) across the days remaining.
- **Export to Anki** - a `.tsv`/`.apkg` export so the bank is drillable
  outside this tool too.
- **A `superday config` command** - view/edit `config.local.json` from
  inside the REPL instead of hand-editing JSON.
- **Provider abstraction beyond just this one Opus feature** - if the
  cross-audit work above goes well, the same `--model` pattern could
  generalize to grading/enrichment too. Don't over-build this speculatively;
  only generalize if the cross-audit code makes it cheap to.

## Constraints - do not regress these

- `superday` with no args must keep launching the REPL (`ib/cli.py`'s
  `repl()`); one-shot invocation (`superday <cmd>`) must keep working
  identically. Both paths share one `argparse` parser - keep it that way.
- Every subcommand you add or change needs a matching entry in
  `completions/_superday` (subcommand description, and its own `_arguments`
  case if it takes flags/positionals).
- All new user-facing output goes through `ib/ui.py` helpers, not raw
  ANSI escapes or unstyled prose next to styled prose.
- `superday selftest` must pass (17+ tests today) after every change. Add
  tests for new logic in the same plain-`assert` style as `ib/tests.py`.
- Schema changes are additive migrations only (`migrations/00N_*.sql`),
  never edits to an already-applied migration file, and never a destructive
  change to `reviews`/`schedule`/`notes` - those are explicitly "yours and
  permanent" per the comment at the top of `001_init.sql`.
- This is a real database with real personal progress in it. Any bulk
  mutation needs the same confirm-before-you-commit-hundreds-of-rows
  treatment `accept-all` already has, not a silent `UPDATE ... WHERE`.
- Don't add a dependency where the existing hand-rolled `urllib` approach in
  `ib/llm.py` would do - that's a deliberate, stated design choice in this
  repo, not an oversight.
- Follow whatever this session's global instructions say about commits: do
  not commit or push unless explicitly asked, even after a big chunk of
  work lands cleanly.

## Definition of done

- I have an Anthropic client, a working cross-audit pass, and a CLI surface
  that makes disagreements between Gemini and Claude easy to see and act on
  - this is the one part of this prompt that must ship, not just be
  proposed.
- I've been given a prioritized list of everything else you'd build, with
  reasoning, before most of it gets built.
- `superday selftest` is green.
- `completions/_superday` and `ib/ui.py` usage are consistent with
  everything that shipped.
- Nothing got committed without me asking for it.
