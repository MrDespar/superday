"""The provider front door, and the Gemini transport behind it.

Every call the tool makes goes through `generate()` (or `embed_batch()`), and
`settings llm_provider` decides who answers: Gemini, Claude or OpenAI. Callers
name a *job* -- enrich, grade, audit, embed -- and never a vendor, so switching
provider is one setting rather than an edit to six modules.

Three things are shared by all three and live here rather than in any one of
them: `LLMError` and `classify`, which turn an HTTP failure into a sentence you
can act on; `_attempt`, the retry loop that honours the delay a provider asked
for; and the usage log every call appends a row to.

What differs is only how each vendor is asked for structure, and each transport
owns that one problem:

  - Gemini  `responseSchema`, a JSON Schema subset, sent as-is.
  - Claude  a single forced tool -- `tool_choice` pins the tool and
            `input_schema` pins its arguments.
  - OpenAI  `response_format: json_schema` with `strict: true`, which needs
            every object closed and every property required, so the schema is
            rewritten on the way out and the nulls that buys are dropped on
            the way back.

Kept deliberately small and dependency-free: raw `urllib`, no SDKs. Every call
that needs parseable output is schema-constrained, because a grader that
sometimes returns prose is a grader you cannot trust.
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

import certifi

from . import usage
from .config import home
from .topics import TOPICS

BASE = "https://generativelanguage.googleapis.com/v1beta/models"
ANTHROPIC_BASE = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_BASE = "https://api.openai.com/v1/chat/completions"
OPENAI_EMBED_BASE = "https://api.openai.com/v1/embeddings"
_SSL = ssl.create_default_context(cafile=certifi.where())

# ---------------------------------------------------------------- providers
#
# One row per vendor. `key` is the environment variable its API key lives in,
# `label` is what error messages and the usage log call it, and `models` are
# the defaults per job -- overridden per job by IB_MODEL_*, which wins whatever
# the provider is, because a model name you typed is a model name you meant.
#
# An empty `embed` means the vendor sells no embedding endpoint. That is a real
# difference rather than an omission: Anthropic has none, so `find --semantic`
# says so and falls back to the lexical search instead of failing.
PROVIDERS = {
    "gemini": {
        "label": "Gemini",
        "key": "GEMINI_API_KEY",
        "setting": "gemini_api_key",
        "console": "https://aistudio.google.com/apikey",
        "limits": "https://aistudio.google.com/rate-limit",
        # Pro returns 429 on a free-tier key with no billing attached, so
        # flash is the default. Audit is a different model than extraction on
        # purpose -- a model grading its own output agrees with itself.
        "models": {"enrich": "gemini-3.5-flash", "grade": "gemini-3.5-flash",
                   "audit": "gemini-3.6-flash", "embed": "gemini-embedding-001"},
    },
    "claude": {
        "label": "Claude",
        "key": "ANTHROPIC_API_KEY",
        "setting": "anthropic_api_key",
        "console": "https://console.anthropic.com/settings/keys",
        "limits": "https://console.anthropic.com/settings/limits",
        # Opus for the audit for the same reason: the pass exists to disagree,
        # and a cheap model checking a cheap model is not a second opinion.
        "models": {"enrich": "claude-sonnet-5", "grade": "claude-sonnet-5",
                   "audit": "claude-opus-5", "embed": ""},
    },
    "openai": {
        "label": "OpenAI",
        "key": "OPENAI_API_KEY",
        "setting": "openai_api_key",
        "console": "https://platform.openai.com/api-keys",
        "limits": "https://platform.openai.com/settings/organization/limits",
        "models": {"enrich": "gpt-5", "grade": "gpt-5",
                   "audit": "gpt-5", "embed": "text-embedding-3-large"},
    },
}
DEFAULT_PROVIDER = "gemini"

# A row in `audits` is either the *first* opinion or a *second* one, and the
# provider column is what tells them apart: the first is filed under a bare
# provider name, the second under a namespaced one (`claude-code`,
# `claude-api`, or whatever `consult --provider` was given). That distinction
# used to be free because the first opinion was always Gemini; now that any
# vendor can produce it, the queries that mean "the audit we are checking"
# have to name the whole set rather than one member of it.
PRIMARY_PROVIDERS = tuple(PROVIDERS)
PRIMARY_SQL = "(" + ", ".join(f"'{p}'" for p in PRIMARY_PROVIDERS) + ")"

# The other half of that rule, and it lives here for the same reason: five
# queries in three modules join on "is this a second opinion", and they were
# each spelling it `IN ('claude-code', 'claude-api')`. That literal was true
# while Claude was the only vendor that could give one. It stops being true
# the moment the first opinion *is* Claude and the second has to come from
# somewhere else -- and a query that has not heard of the name a verdict was
# filed under reports a rejection as no rejection at all, which hands a
# question a second reader called wrong straight back to the drill.
SECOND_PROVIDERS = ("claude-code",) + tuple(f"{p}-api" for p in PRIMARY_PROVIDERS)
SECOND_SQL = "(" + ", ".join(f"'{p}'" for p in SECOND_PROVIDERS) + ")"


def provider() -> str:
    """Who answers. An unknown name falls back rather than failing every call.

    Same rule as `thinking_level`: a typo in a setting should cost you the
    setting, not the tool.
    """
    name = _env("IB_PROVIDER", DEFAULT_PROVIDER).lower()
    return name if name in PROVIDERS else DEFAULT_PROVIDER


def provider_label(name: str = "") -> str:
    return PROVIDERS[name or provider()]["label"]


def label_for(stored: str) -> str:
    """A stored `audits.provider` as the vendor a person would say.

    `claude-code` and `claude-api` are two ways the same model's opinion
    arrived; the queries need that distinction and a sentence on screen does
    not. An unrecognised name is returned as it is stored -- `consult
    --provider gpt-5-via-chat` names whatever you typed, and guessing at it
    would be worse than repeating it.
    """
    base = (stored or "").split("-")[0]
    return PROVIDERS[base]["label"] if base in PROVIDERS else (stored or "")


def limits_url(name: str = "") -> str:
    """Where this provider's real rate limits are, which is behind a login.

    Per-provider because `usage` used to say "WHAT GOOGLE ACTUALLY SAID" and
    point at AI Studio whoever was answering. A screen whose whole argument is
    "these are the calls we made, not what the provider says you have left"
    cannot then misname the provider.
    """
    return PROVIDERS[name or provider()]["limits"]


def default_model(job: str, name: str = "") -> str:
    return PROVIDERS[name or provider()]["models"].get(job, "")

# The table above holds the defaults; the accessors below are how anything
# reads them. Read through functions, never bound to module constants:
# constants are captured at import time, which is before load_env() has ever
# run, so nothing written to .env.local would be seen -- and .env.local is
# exactly where `settings` writes. Every accessor loads it first, which is what
# makes the knobs work at all.
#
# A model override is stored per provider -- IB_MODEL_ENRICH_GEMINI,
# IB_MODEL_ENRICH_CLAUDE -- because a model name only means anything next to
# the vendor that sells it. `settings model_enrich <name>` writes the one for
# whichever provider is current, so each vendor remembers its own and swapping
# back and forth changes nothing. See `model_for`.

# Free tier is rate limited per minute. Batching plus a floor between calls
# keeps a 2,100 page ingest from dying halfway through.
DEFAULT_MIN_CALL_INTERVAL = 0.5
DEFAULT_BACKOFF_BASE = 2.0
# A wait the provider named is honoured up to here. Longer than this is
# indistinguishable from a hang, and the caller can always re-run.
MAX_RETRY_WAIT = 60.0

# generationConfig.thinkingConfig.thinkingLevel. Extraction, enrich and audit
# fill in a fixed schema rather than reason their way to one, so they ask for
# "low" and stop paying for thinking tokens they were not using. Grading is
# left at the provider default: that one is the judgement call.
# IB_THINKING_LEVEL overrides every caller.
#
# This is the *tool's* vocabulary, not any one vendor's. Gemini's thinkingLevel
# enum stops at `high` and OpenAI's reasoning_effort does too, while Claude's
# `output_config.effort` goes two rungs further, so each transport clamps this
# to what its own API will accept rather than the setting being the intersection
# of all three. Asking for `max` and getting Gemini's `high` is the right
# outcome; refusing to accept `max` at all because Gemini has never heard of it
# is not.
THINKING_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")
THINKING_BULK = "low"
# What each vendor will actually take. A level above a vendor's ceiling is
# clamped down to it; `minimal` below Claude's floor is raised to `low`.
_GEMINI_LEVELS = ("minimal", "low", "medium", "high")
_OPENAI_LEVELS = ("minimal", "low", "medium", "high")
_CLAUDE_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _clamp_level(level: str, allowed: tuple[str, ...]) -> str:
    """The nearest level this vendor understands, or "" for no level at all."""
    if not level:
        return ""
    if level in allowed:
        return level
    order = [lv for lv in THINKING_LEVELS if lv in allowed]
    want = THINKING_LEVELS.index(level)
    below = [lv for lv in order if THINKING_LEVELS.index(lv) < want]
    return below[-1] if below else order[0]


_last_call = 0.0


def _env(name: str, default: str) -> str:
    load_env()
    val = os.environ.get(name)
    return val.strip() if val and val.strip() else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def model_enrich(name: str = "") -> str:
    return model_for("enrich", name)


def model_grade(name: str = "") -> str:
    return model_for("grade", name)


def model_audit(name: str = "") -> str:
    return model_for("audit", name)


def model_embed(name: str = "") -> str:
    return model_for("embed", name)


# What a model name says about who sells it. Matched by prefix, the same way
# `_reasoning_model` and `_claude_takes_temperature` are matched, and for the
# same reason: there is no endpoint to ask, and the names are stable where it
# matters. "" means it cannot tell -- a fine-tune, an alias, a preview name
# nobody here has seen -- and an unrecognised name is always honoured rather
# than second-guessed.
_MODEL_SHAPES = {
    "gemini": ("gemini", "models/gemini", "gemma", "learnlm"),
    "claude": ("claude",),
    "openai": ("gpt-", "gpt3", "gpt4", "chatgpt", "o1", "o3", "o4",
               "text-embedding-", "davinci", "babbage"),
}


def vendor_of(model: str) -> str:
    """Which provider a model name belongs to, or "" when it is not obvious."""
    m = (model or "").strip().lower()
    for name, shapes in _MODEL_SHAPES.items():
        if m.startswith(shapes):
            return name
    return ""


def model_key(job: str, name: str = "") -> str:
    """The environment variable holding this provider's override for one job.

    Per provider, because a model name is only meaningful next to the vendor
    that sells it. One shared `IB_MODEL_ENRICH` meant switching provider
    carried the old vendor's model across with it, so every call after the
    switch 404'd on a model that endpoint has never heard of -- and the fix
    was to work out which of four settings was the stale one. Set per
    provider, each vendor remembers its own and swapping back and forth
    changes nothing.
    """
    return f"IB_MODEL_{job.upper()}_{(name or provider()).upper()}"


def model_for(job: str, name: str = "") -> str:
    """The model one job runs on, for one provider. Overrides, then the default.

    Three places it can come from, in order: this provider's own override, the
    older shared override kept for the .env.local files that already carry one,
    and the table at the top of this file. The shared one is honoured only when
    it does not name a *different* vendor's model -- `IB_MODEL_ENRICH` left
    over from Gemini is not an instruction to ask Anthropic for
    `gemini-3.5-flash`, it is a setting that stopped applying when the provider
    changed.
    """
    name = name or provider()
    own = _env(model_key(job, name), "")
    if own:
        return own
    shared = _env(f"IB_MODEL_{job.upper()}", "")
    if shared and vendor_of(shared) in ("", name):
        return shared
    return default_model(job, name)


def stale_override(job: str, name: str = "") -> str:
    """The shared override this provider is ignoring, if there is one.

    Ignoring it silently is right for the call and wrong for the screen: the
    one thing worse than a setting that stopped applying is a setting that
    stopped applying and still reads as set. `settings` and `llm` both say so.
    """
    name = name or provider()
    if _env(model_key(job, name), ""):
        return ""
    shared = _env(f"IB_MODEL_{job.upper()}", "")
    v = vendor_of(shared)
    return shared if shared and v and v != name else ""


# `cross-audit --api` used to have its own accessor here, reading
# IB_MODEL_AUDIT_CLAUDE so that pass could not follow `llm_provider` onto the
# model that wrote the answer. Per-provider overrides made that accessor the
# general case: `model_audit("claude")` reads the very same variable, and it
# reads IB_MODEL_AUDIT_OPENAI when the second opinion has to come from
# somewhere else because Claude is the one being checked. One rule, spelled
# once -- see `crossaudit.second_provider`.


def setup_help(name: str) -> str:
    """What to do about a provider you have no key for, as a paragraph."""
    spec = PROVIDERS[name]
    return (f"No {spec['key']}, which is what the {spec['label']} provider needs.\n"
            f"  Get one at {spec['console']} and set it with:\n"
            f"    settings {spec['setting']} <key>")


def embeds(name: str = "") -> bool:
    """Whether a provider sells an embedding endpoint at all."""
    return bool(model_embed(name))


def min_call_interval() -> float:
    return _env_float("IB_MIN_CALL_INTERVAL", DEFAULT_MIN_CALL_INTERVAL)


def backoff_base() -> float:
    return _env_float("IB_BACKOFF_BASE", DEFAULT_BACKOFF_BASE)


def thinking_level(default: str = "") -> str:
    """The thinking level a call should ask for, or "" for the provider default.

    An unrecognised value is dropped rather than sent: the v1beta endpoint
    validates this enum before it checks quota, so a typo would turn every
    call into a 400 instead of a slightly pricier answer.
    """
    level = (_env("IB_THINKING_LEVEL", default)).lower()
    return level if level in THINKING_LEVELS else ""


def throttle() -> None:
    global _last_call
    wait = min_call_interval() - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


class LLMError(RuntimeError):
    """A provider call that failed, phrased for the person who has to act.

    `str(e)` is one sentence you can do something about. The provider's raw
    body is kept on `.detail` for when you actually want it (IB_DEBUG=1), and
    is deliberately not in the message: a 400-character JSON blob printed
    mid-drill buries the one fact that matters, which is usually "top up your
    credits" or "the key is wrong".
    """

    def __init__(self, message: str, *, detail: str = "", hint: str = "",
                 retryable: bool = False, status: int | None = None,
                 retry_after: float | None = None, logged: bool = False):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.hint = hint
        self.retryable = retryable
        self.status = status
        # How long the provider asked us to wait, when it said. Guessing is
        # worse than asking: see _attempt.
        self.retry_after = retry_after
        # Set once this failure has been written to the usage log. A failure
        # the response body explains -- MAX_TOKENS, a safety stop -- is logged
        # where the token counts are, which is inside the call; everything
        # else is logged by the retry loop. Without the flag the first kind
        # would be counted twice, and the day's call count would drift up by
        # exactly the calls that went wrong.
        self.logged = logged

    def __str__(self) -> str:
        if self.detail and os.environ.get("IB_DEBUG"):
            return f"{self.message}  [{self.detail}]"
        return self.message


def _body_message(detail: str) -> str:
    """Dig the human sentence out of a provider's JSON error body."""
    try:
        payload = json.loads(detail)
    except (ValueError, TypeError):
        return detail.strip()[:200]
    for path in (("error", "message"), ("error", "status"), ("message",)):
        node = payload
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, str) and node.strip():
            return node.strip()
    return detail.strip()[:200]


