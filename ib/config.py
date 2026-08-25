"""Paths and settings.

The corpus lives outside this repo on purpose: it is large, proprietary, and
sits in iCloud where it gets backed up. Code and database live here, out of
iCloud, because SQLite and cloud sync corrupt each other.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import theme

# What ships with the tool: the migrations and the authored packs. Inside the
# package rather than beside it, because an installed wheel has no "beside it"
# -- `pipx install` puts `ib/` in site-packages and nothing else anywhere.
PACKAGE = Path(__file__).resolve().parent


def home() -> Path:
    """Where *your* half lives: the database, the config, the usage log.

    Three answers, in order.

    `SUPERDAY_HOME` wins, for pointing a second install somewhere else.

    Then the checkout, if this is running from one. That is not a fallback, it
    is the promise that installing the packaged build next to an existing
    clone does not move somebody's bank out from under them -- `ib.db` is real
    personal progress, and a tool that relocates it on upgrade has lost it as
    far as its owner is concerned.

    Otherwise the XDG data directory, created on demand. That is the case a
    `uv tool install` lands in, where there is no repo to keep anything in.

    A function rather than a constant for the reason every env-backed thing
    here is one: bound at import it is read before anything has had a chance
    to set the variable.
    """
    env = os.environ.get("SUPERDAY_HOME")
    if env:
        return Path(env).expanduser()
    checkout = PACKAGE.parent
    if (checkout / ".git").exists() or (checkout / "ib.db").exists():
        return checkout
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    place = Path(base).expanduser() / "superday"
    place.mkdir(parents=True, exist_ok=True)
    return place


def local_config() -> Path:
    return home() / "config.local.json"

DEFAULTS = {
    "corpus_dir": str(Path.home() / "Desktop" / "IB_Resources"),
    # Resolved at load() rather than here: `home()` is a function.
    "inbox_dir": "",
    # Folders under corpus_dir worth ingesting. Slide decks and photos are deal
    # and formatting reference, not question material.
    "ingest_globs": [
        "*.docx",
        "HandBooks/*.pdf",
        "Breaking_Into_Wallstreet_Guide/*.pdf",
        "*.pdf",
        "VC/*.pdf",
        "Random_Topics/*.pdf",
    ],
    "exclude_globs": ["Slide_Decks/*", "Companies/*", "~$*"],
    "desired_retention": 0.9,
    # auto: grade a typed answer when a key is configured.
    # off:  never call out, ever -- self-rate against the stored rubric.
    # Drilling is free either way when you press Enter rather than typing an
    # answer; this is the standing preference for when you do type one.
    "grade_mode": "auto",
    # The SEC requires automated clients to identify themselves with a contact
    # address and returns 403 without one. This is their stated access
    # condition, so it is met rather than worked around.
    #
    # Empty on purpose, and `ingest-filing` refuses to run until you fill it in
    # with `settings sec_contact you@example.com`. Shipping someone else's
    # address as the default would sign every user's EDGAR traffic with it,
    # which misidentifies the client to the SEC and hands one person the
    # rate-limiting and the abuse complaints for everybody.
    "sec_contact": "",
    # fold: a question you have answered folds to one line in the shell -- the
    #       rating, the time it took and the question, with `recap` holding
    #       everything else. A twenty-question sitting is twenty lines, not
    #       four hundred.
    # full: every question keeps its whole block in scrollback.
    # Only the full-screen shell can take lines back; a pipe or the plain REPL
    # has already sent them and behaves as it always did.
    "drill_scrollback": "fold",
    # The date you are preparing for, as YYYY-MM-DD. Empty means no date is
    # set, and every screen that would count down says so rather than picking
    # a fortnight out of the air. Stored absolute even when you type it
    # relative: "+3 weeks" written down literally means a different day every
    # morning, which is the one thing a deadline may not do.
    "interview_date": "",
    # Where `export --md` writes, and whether it re-runs itself. Empty means
    # off. Set it once and every command that changes the bank's content
    # refreshes the Markdown, so the copy you read, share or hand to another
    # model is never the stale one.
    "export_md_dir": "",
    # What your API key is actually allowed, per minute and per day.
    #
    # These are 0 (unset) on purpose and are never guessed at. There is no
    # endpoint that reports quota, Google stopped publishing per-model
    # free-tier numbers in the docs, and the figures on the open web disagree
    # with each other by a factor of four. `usage` therefore reports the calls
    # it made and nothing else until you fill these in from the one place the
    # real numbers live: https://aistudio.google.com/rate-limit
    "rate_limit_rpm": 0,
    "rate_limit_rpd": 0,
    # Named here rather than only in `theme.py` so `settings` has a default to
    # report. Without a row here the page answered "None", which on the screen
    # you check a setting on reads as a fault rather than as "the default one".
    "theme": theme.DEFAULT,
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    cfg["inbox_dir"] = str(home() / "inbox")
    path = local_config()
    if path.exists():
        cfg.update(json.loads(path.read_text()))
    if os.environ.get("IB_CORPUS_DIR"):
        cfg["corpus_dir"] = os.environ["IB_CORPUS_DIR"]
    return cfg


def corpus_dir() -> Path:
    return Path(load()["corpus_dir"]).expanduser()


def inbox_dir() -> Path:
    return Path(load()["inbox_dir"]).expanduser()
