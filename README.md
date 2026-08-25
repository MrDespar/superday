# superday

An investment-banking interview-prep CLI that drills a real question bank on a
real spaced-repetition schedule, in a full-screen terminal shell.

It is not a flashcard app with finance content pasted in.
It ships with 1,004 questions and grows from your own material - guides, handbooks, filings, forum threads - extracted, deduplicated, audited by a second model, and graded against a rubric rather than against a paragraph.
Everything lives in one SQLite file you own, and every model call goes to a
provider you choose with a key you own: **Gemini, Claude or OpenAI**, one
setting, no vendor lock-in.

![superday: drilling a question, browsing the bank, and the readiness dashboard](docs/demo.gif)

```
$ superday
> drill -t dcf
> browse
> mock dcm
> dashboard
```

## What it does

You point it at your own material.
`ingest-pdf`, `ingest-epub`, `ingest-web` and `ingest` (docx) hand a document to a model and keep only the question/answer pairs it can ground in the source text.
`ingest-filing` uses no model at all: it reads a company's XBRL facts from SEC EDGAR and computes the answers locally.

Everything that lands goes through an admission gate first, which resolves each candidate to new, duplicate or variant.
That is lexical, so it needs no key.
Then `enrich` fills in topic, difficulty and a rubric, and `audit` re-reads the result with a different model, because a model checking its own extraction agrees with itself.

You drill what comes out.
Answers are marked against the rubric point by point, FSRS decides when each question comes back, and `plan` tells you whether the pace fits before your date.
Pressing Enter and self-rating makes no network call.

## Install

Python 3.10 or newer.
Three dependencies and no SDKs - the LLM clients are raw `urllib`.

```sh
uv tool install git+https://github.com/MrDespar/superday.git      # or:
pipx install git+https://github.com/MrDespar/superday.git
```

Either gives you `superday` on your PATH with no clone and no virtualenv to manage.
To try it without installing anything: `uvx --from git+https://github.com/MrDespar/superday.git superday`.

Your database, config and keys go in `~/.local/share/superday/` (`$XDG_DATA_HOME` is respected, `SUPERDAY_HOME` overrides both).
Run it from inside a clone and it uses the clone instead, so installing the packaged build next to an existing checkout never moves your bank out from under you.

<details>
<summary>From a clone, for hacking on it</summary>

```sh
git clone https://github.com/MrDespar/superday.git
cd superday
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./superday                       # launches the shell
./superday selftest              # 380+ tests, no pytest, throwaway database
```

`./superday` is a `sh` launcher that resolves symlinks and execs the venv, so symlinking it onto your PATH works.
The zsh completion file at `completions/_superday` is generated from the parser by `superday completions --write`, and `selftest` fails when the two disagree.
</details>

## Bring your own model

One setting moves the whole tool: extraction, `enrich`, `audit`, grading and embeddings all follow it.

```sh
superday llm                  # all three, side by side
superday settings openai_api_key sk-...
superday llm --use openai     # gemini | claude | openai
superday llm --test           # does the key actually work?
```