def _retry_delay(detail: str) -> float | None:
    """The wait the provider actually asked for, dug out of the error body.

    Google puts it in error.details[].retryDelay precisely so a client does
    not have to guess. Guessing is what made the old backoff useless: a
    per-minute quota resets on a 60-second window, so 2s then 4s put all three
    attempts inside the same exhausted window, and each rejected request still
    counted against the quota it was waiting on.
    """
    try:
        payload = json.loads(detail)
    except (ValueError, TypeError):
        return None
    for d in ((payload.get("error") or {}).get("details") or []):
        raw = d.get("retryDelay") if isinstance(d, dict) else None
        if isinstance(raw, str) and raw.endswith("s"):
            try:
                return float(raw[:-1])
            except ValueError:
                return None
    return None


def _retry_after_header(headers) -> float | None:
    """Retry-After, when it is a plain number of seconds."""
    raw = headers.get("Retry-After") if headers else None
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def classify(provider: str, status: int, detail: str, model: str = "",
             retry_after: float | None = None) -> LLMError:
    """One HTTP failure, turned into a message and a decision about retrying.

    The retry flag matters as much as the wording. Depleted credits are not a
    transient condition, and the old code spent three attempts and about two
    minutes of backoff discovering that -- with the screen frozen -- before
    printing the raw JSON.
    """
    body = _body_message(detail)
    low = body.lower()
    # `provider` arrives here as the transport's own label -- "Gemini",
    # "Claude", "OpenAI" -- so this has to fold case before it can name the
    # right key. It used to read `provider == "claude"`, which is never true of
    # "Claude", so a rejected Anthropic key told you to check GEMINI_API_KEY
    # and an OpenAI one had no branch at all: the 401 message pointed every
    # user of the two newer providers at a variable they had never set.
    spec = PROVIDERS.get(provider.strip().lower())
    key_env = spec["key"] if spec else PROVIDERS[DEFAULT_PROVIDER]["key"]

    if status == 429:
        if any(w in low for w in ("credit", "billing", "prepay", "balance",
                                  "insufficient_quota", "plan and billing")):
            return LLMError(
                f"{provider} has no credit left on this key",
                detail=detail, status=status, retryable=False,
                hint="top the account up, or drill with --local to skip grading")
        asked = retry_after or _retry_delay(detail)
        if asked:
            # Clamped here rather than at the sleep, so the hint states the
            # wait that will actually happen. A message promising an hour and
            # then retrying after a minute is worse than no message.
            wait = min(MAX_RETRY_WAIT, asked)
            note = (f"it will retry in {wait:.0f}s"
                    if wait >= asked else
                    f"{provider} asked for {asked:.0f}s; it will retry in "
                    f"{wait:.0f}s and may be rejected again")
            return LLMError(
                f"{provider} rate limit reached",
                detail=detail, status=status, retryable=True, retry_after=wait,
                hint=note)
        return LLMError(
            f"{provider} rate limit reached",
            detail=detail, status=status, retryable=True,
            hint="it will retry; raise min_call_interval if it keeps happening")
    if status in (401, 403):
        return LLMError(
            f"{provider} rejected the API key",
            detail=detail, status=status, retryable=False,
            hint=f"check {key_env} in .env.local, or run: settings {key_env.lower()}")
    if status == 404:
        return LLMError(
            f"{provider} has no model called {model!r}" if model
            else f"{provider} endpoint not found",
            detail=detail, status=status, retryable=False,
            hint="run `settings` and check the model this command uses: "
                 "model_enrich, model_grade, model_audit or model_embed")
    if status == 400:
        return LLMError(f"{provider} rejected the request: {body[:120]}",
                        detail=detail, status=status, retryable=False)
    if status in (500, 502, 503, 504):
        return LLMError(f"{provider} is unavailable right now (HTTP {status})",
                        detail=detail, status=status, retryable=True)
    return LLMError(f"{provider} returned HTTP {status}: {body[:120]}",
                    detail=detail, status=status, retryable=True)


def classify_transport(provider: str, exc: Exception) -> LLMError:
    """Network-level failure: no HTTP status to go on, just what broke."""
    import socket
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return LLMError(f"{provider} did not answer in time",
                        detail=repr(exc), retryable=True)
    if isinstance(exc, urllib.error.URLError):
        return LLMError(f"could not reach {provider} - check your connection",
                        detail=str(getattr(exc, "reason", exc)), retryable=True)
    if isinstance(exc, ValueError):        # json.JSONDecodeError is a subclass
        return LLMError(f"{provider} sent something that was not JSON",
                        detail=str(exc)[:200], retryable=True)
    return LLMError(f"{provider} call failed: {type(exc).__name__}",
                    detail=str(exc)[:200], retryable=True)


def give_up_note(e: LLMError) -> str:
    """What a batch loop should say when it stops.

    The old wording guessed -- "repeated failures, likely quota" -- because
    nothing upstream knew why. classify() knows, so the guess is replaced by
    whatever it worked out, and by the thing you can do about it.
    """
    if e.hint:
        return f"stopping: {e.message}. {e.hint}"
    if not e.retryable:
        return f"stopping: {e.message}"
    return f"stopping after repeated failures: {e.message}. Re-run to resume."


# The shell shows a spinner while a call is in flight by installing a runner
# here. Without one this is a plain function call, which is what keeps the
# one-shot path and the tests free of threads.
RUNNER = None