| provider | key | free tier | embeddings |
|---|---|---|---|
| `gemini` (default) | `GEMINI_API_KEY` - [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | yes | yes |
| `claude` | `ANTHROPIC_API_KEY` - [console.anthropic.com](https://console.anthropic.com/settings/keys) | no | no |
| `openai` | `OPENAI_API_KEY` - [platform.openai.com](https://platform.openai.com/api-keys) | no | yes |

Only the provider you use needs a key, and switching provider moves the model defaults with it.
Keys are written to `.env.local` at `0600`, travel in a request header rather than a URL, display masked, and are redacted out of the shell history.

**You do not need a key at all.**
Drilling, mocks, search, browse, tagging, the admission gate, `ingest` (docx), `ingest-pack`, `ingest-filing` and `check` are all local.
A key buys four things: extraction from PDF/EPUB/web, `enrich`, `audit`, and grading a *typed* answer.

<details>
<summary>The parts that are deliberately not provider-agnostic</summary>

`cross-audit --api` runs on a provider that is **not** the one that gave the first opinion - Claude for preference, then OpenAI, then Gemini, from the keys you hold.
That pass exists to disagree with whoever wrote the answer, so following your setting would quietly make it the same model checking its own work.
`--using` overrides the choice.
Its default path needs no key at all: it exports a JSON batch, you or a coding agent judge it, and `--import` files the verdicts back.

Anthropic sells no embeddings endpoint, so on `claude` a `find --semantic` falls back to keyword search **and says so** rather than labelling keyword hits as semantic.

Each vendor is asked for structured output the way that vendor supports it - Gemini's `responseSchema`, a single forced tool on Claude, `strict` json_schema on OpenAI - so a rubric comes back parseable on all three rather than as prose you hope to regex.
That lives entirely in `ib/llm.py`; nothing else in the tool knows which vendor answered.
</details>

<details>
<summary>What gets sent where</summary>

- **Your chosen provider** receives source text during ingestion, and the question, rubric and your typed answer when you ask to be graded.
  `usage` shows every call made, counted from rows written after the request went out.
- **A second provider** receives question/answer pairs if you run `cross-audit --api`, whether or not it is your configured one.
  It names which one before it starts.
- **SEC EDGAR** receives a request identifying you by the contact address you set.
  The SEC requires automated clients to identify themselves, so `ingest-filing` refuses to run until you set one: `superday settings sec_contact you@example.com`.
- Nothing else leaves the machine.
  No telemetry, no account, no sync.
  `usage.jsonl` is a local file.
</details>

## Commands

`superday` with no arguments opens the shell; `superday <command>` runs the same thing one-shot, through the same parser.
Inside the shell, `?` prints the live list and the keymap.

<details>
<summary><b>Study</b> - drill, mock, browse, find, plan, dashboard</summary>

| | |
|---|---|
| `drill` | get asked questions. `-t <topic>`, `--tag`, `--weak`, `--local` (never calls out), `--resume`, `--again` (ignore the due window) |
| `mock` | timed mock interview with a scorecard. `mock dcm`, `mock ecm`, `mock midmarket` |
| `browse` | walk the bank by topic and tag, stacking filters |
| `find` | full-text search, porter-stemmed. `--semantic` where the provider sells embeddings |
| `list` | list topics, or drill one: `list dcf` |
| `show` | everything known about one question, as tabs |
| `recap` | what you have already answered and how it went: `recap`, `recap week`, `recap all` |
| `sessions` | drill and mock sittings, and which can be resumed |
| `plan` | the daily pace needed to be ready by your interview date |
| `dashboard` | readiness on one screen: mastery, retention, momentum, what is coming |

A drill folds each answered question to one line, so a twenty-question sitting is twenty lines rather than four hundred; `recap` holds everything else.
Every list is driven by the four arrow keys and Enter with no modifier chord anywhere, because terminal emulators and window managers eat those before the process sees them.
`browse` hands its selection to `drill --ids` and `mock --ids`, which restrict the pool and change nothing else about how a sitting is picked.
</details>

<details>
<summary><b>Getting questions in</b> - ingest, enrich, audit</summary>

| | |
|---|---|
| `ingest` | corpus docx |
| `ingest-pdf` | the PDF guides |
| `ingest-epub` | EPUB guides |
| `ingest-web` | a forum thread, article or saved page |
| `ingest-filing` | a company's filed XBRL figures, computed locally, no model involved |
| `ingest-pack` | an authored JSON pack. No API key, no provider call |
| `enrich` | real topics, difficulty and rubrics |
| `audit` | second-opinion QA over what was extracted |
| `reground` | re-read ingested PDFs to repair provenance and phrasings |
| `market` | seed and refresh market-awareness questions |

Each format does one job - turn its bytes into chunks - and everything after that is shared, so the grounding rule and the failure budget are the same whichever door a question came in through.
A paid call is never made twice: an ingest that hits a quota wall at chunk 30 of 60 resumes there rather than re-paying for the first 30.
</details>

<details>
<summary><b>Quality</b> - review, check, cross-audit, dupes, chains</summary>

| | |
|---|---|
| `review` | work the review queue |
| `accept-all` | accept everything pending, after showing what it will touch |
| `check` | answers that are provably wrong. Arithmetic and statement links only, no model, no opinion |
| `cross-audit` | an independent second opinion, stored beside the first verdict and never over it |
| `disagreements` | where the two audit passes differ, worst case first |
| `consult` | batch questions out to any chat model as Markdown, file the reply back |
| `dupes` | near-duplicates already in the bank, and phrasings that have drifted off the question they hang from |
| `chains` | question lines: follow-ups that cannot be asked without the question before them |
| `gate` | what the admission gate admitted, merged and dropped |
| `undo` | take back the last status, answer or wording change |

`check` is one-sided by design: a false positive teaches you to ignore the report, at which point the true positives stop mattering.
Every status, answer and wording write goes through a history table, so `undo` reverses a decision rather than a row.
</details>

<details>
<summary><b>Organise and inspect</b> - tags, edit, add, stats, export</summary>

| | |
|---|---|
| `tag` / `untag` / `tags` | the concept and firm taxonomy, in a two-level tree |
| `autotag` | apply the taxonomy across the bank. Lexical, no key |
| `add` | add a question you were actually asked. `--llm` spends one call to structure it |
| `edit` | a question's text, answer, topic or rubric |
| `stats` | what is in the bank |
| `usage` | how many provider calls you have made, and what got refused |
| `llm` | which provider answers, and whether its key works |
| `settings` | every knob in one place, with where each value came from |
| `export` | JSON, Markdown, a byte-exact `.sqlite` snapshot, or Anki |

`export --md` writes one file per topic, so a changed answer is a three-line diff rather than a hunk inside a megabyte.
Personal progress is opt-in via `--with-progress`, which makes the default export the one you can share.
</details>

## The bank you start with

Fourteen packs, 1,004 questions after the gate has deduplicated them, none of which need a key:

```sh
superday ingest-pack all
```

Six are authored or parsed from a question-shaped source:

| pack | questions | grounded in | what it covers |
|---|---:|---|---|
| `01-dcm-syndicate` | 103 | a third-party DCM syndicate desk handbook | bond math, new issue pricing, syndicate process, Schuldschein, hybrids, ratings, macro |
| `02-deal-mechanics` | 40 | authored | locked box, completion accounts, the working capital peg, UK/German public M&A |
| `03-ecm-europe` | 38 | Cleary Gottlieb's London ECM/DCM Execution Handbook | IPO process, free float, stabilisation, cornerstones, lock-ups |
| `04-levfin-private-credit` | 24 | Wall Street Prep, plus authored | unitranche, AAL, liability management, private credit |
| `05-hgb-dach` | 14 | authored | HGB vs IFRS, German tax, DACH accounting |
| `06-smallcap-valuation` | 15 | authored | illiquidity, private company comps, mid-market DCF |

The other eight are the generalist core, extracted from guides with `ingest-pdf` and `ingest`, then enriched, audited by a second model and reviewed by hand:

| pack | questions | extracted from |
|---|---:|---|
| `07-accounting` | 176 | the Breaking Into Wall Street Accounting Guide, a 400-question interview set, chapter Q&A sets on working capital and the statement walkthroughs, leasing notes |
| `08-valuation` | 172 | the BIWS Valuation Guide, the 400-question set, a valuation multiples Q&A set |
| `09-ma-merger-model` | 115 | the BIWS Merger Model Guide, the 400-question set, a 100-question technical guide |
| `10-ev-equity-value` | 95 | the BIWS Equity Value / Enterprise Value Guide, the 400-question set, four Q&A sets over the same ground |
| `11-lbo` | 94 | the BIWS LBO Model Guide, the 400-question set |
| `12-dcf` | 63 | the 400-question set, Q&A sets on DCF factors and multiples |
| `13-products` | 36 | the 400-question set, where it touches DCM, ECM and deal process |
| `14-interview-craft` | 24 | the 400-question set and interview and assessment centre session notes |

Every answer and rubric in those eight is the pipeline's wording rather than the source's, and no pack carries source text: `packs/_build_corpus.py` writes them out of a reviewed database and omits the `verbatim` field entirely, which `test_no_shipped_pack_reproduces_its_source_verbatim` holds it to.

`14-interview-craft` is deliberately the smallest of them.
A guide answers "what is your greatest weakness" with a candidate's own story, and a rubric over that story marks you down for not being that candidate, so what ships is the advice about the shape of an answer and never an answer in the first person.
The fit answer is the one thing in this bank nobody else can write for you.

Adding your own material is the same three commands the packs came out of:

```sh
superday settings corpus_dir ~/Documents/IB_Resources
superday ingest-pdf ~/Documents/IB_Resources/handbook.pdf
superday enrich && superday audit && superday review
```

Two hundred pages of guide is a few hundred questions.
The admission gate is what makes the second handbook over the same ground worth ingesting: overlapping questions attach to what the first one taught you as extra evidence rather than being asked again under a new id.

## Licence

MIT. See [LICENSE](LICENSE).

The question packs are content rather than code: rubrics and authored
questions are covered by the same licence, and the `note` on each pack records
what its factual content is grounded in. The eight extracted packs are
questions and rubrics the pipeline wrote from third-party guides, not those
guides' own words, and none of them reproduces source text. Market levels, tax
thresholds and regulatory numbers move - re-check any figure before quoting it
in a room.