def run_call(label: str, fn):
    return RUNNER(label, fn) if RUNNER else fn()


_env_loaded = False


def load_env(path: Path | None = None, *, force: bool = False) -> None:
    """Minimal .env.local reader so the key never has to live in the shell.

    Memoised, because every accessor above calls it and an embedding backfill
    calls the accessors once per question: re-reading the same file 800 times
    to learn the same thing is pure syscall. `settings` writes the new value
    into os.environ as well as the file, so the cache cannot go stale behind
    it; pass force=True for anything that edits the file another way.
    """
    global _env_loaded
    if path is None:
        if _env_loaded and not force:
            return
        _env_loaded = True
    f = path or (home() / ".env.local")
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def key_env(name: str = "") -> str:
    """Which environment variable holds the key for a provider."""
    return PROVIDERS[name or provider()]["key"]


def api_key(name: str = "") -> str:
    load_env()
    name = name or provider()
    spec = PROVIDERS[name]
    key = os.environ.get(spec["key"])
    if not key:
        raise LLMError(
            f"No {spec['key']}, which is what the {spec['label']} provider needs.",
            hint=f"settings {spec['setting']} <key>   ·   {spec['console']}")
    return key


def _auth_headers(name: str = "") -> dict[str, str]:
    """The key travels in a header, never in the query string.

    Gemini accepts `?key=`, and that is how this used to send it. A URL is the
    part of a request that gets written down -- by proxies, by corporate TLS
    inspection, by anything that logs a request line -- and `HTTPError` carries
    the URL it failed on, so a key in the query string is a key one stray
    traceback away from a bug report. A header is logged by none of those.

    All three vendors take it in a header, and each spells that header its own
    way, so this is the one place any of them is named.
    """
    name = name or provider()
    key = api_key(name)
    base = {"Content-Type": "application/json"}
    if name == "gemini":
        return base | {"x-goog-api-key": key}
    if name == "claude":
        return base | {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    return base | {"Authorization": f"Bearer {key}"}


def available(name: str = "") -> bool:
    load_env()
    return bool(os.environ.get(key_env(name)))


def configured() -> list[str]:
    """Every provider you actually hold a key for, in table order."""
    return [name for name in PROVIDERS if available(name)]


JOBS = ("enrich", "grade", "audit", "embed")


def overview() -> list[dict]:
    """Every provider, side by side: key, models, and what today cost.

    One row per vendor whether or not you hold its key, because the question
    this answers is "which of these could answer right now, and what would it
    cost me to switch" -- and a provider left off the list because it is
    unconfigured is exactly the one you are trying to find out how to
    configure.

    The call counts come from the local log rather than from anywhere
    authoritative, and mean what they mean everywhere else in this tool: calls
    made, not quota left.
    """
    load_env()
    today = usage.since_midnight(usage.entries())
    counts = dict(usage.tally(today, "provider"))
    active = provider()
    out = []
    for name, spec in PROVIDERS.items():
        key = os.environ.get(spec["key"]) or ""
        g = counts.get(name, {})
        failures = [r for r in today
                    if r.get("provider") == name and r.get("outcome") != "ok"]
        out.append({
            "name": name,
            "label": spec["label"],
            "active": name == active,
            "key_set": bool(key),
            "key_tail": key[-4:] if len(key) > 4 else "",
            "key_env": spec["key"],
            "setting": spec["setting"],
            "console": spec["console"],
            "limits": spec["limits"],
            "models": {job: model_for(job, name) for job in JOBS},
            "stale": {job: stale for job in JOBS
                      if (stale := stale_override(job, name))},
            "embeds": embeds(name),
            "calls_today": g.get("calls", 0),
            "failed_today": g.get("failed", 0),
            "refused_today": g.get("refused", 0),
            "tokens_today": g.get("total_tokens", 0),
            "last_failure": (failures[-1].get("reason") or "") if failures else "",
        })
    return out


# ---------------------------------------------------------------- probing

# The smallest real call there is: a one-field schema, a one-word answer. It
# has to be a *real* call, because everything you would want to know about a
# key -- is it valid, is it out of credit, does this account have this model,
# does the network reach the host at all -- is only knowable by asking. There
# is no endpoint that validates a key without spending one.
PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}
PROBE_PROMPT = "Reply with ok set to true. Nothing else."


class Probe(NamedTuple):
    """One endpoint, asked one question, and what came back.

    `job` is which of a provider's models was exercised, because "the key
    works" and "the model this command uses exists on this account" are
    different facts and only the second one explains a 404 mid-ingest.
    """
    provider: str
    job: str
    model: str
    ok: bool
    seconds: float
    message: str
    hint: str = ""


def probe(name: str = "", *, embed: bool = True, timeout: int = 30) -> list[Probe]:
    """Spend the smallest possible call on finding out whether a key works.

    One line per endpoint: the chat model every command routes through, and
    the embedding model `find --semantic` needs, which is a separate account
    permission on OpenAI and does not exist at all on Anthropic. A provider
    with no key is reported rather than skipped -- "not set" is the answer to
    the question that was asked, and it is knowable without spending a call to
    be told it.

    A failure is a row rather than an exception: this is the command you run
    *because* something is wrong, so every way a call can fail has to come back
    as a line you can read next to the ones that worked.
    """
    name = name if name in PROVIDERS else provider()
    spec = PROVIDERS[name]
    if not available(name):
        return [Probe(name, "grade", model_grade(name), False, 0.0,
                      f"no {spec['key']} set",
                      f"settings {spec['setting']} <key>   ·   {spec['console']}")]

    out: list[Probe] = []
    model = model_grade(name)
    started = time.time()
    try:
        generate(PROBE_PROMPT, schema=PROBE_SCHEMA, model=model, using=name,
                 retries=1, timeout=timeout, thinking=THINKING_BULK,
                 caller="probe", label=f"asking {spec['label']}")
        out.append(Probe(name, "grade", model, True, time.time() - started,
                         "answered"))
    except LLMError as e:
        out.append(Probe(name, "grade", model, False, time.time() - started,
                         e.message, e.hint))
    if not embed:
        return out
    emb = model_embed(name)
    if not emb:
        out.append(Probe(name, "embed", "", False, 0.0,
                         f"{spec['label']} sells no embeddings endpoint",
                         "`find` searches by keyword instead - that is the "
                         "whole difference"))
        return out
    started = time.time()
    try:
        embed_batch(["superday"], model=emb, retries=1, timeout=timeout,
                    using=name)
        out.append(Probe(name, "embed", emb, True, time.time() - started,
                         "answered"))
    except LLMError as e:
        out.append(Probe(name, "embed", emb, False, time.time() - started,
                         e.message, e.hint))
    return out


def _read_response(payload: dict, schema: dict | None):
    """One Gemini response body, judged. Raises `LLMError` for anything unusable.

    Split out of `generate` so the usage log has exactly one success path and
    exactly one failure path to hang off, rather than a `record` call at each
    of six exits -- the one that gets forgotten is the one that then reports a
    failed call as a call that never happened.
    """
    cands = payload.get("candidates") or []
    if not cands:
        blocked = (payload.get("promptFeedback") or {}).get("blockReason")
        raise LLMError("Gemini returned nothing"
                       + (f" - blocked as {blocked}" if blocked else ""),
                       detail=str(payload)[:300], retryable=not blocked)
    # Why the model stopped decides whether trying again can possibly help. A
    # response cut off at the output cap comes back as truncated JSON,
    # json.loads raises a ValueError, and classify_transport used to read that
    # as "not JSON, probably transient" -- so the identical oversized prompt
    # was sent three times and the error named neither the cause nor the fix.
    finish = (cands[0].get("finishReason") or "").upper()
    if finish == "MAX_TOKENS":
        raise LLMError(
            "Gemini hit its output limit before finishing the answer",
            detail=str(payload)[:300], retryable=False,
            hint="ask for fewer items per call: --batch on enrich / audit, "
                 "--window on an ingest")
    if finish in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT"):
        raise LLMError(f"Gemini stopped this response as {finish.lower()}",
                       detail=str(payload)[:300], retryable=False)
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise LLMError("Gemini returned an empty completion", retryable=True)
    if schema:
        try:
            return json.loads(text)
        except ValueError as e:
            # Still worth one more try -- the deterministic cause of
            # unparseable output is truncation, and that is caught above by
            # finishReason before it ever reaches here.
            raise LLMError("Gemini sent something that was not JSON",
                           detail=f"{e}: {text[:200]}", retryable=True,
                           hint="try a smaller --batch") from None
    return text


def generate(
    prompt: str,
    *,
    schema: dict | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    retries: int = 3,
    timeout: int = 180,
    label: str = "",
    thinking: str = "",
    caller: str = "",
    using: str = "",
) -> dict | str:
    """Ask whoever is configured. Callers name a job, never a vendor.

    `using` pins one call to one provider regardless of the setting, which is
    what `cross-audit --api` wants: the whole point of that pass is a second
    opinion from somebody else, so it must not quietly become the same model
    that wrote the answer when the setting happens to agree.
    """
    name = using if using in PROVIDERS else provider()
    # The model defaults to *this* provider's, never the configured one's. A
    # pinned call that fell back to `model_grade()` asked Google for
    # `claude-sonnet-5` the moment the setting said claude.
    model = model or model_grade(name)
    fn = {"claude": _claude_generate, "openai": _openai_generate}.get(
        name, _gemini_generate)
    return fn(prompt, schema=schema, model=model, temperature=temperature,
              retries=retries, timeout=timeout, label=label, thinking=thinking,
              caller=caller)


def _gemini_generate(
    prompt: str,
    *,
    schema: dict | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    retries: int = 3,
    timeout: int = 180,
    label: str = "",
    thinking: str = "",
    caller: str = "",
) -> dict | str:
    model = model or model_grade("gemini")
    body: dict = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }
    level = _clamp_level(thinking_level(thinking), _GEMINI_LEVELS)
    if level:
        body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": level}
    if schema:
        body["generationConfig"]["responseMimeType"] = "application/json"
        body["generationConfig"]["responseSchema"] = schema

    url = f"{BASE}/{model}:generateContent"
    data = json.dumps(body).encode()

    who = caller or label or "generate"

    def once():
        throttle()
        req = urllib.request.Request(url, data=data,
                                     headers=_auth_headers("gemini"))
        started = time.time()
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            payload = json.loads(r.read())
        # `usageMetadata` is the only per-call usage signal the API gives, and
        # it is on the response whether or not the response turns out to be
        # usable. A call that stopped at MAX_TOKENS or came back blocked still
        # spent its place in the rate limit, so it is logged with the tokens it
        # burned rather than dropped -- those are exactly the calls worth
        # knowing about when the day's count looks higher than the work done.
        meta = usage.tokens_from(payload)
        elapsed = time.time() - started

        # One place logs the outcome, whichever way the response goes. Written
        # as a wrapper rather than a `note` at each exit because there are six
        # exits and the one that gets forgotten is the one that then reports
        # a failed call as a call that never happened.
        try:
            result = _read_response(payload, schema)
        except LLMError as e:
            usage.record(provider="gemini", caller=who, model=model,
                         outcome="failed", reason=e.message,
                         seconds=elapsed, **meta)
            e.logged = True
            raise
        usage.record(provider="gemini", caller=who, model=model, outcome="ok",
                     seconds=elapsed, **meta)
        return result

    return _attempt(once, "Gemini", retries, model, label=label, caller=who)


# ---------------------------------------------------------------- Claude

TOOL_NAME = "record_result"

# Thinking is on by default on the current Claude models and its tokens come
# out of `max_tokens`, so a ceiling sized for the answer alone is a ceiling the
# reasoning eats before the answer starts. That arrives as
# `stop_reason: max_tokens`, which this transport correctly refuses to retry --
# so an undersized ceiling reads as "ask about fewer items" when the batch size
# was never the problem. 16k is the largest a non-streaming request comfortably
# returns inside the request timeout; past that the API wants streaming.
MAX_TOKENS_CLAUDE = 16000

# Which Claude models still accept a sampling parameter.
#
# `temperature`, `top_p` and `top_k` were *removed* on Opus 5, Opus 4.7/4.8,
# Sonnet 5 and Fable 5: the API returns 400 rather than ignoring them, so one
# unconditional `temperature` key turned every call this transport made into a
# failed call.
#
# Matched by name because there is no capability endpoint to ask, and the
# default is the safe direction -- which here is *omit*. Sending one to a model
# that refuses it fails the call outright; omitting one from a model that
# accepts it costs a little determinism and nothing else. So this is an
# allow-list of the families that still take it rather than a deny-list of the
# ones that do not, and an unrecognised model is assumed to be newer.
_CLAUDE_SAMPLING_OK = ("claude-3", "claude-haiku-4", "claude-sonnet-4-5",
                       "claude-opus-4-5", "claude-opus-4-6", "claude-sonnet-4-6")


def _claude_takes_temperature(model: str) -> bool:
    return (model or "").lower().startswith(_CLAUDE_SAMPLING_OK)


def _claude_generate(prompt, *, schema, model, temperature, retries, timeout,
                     label, thinking, caller):
    """Anthropic. Structure comes from one forced tool rather than a schema field.

    `tool_choice` pins the model to a single tool and `input_schema` pins that
    tool's arguments, which is the same guarantee Gemini's responseSchema gives.
    With no schema this is an ordinary text completion.
    """
    model = model or model_grade("claude")
    who = caller or label or "generate"
    body: dict = {
        "model": model,
        "max_tokens": MAX_TOKENS_CLAUDE,
        "messages": [{"role": "user", "content": prompt}],
    }
    # Sampling parameters are *removed* on every current Claude model -- Opus 5,
    # Sonnet 5, Opus 4.7 and later all return 400 for `temperature` rather than
    # ignoring it, so sending one broke every call this transport made. Effort
    # is the depth knob here, and it is set below.
    #
    # The OpenAI transport next door had already met this in the o-series and
    # guards it with `_reasoning_model`; this one is the same rule with no
    # exceptions left, so the key is simply never sent.
    if _claude_takes_temperature(model):
        body["temperature"] = temperature
    if schema:
        body["tools"] = [{"name": TOOL_NAME,
                          "description": "Return the result in this exact shape.",
                          "input_schema": schema}]
        body["tool_choice"] = {"type": "tool", "name": TOOL_NAME}
    # Thinking is the cost lever here too, spelled `effort` rather than a level.
    level = _clamp_level(thinking_level(thinking), _CLAUDE_LEVELS)
    if level:
        body["output_config"] = {"effort": level}
    data = json.dumps(body).encode()

    def once():
        throttle()
        req = urllib.request.Request(ANTHROPIC_BASE, data=data,
                                     headers=_auth_headers("claude"))
        started = time.time()
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            payload = json.loads(r.read())
        counts = payload.get("usage") or {}
        meta = {"prompt_tokens": counts.get("input_tokens"),
                "output_tokens": counts.get("output_tokens")}
        if all(isinstance(v, int) for v in meta.values()):
            meta["total_tokens"] = meta["prompt_tokens"] + meta["output_tokens"]
        elapsed = time.time() - started

        def note(outcome: str, reason: str = "") -> None:
            usage.record(provider="claude", caller=who, model=model,
                         outcome=outcome, reason=reason, seconds=elapsed, **meta)

        if payload.get("stop_reason") == "refusal":
            note("failed", "refused on safety grounds")
            raise LLMError("Claude refused this batch on safety grounds",
                           retryable=False, logged=True)
        # A tool call cut off at max_tokens still arrives as a tool_use block,
        # just with fewer items than were asked about. Accepting it makes a
        # partial pass report itself as complete.
        if payload.get("stop_reason") == "max_tokens":
            note("failed", "hit the output limit")
            raise LLMError("Claude hit its output limit before finishing",
                           detail=str(payload)[:300], retryable=False,
                           logged=True, hint="ask about fewer items: --batch")
        if schema:
            for block in payload.get("content", []):
                if block.get("type") == "tool_use":
                    note("ok")
                    return block.get("input") or {}
            note("failed", "answered without calling the tool")
            raise LLMError("Claude answered without calling the tool",
                           detail=str(payload)[:300], retryable=True, logged=True)
        text = "".join(b.get("text", "") for b in payload.get("content", [])).strip()
        if not text:
            note("failed", "empty completion")
            raise LLMError("Claude returned an empty completion", retryable=True,
                           logged=True)
        note("ok")
        return text

    return _attempt(once, "Claude", retries, model, label=label, caller=who)


# ---------------------------------------------------------------- OpenAI


def strict_schema(schema: dict) -> dict:
    """A JSON Schema rewritten for OpenAI's `strict: true`.

    Strict mode refuses a schema unless every object closes itself
    (`additionalProperties: false`) and lists *every* property in `required`.
    Taken literally that would make optional fields mandatory, so the ones the
    caller did not require are widened to accept null instead -- the model can
    still decline to answer them, it just has to say so out loud. `_drop_nulls`
    then takes those back out, so a caller sees the same "key absent" it sees
    from Gemini and never learns which provider it was talking to.
    """
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        props = {k: strict_schema(v) for k, v in out["properties"].items()}
        optional = [k for k in props if k not in (out.get("required") or [])]
        for k in optional:
            t = props[k].get("type")
            if isinstance(t, str) and t != "null":
                props[k] = dict(props[k], type=[t, "null"])
        out["properties"] = props
        out["required"] = list(props)
        out["additionalProperties"] = False
    elif out.get("type") == "array" and isinstance(out.get("items"), dict):
        out["items"] = strict_schema(out["items"])
    return out


def _drop_nulls(value):
    """Undo what strict mode forced: a null is an absent key again."""
    if isinstance(value, dict):
        return {k: _drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nulls(v) for v in value]
    return value


def _openai_generate(prompt, *, schema, model, temperature, retries, timeout,
                     label, thinking, caller):
    model = model or model_grade("openai")
    who = caller or label or "generate"
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    # Reasoning models reject an explicit temperature rather than ignoring it,
    # and the whole point of this layer is that a caller does not have to know
    # which family it is talking to, so it is simply never sent.
    if not _reasoning_model(model):
        body["temperature"] = temperature
    level = _clamp_level(thinking_level(thinking), _OPENAI_LEVELS)
    if level and _reasoning_model(model):
        body["reasoning_effort"] = level
    if schema:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "result", "strict": True,
                            "schema": strict_schema(schema)},
        }
    data = json.dumps(body).encode()

    def once():
        throttle()
        req = urllib.request.Request(OPENAI_BASE, data=data,
                                     headers=_auth_headers("openai"))
        started = time.time()
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            payload = json.loads(r.read())
        counts = payload.get("usage") or {}
        meta = {"prompt_tokens": counts.get("prompt_tokens"),
                "output_tokens": counts.get("completion_tokens"),
                "total_tokens": counts.get("total_tokens")}
        elapsed = time.time() - started

        def note(outcome: str, reason: str = "") -> None:
            usage.record(provider="openai", caller=who, model=model,
                         outcome=outcome, reason=reason, seconds=elapsed, **meta)

        choice = (payload.get("choices") or [{}])[0]
        # `length` is this vendor's MAX_TOKENS: it arrives looking like success
        # with truncated JSON inside, and retrying an oversized prompt buys the
        # same truncation three times.
        if choice.get("finish_reason") == "length":
            note("failed", "hit the output limit")
            raise LLMError("OpenAI hit its output limit before finishing",
                           detail=str(payload)[:300], retryable=False,
                           logged=True, hint="ask about fewer items: --batch")
        if choice.get("finish_reason") == "content_filter":
            note("failed", "content filter")
            raise LLMError("OpenAI stopped this one on its content filter",
                           retryable=False, logged=True)
        text = ((choice.get("message") or {}).get("content") or "").strip()
        if not text:
            note("failed", "empty completion")
            raise LLMError("OpenAI returned an empty completion", retryable=True,
                           logged=True)
        if not schema:
            note("ok")
            return text
        try:
            parsed = json.loads(text)
        except ValueError as e:
            note("failed", "not JSON")
            raise LLMError("OpenAI sent something that was not JSON",
                           detail=f"{e}: {text[:200]}", retryable=True,
                           logged=True, hint="try a smaller --batch") from None
        note("ok")
        return _drop_nulls(parsed)

    return _attempt(once, "OpenAI", retries, model, label=label, caller=who)


def _reasoning_model(model: str) -> bool:
    """o-series and GPT-5 reason by default and take `reasoning_effort`.

    Matched by name because there is no capability endpoint to ask, and got
    wrong in the safe direction: an unrecognised model keeps `temperature`,
    which every chat model accepts.
    """
    m = model.lower()
    return m.startswith(("o1", "o3", "o4", "gpt-5"))


def _attempt(once, provider: str, retries: int, model: str = "",
             label: str = "", caller: str = ""):
    """Run a provider call with retries, giving up early when retrying is futile.

    A depleted balance or a wrong key does not become true on the third try;
    the old loop spent about two minutes of backoff proving that. Only errors
    marked retryable are worth waiting for.
    """
    last: LLMError | None = None
    for attempt in range(max(1, retries)):
        try:
            return run_call(label or f"asking {provider}", once)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf8", "ignore")[:600]
            last = classify(provider, e.code, detail, model,
                            retry_after=_retry_after_header(e.headers))
        except LLMError as e:
            last = e
        except Exception as e:
            last = classify_transport(provider, e)
        # A refusal never reaches a response body, so it is logged here rather
        # than in `once`. This is the row that matters most in the whole log:
        # a 429 with the provider's own `retryDelay` on it is the only
        # authoritative statement about your quota that exists anywhere.
        if not last.logged:
            usage.record(provider=provider.lower(),
                         caller=caller or label or "unknown", model=model,
                         outcome="refused" if last.status == 429 else "failed",
                         status=last.status, reason=last.message,
                         retry_after=last.retry_after)
        if not last.retryable:
            raise last from None
        if attempt == retries - 1:
            break
        # A wait the provider named is worth honouring up to a minute: it knows
        # when its own window resets, and sleeping less than that just spends
        # another request on the same exhausted quota. A wait we guessed is
        # capped harder, because a guessed long pause is indistinguishable
        # from a hang.
        if last.retry_after:
            time.sleep(last.retry_after)     # already clamped by classify()
        else:
            time.sleep(min(20.0, backoff_base() * (2 ** attempt)))
    if last is None:
        last = LLMError(f"{provider} call failed")
    if retries > 1:
        last.message = f"{last.message} (gave up after {retries} tries)"
    raise last


# batchEmbedContents takes this many texts in one request. One question per
# HTTPS round trip meant 842 calls, 842 throttle waits and 842 chances for the
# failure budget to abandon the run, to fetch what nine calls fetch.
MAX_EMBED_BATCH = 100


def _openai_embed(texts: list[str], *, model: str | None, retries: int,
                  timeout: int) -> list[list[float]]:
    """One request for the whole batch, same as the Gemini path.

    The stored vectors carry the model that produced them, and vectors from two
    models are not comparable, so switching provider re-embeds rather than
    scoring one model's vectors against another's -- `index_embeddings` already
    enforces that and needs no help here.
    """
    model = model or model_embed("openai")
    body = {"model": model, "input": [t[:2000] for t in texts]}
    data = json.dumps(body).encode()

    def once():
        throttle()
        req = urllib.request.Request(OPENAI_EMBED_BASE, data=data,
                                     headers=_auth_headers("openai"))
        started = time.time()
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            payload = json.loads(r.read())
        counts = payload.get("usage") or {}
        usage.record(provider="openai", caller="embed", model=model, outcome="ok",
                     seconds=time.time() - started,
                     prompt_tokens=counts.get("prompt_tokens"),
                     total_tokens=counts.get("total_tokens"))
        rows = sorted(payload.get("data") or [], key=lambda d: d.get("index", 0))
        out = [r.get("embedding") or [] for r in rows]
        if len(out) != len(texts):
            raise LLMError("OpenAI returned a different number of vectors than "
                           f"texts sent ({len(out)} for {len(texts)})",
                           retryable=True)
        return out

    return _attempt(once, "OpenAI", retries, model, label="embedding",
                    caller="embed")


def embed_batch(
    texts: list[str],
    *,
    model: str | None = None,
    retries: int = 3,
    timeout: int = 120,
    using: str = "",
) -> list[list[float]]:
    """Embed up to MAX_EMBED_BATCH snippets in one call. Order is preserved.

    `using` pins the vendor the same way `generate` does, so `llm --test` can
    check one provider's embedding endpoint without first making it the
    configured one.
    """
    name = using if using in PROVIDERS else provider()
    if not texts:
        return []
    if len(texts) > MAX_EMBED_BATCH:
        raise LLMError(f"embed_batch takes at most {MAX_EMBED_BATCH} texts at once")
    if not model and not embeds(name):
        # Anthropic sells no embedding endpoint. Saying so is the whole job
        # here: the caller falls back to the lexical search, which is what
        # `find` does without a key at all, rather than failing the command.
        others = [p for p in PROVIDERS if default_model("embed", p)]
        raise LLMError(
            f"{provider_label(name)} has no embeddings endpoint",
            retryable=False,
            hint="`find` still works lexically; for --semantic set "
                 "`settings llm_provider " + " or ".join(others) + "`")
    if name == "openai":
        return _openai_embed(texts, model=model, retries=retries, timeout=timeout)
    model = model or model_embed(name)
    body = {"requests": [
        {"model": f"models/{model}", "content": {"parts": [{"text": t[:2000]}]}}
        for t in texts
    ]}
    url = f"{BASE}/{model}:batchEmbedContents"
    data = json.dumps(body).encode()

    def once():
        throttle()
        req = urllib.request.Request(url, data=data,
                                     headers=_auth_headers("gemini"))
        started = time.time()
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            payload = json.loads(r.read())

        def note(outcome: str, reason: str = "") -> None:
            # batchEmbedContents carries no usageMetadata, so the token fields
            # stay absent rather than being written as zero -- a zero here
            # would read as "that call was free", which is a different claim
            # from "the endpoint does not say".
            usage.record(provider="gemini", caller="embed", model=model,
                         outcome=outcome, reason=reason,
                         seconds=time.time() - started, **usage.tokens_from(payload))

        out = payload.get("embeddings") or []
        # A short batch is not a blip: the same request returns the same short
        # batch, so retrying it just pays twice for the same surprise.
        if len(out) != len(texts):
            note("failed", f"embedded {len(out)} of {len(texts)}")
            raise LLMError(
                f"Gemini embedded {len(out)} of {len(texts)} texts",
                detail=str(payload)[:300], retryable=False, logged=True)
        vecs = [[float(v) for v in (e.get("values") or [])] for e in out]
        if any(not v for v in vecs):
            note("failed", "empty embedding")
            raise LLMError("Gemini returned an empty embedding",
                           detail=str(payload)[:300], retryable=True, logged=True)
        note("ok")
        return vecs

    return _attempt(once, "Gemini", retries, model,
                    label=f"embedding {len(texts)}", caller="embed")


def embed(text: str, *, model: str | None = None, retries: int = 3,
          timeout: int = 60, using: str = "") -> list[float]:
    """One text, embedded by whichever provider sells the endpoint."""
    return embed_batch([text], model=model, retries=retries, timeout=timeout,
                       using=using)[0]


# ---------------------------------------------------------------- schemas

ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {
            "type": "string",
            "enum": list(TOPICS),
        },
        "subtopic": {"type": "string"},
        "difficulty": {"type": "integer"},
        "rubric_points": {"type": "array", "items": {"type": "string"}},
        "common_mistakes": {"type": "array", "items": {"type": "string"}},
        "canonical_question": {"type": "string"},
    },
    "required": ["topic", "difficulty", "rubric_points", "canonical_question"],
}

GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "rubric_hits": {"type": "array", "items": {"type": "boolean"}},
        "score": {"type": "number"},
        "verdict": {"type": "string", "enum": ["strong", "adequate", "weak", "wrong"]},
        "missed": {"type": "array", "items": {"type": "string"}},
        "feedback": {"type": "string"},
        "followup": {"type": "string"},
        "suggested_rating": {"type": "integer"},
        # How it was said, scored apart from whether it was right. An answer
        # that is technically correct and rambles for four minutes loses the
        # offer just as surely as a wrong one, and a single blended score
        # hides which of the two happened.
        "structure": {"type": "integer"},
        "structure_note": {"type": "string"},
    },
    "required": ["rubric_hits", "score", "verdict", "feedback", "suggested_rating"],
}

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                    "source_quote": {"type": "string"},
                    "topic": {
                        "type": "string",
                        "enum": list(TOPICS),
                    },
                    "difficulty": {"type": "integer"},
                },
                "required": ["question", "answer", "source_quote", "topic",
                             "difficulty"],
            },
        }
    },
    "required": ["questions"],
}
